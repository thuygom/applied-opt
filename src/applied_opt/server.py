"""Shared dashboard/API application and a small local HTTP transport.

The production WSGI transport lives in :mod:`applied_opt.hosting`. Both transports
use the same request validation, review, graph compilation and report loading.
Submitted source is parsed as data; it is never saved, imported or executed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import ipaddress
import json
import os
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

MAX_REQUEST_BYTES = 512 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
WEB_ROOT = Path(__file__).parent / "web"


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authority(raw: str, default_port: int) -> tuple[str, int]:
    """Parse an authority without accepting userinfo, paths or control bytes."""
    if not raw or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in raw) or any(char in raw for char in "/\\?#@"):
        raise ValueError("Invalid host")
    parsed = urlsplit("//" + raw)
    if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Invalid host")
    host = parsed.hostname or ""
    if not host or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-_:" for char in host):
        raise ValueError("Invalid host")
    port = parsed.port if parsed.port is not None else default_port
    if not 1 <= port <= 65535 or raw.endswith(":"):
        raise ValueError("Invalid port")
    return host, port


@dataclasses.dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, raw: str, *, configuration: bool = False) -> Origin:
        if not raw or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in raw) or "?" in raw or "#" in raw:
            raise ValueError("Public URL must be an http(s) origin without a path, query or credentials")
        parsed = urlsplit(raw)
        paths = {"", "/"} if configuration else {""}
        if parsed.scheme not in {"http", "https"} or parsed.path not in paths or parsed.query or parsed.fragment:
            raise ValueError("Public URL must be an http(s) origin without a path, query or credentials")
        host, port = _authority(parsed.netloc, 443 if parsed.scheme == "https" else 80)
        return cls(parsed.scheme, host, port)


def _token(value: str | None, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if value is None or len(value) < 16 or not value.strip() or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("APPLIED_OPT_TOKEN must contain at least 16 printable ASCII characters without spaces")
    return value


def _report_path(value: Path | str | None) -> Path | None:
    path = Path(value).resolve() if value is not None else None
    if path is not None and not path.is_file():
        raise ValueError("Report path must be an existing file")
    return path


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _empty_report() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "project": "Applied Opt",
        "generated_at": None,
        "benchmark": {"kind": "not_measured", "cases": []},
        "review": {"findings": []},
        "quality": {},
        "traces": {"baseline": [], "candidate": []},
        "message": "No report loaded. Generate a report or open a JSON report in the dashboard.",
    }


def _reject_constant(value: str) -> None:
    raise ValueError("JSON numbers must be finite")


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str

    @property
    def headers(self) -> list[tuple[str, str]]:
        return [
            ("Content-Type", self.content_type),
            ("Content-Length", str(len(self.body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
             "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
        ]


def _json(status: int, data: Any) -> Response:
    return Response(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")


def _error(status: int, message: str) -> Response:
    return _json(status, {"error": message})


class ReportApplication:
    """Transport-independent routing, authentication and non-executing APIs."""

    def __init__(
        self,
        report_path: Path | None,
        token: str | None,
        *,
        port: int,
        loopback_only: bool = True,
        public_origin: Origin | None = None,
    ) -> None:
        self.report_path = report_path
        self.token = token
        self.port = port
        self.loopback_only = loopback_only
        self.public_origin = public_origin

    def _host_allowed(self, headers: Mapping[str, str], path: str) -> bool:
        try:
            origin = self.public_origin
            host, port = _authority(headers.get("host", ""), 443 if origin and origin.scheme == "https" else 80)
            if origin is not None:
                if (host, port) == (origin.host, origin.port):
                    return True
                # Containers/proxies may probe loopback directly. This exception
                # only exposes health, never the UI shell, reports or APIs.
                return path == "/healthz" and _loopback(host) and port == self.port
            return port == self.port and (_loopback(host) if self.loopback_only else bool(host))
        except ValueError:
            return False

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        if not self.token:
            return True
        supplied = headers.get("authorization", "")
        return hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + self.token).encode("utf-8"))

    def _same_origin(self, headers: Mapping[str, str]) -> bool:
        if headers.get("sec-fetch-site", "").lower() == "cross-site":
            return False
        origin = headers.get("origin")
        if not origin:
            return True  # Bearer-authenticated CLI clients do not send Origin.
        try:
            expected = self.public_origin or Origin.parse("http://" + headers.get("host", ""))
            return Origin.parse(origin) == expected
        except ValueError:
            return False

    def _report(self) -> dict[str, Any]:
        if self.report_path is None:
            return _empty_report()
        # Bound the actual read even if a file is replaced while serving it.
        with self.report_path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("Report exceeds 8 MiB limit")
        report = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(report, dict):
            raise ValueError("Report must be a JSON object")
        json.dumps(report, allow_nan=False)
        return report

    def handle(self, method: str, target: str, headers: Mapping[str, str], read_body: Callable[[int], bytes]) -> Response:
        """Handle one request. Headers are normalized here for both transports."""
        headers = {key.lower(): value for key, value in headers.items()}
        try:
            path = urlsplit(target).path
        except ValueError:
            return _error(HTTPStatus.BAD_REQUEST, "Invalid request path")
        if not self._host_allowed(headers, path):
            return _error(HTTPStatus.FORBIDDEN, "Untrusted Host header")
        if method in {"GET", "HEAD"}:
            if path in {"/", "/index.html"}:
                try:
                    return Response(HTTPStatus.OK, (WEB_ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
                except OSError:
                    return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard asset unavailable")
            if path == "/healthz":
                return _json(HTTPStatus.OK, {"status": "ok", "service": "applied-opt", "review_api": True,
                                             "mode": "hosted" if self.public_origin else "local", "auth_required": bool(self.token)})
            if path in {"/api/report", "/demo-report.json"}:
                if not self._authorized(headers):
                    return _error(HTTPStatus.UNAUTHORIZED, "API token required")
                try:
                    return _json(HTTPStatus.OK, self._report())
                except (OSError, ValueError, UnicodeError, RecursionError):
                    return _error(HTTPStatus.UNPROCESSABLE_ENTITY, "Report cannot be read as a JSON object")
            return _error(HTTPStatus.NOT_FOUND, "Not found")
        if method != "POST":
            return _error(HTTPStatus.METHOD_NOT_ALLOWED, "Use GET, HEAD or POST")
        if not self._same_origin(headers):
            return _error(HTTPStatus.FORBIDDEN, "Cross-origin requests are not allowed")
        if path not in {"/api/review", "/api/graph"}:
            return _error(HTTPStatus.NOT_FOUND, "Not found")
        if not self._authorized(headers):
            return _error(HTTPStatus.UNAUTHORIZED, "API token required")
        if headers.get("transfer-encoding"):
            return _error(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported")
        if headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
            return _error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Use application/json")
        length_header = headers.get("content-length")
        if length_header is None:
            return _error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        if not length_header.isascii() or not length_header.isdigit() or len(length_header) > 10:
            return _error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
        length = int(length_header)
        if length > MAX_REQUEST_BYTES:
            return _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Source request exceeds 512 KiB")
        try:
            raw = read_body(length)
            if len(raw) != length:
                raise ValueError
            request = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(request, dict):
                raise ValueError
            if path == "/api/review" and not isinstance(request.get("source"), str):
                raise ValueError
            if path == "/api/graph":
                inputs = [key for key in ("source", "report", "graph") if key in request]
                if len(inputs) != 1:
                    raise ValueError
                if inputs[0] == "source" and not isinstance(request["source"], str):
                    raise ValueError
                if inputs[0] != "source" and not isinstance(request[inputs[0]], dict):
                    raise ValueError
                if not isinstance(request.get("variant", "candidate"), str) or request.get("variant", "candidate") not in {"baseline", "candidate"}:
                    raise ValueError
                if not isinstance(request.get("format", "svg"), str) or request.get("format", "svg") not in {"svg", "mermaid", "plantuml"}:
                    raise ValueError
            filename = request.get("filename", "submitted.py")
            if not isinstance(filename, str) or not filename or len(filename) > 240 or "\x00" in filename:
                raise ValueError
            filename.encode("utf-8")
            if "source" in request:
                request["source"].encode("utf-8")
        except (ValueError, UnicodeError, OSError, RecursionError):
            message = ("Expected JSON with source and optional filename strings" if path == "/api/review"
                       else "Expected one of source, report, or graph; variant baseline/candidate; format svg/mermaid/plantuml")
            return _error(HTTPStatus.BAD_REQUEST, message)
        try:
            if path == "/api/graph":
                from .diagrams import compile_graph, graph_from_report, graph_from_source, normalize_graph

                if "source" in request:
                    graph = graph_from_source(request["source"], filename=filename)
                elif "report" in request:
                    graph = graph_from_report(request["report"], variant=request.get("variant", "candidate"))
                else:
                    graph = request["graph"]
                graph = normalize_graph(graph)
                artifacts = {kind: compile_graph(graph, format=kind) for kind in ("mermaid", "plantuml", "svg")}
                revision = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:12]
                return _json(HTTPStatus.OK, {"graph": graph, **artifacts, "revision": revision})
            from .review import review_source

            result = _jsonable(review_source(request["source"], filename=filename))
            return _json(HTTPStatus.OK, {"findings": result} if isinstance(result, list) else result)
        except (SyntaxError, ValueError, RecursionError):
            return _error(HTTPStatus.UNPROCESSABLE_ENTITY, "Source or graph could not be analyzed; check syntax, node IDs, edges, and limits")
        except Exception:
            # No traceback or submitted source in HTTP responses.
            return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "Review failed; inspect the server installation")


class ReportServer(ThreadingHTTPServer):
    """Local transport with a fixed report path; no arbitrary file endpoint."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], report_path: Path | None, token: str | None) -> None:
        self.application = ReportApplication(report_path, token, port=address[1], loopback_only=_loopback(address[0]))
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, ReportHandler)
        self.application.port = self.server_port

    @property
    def token(self) -> str | None:
        return self.application.token

    @token.setter
    def token(self, value: str | None) -> None:
        self.application.token = _token(value)

    @property
    def report_path(self) -> Path | None:
        return self.application.report_path

    @property
    def loopback_only(self) -> bool:
        return self.application.loopback_only


class ReportHandler(BaseHTTPRequestHandler):
    server: ReportServer
    server_version = "AppliedOpt"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log source, query strings, Authorization, or report data.
        return

    def _dispatch(self) -> None:
        response = self.server.application.handle(self.command, self.path, dict(self.headers), self.rfile.read)
        self.send_response(response.status)
        for key, value in response.headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _dispatch


def create_server(
    report_path: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
) -> ReportServer:
    """Create a local server. Use ``applied-opt host`` behind an HTTPS proxy."""
    token = _token(token if token is not None else os.environ.get("APPLIED_OPT_TOKEN"))
    if not _loopback(host) and not token:
        raise ValueError("Non-loopback binding requires APPLIED_OPT_TOKEN or token (at least 16 characters)")
    return ReportServer((host, port), _report_path(report_path), token)


def serve(
    report_path: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
) -> None:
    """Serve locally until interrupted; submitted source is never executed."""
    with create_server(report_path=report_path, host=host, port=port, token=token) as server:
        print(f"Applied Opt dashboard: http://{host}:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
