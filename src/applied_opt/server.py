"""Local, read-only report server and non-executing Python review endpoint.

The dashboard understands schema_version 1.0 reports containing ``project``,
``generated_at``, ``benchmark``, ``review.findings``, ``quality``, ``traces`` and
``tradeoffs``. Missing sections render explicit empty states. The report file is
read afresh on every request; source submitted to /api/review is never saved or
executed. Intended for loopback or same-port container forwarding. TLS reverse-
proxy host rewriting is not supported in this version.
"""

from __future__ import annotations

import dataclasses
import hmac
import hashlib
import ipaddress
import json
import os
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
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


class ReportServer(ThreadingHTTPServer):
    """A server instance with a fixed report path; no arbitrary file endpoint."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        report_path: Path | None,
        token: str | None,
    ) -> None:
        self.report_path = report_path
        self.token = token
        self.loopback_only = _loopback(address[0])
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, ReportHandler)


class ReportHandler(BaseHTTPRequestHandler):
    server: ReportServer
    server_version = "AppliedOpt"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log source, query strings, Authorization, or submitted report data.
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(
            status,
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_allowed(self) -> bool:
        raw = self.headers.get("Host", "")
        try:
            parsed = urlsplit("//" + raw)
            host = parsed.hostname or ""
            if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                return False
            if (parsed.port or 80) != self.server.server_port:
                return False
        except ValueError:
            return False
        return _loopback(host) if self.server.loopback_only else bool(host)

    def _authorized(self) -> bool:
        if not self.server.token:
            return True
        supplied = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _same_origin(self) -> bool:
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True  # CLI clients do not normally set Origin.
        try:
            parsed = urlsplit(origin)
            expected = urlsplit("http://" + self.headers.get("Host", ""))
            return (
                parsed.scheme == "http"
                and not parsed.username
                and not parsed.password
                and parsed.netloc == expected.netloc
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    def _report(self) -> dict[str, Any]:
        path = self.server.report_path
        if path is None:
            return _empty_report()
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("Report exceeds 8 MiB limit")
        # A report is trusted user-selected JSON, not an importable plugin.
        report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("Report must be a JSON object")
        # Also reject non-JSON finite number extensions on read.
        json.dumps(report, allow_nan=False)
        return report

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._error(HTTPStatus.FORBIDDEN, "Untrusted Host header")
            return
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, (WEB_ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._json(HTTPStatus.OK, {"status": "ok", "service": "applied-opt", "review_api": True})
        elif path in {"/api/report", "/demo-report.json"}:
            if not self._authorized():
                self._error(HTTPStatus.UNAUTHORIZED, "API token required")
                return
            try:
                self._json(HTTPStatus.OK, self._report())
            except (OSError, ValueError, UnicodeError, RecursionError):
                self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "Report cannot be read as a JSON object")
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        if not self._host_allowed() or not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "Cross-origin requests are not allowed")
            return
        path = urlsplit(self.path).path
        if path not in {"/api/review", "/api/graph"}:
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self._authorized():
            self._error(HTTPStatus.UNAUTHORIZED, "API token required")
            return
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Use application/json")
            return
        try:
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                self._error(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
                return
            length = int(length_header)
            if length < 0:
                raise ValueError
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if length > MAX_REQUEST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Source request exceeds 512 KiB")
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            request = json.loads(raw.decode("utf-8"))
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
        except (ValueError, UnicodeError, TimeoutError, RecursionError):
            message = ("Expected JSON with source and optional filename strings" if path == "/api/review"
                       else "Expected one of source, report, or graph; variant baseline/candidate; format svg/mermaid/plantuml")
            self._error(HTTPStatus.BAD_REQUEST, message)
            return
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
                self._json(HTTPStatus.OK, {"graph": graph, **artifacts, "revision": revision})
                return
            from .review import review_source

            result = _jsonable(review_source(request["source"], filename=filename))
            if isinstance(result, list):
                result = {"findings": result}
            self._json(HTTPStatus.OK, result)
        except (SyntaxError, ValueError, RecursionError):
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, "Source or graph could not be analyzed; check syntax, node IDs, edges, and limits")
        except Exception:
            # Do not disclose tracebacks or submitted code in an HTTP response.
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Review failed; inspect the local installation")


def create_server(
    report_path: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
) -> ReportServer:
    """Create an unstarted report server. Non-loopback binds require an API token.

    Set ``APPLIED_OPT_TOKEN`` or pass ``token`` explicitly. This local server
    supports same-port container forwarding, not TLS reverse-proxy host rewriting.
    """
    token = token if token is not None else os.environ.get("APPLIED_OPT_TOKEN")
    if token is not None and (not token.strip() or len(token) < 16):
        raise ValueError("API token must contain at least 16 characters")
    if not _loopback(host) and not token:
        raise ValueError("Non-loopback binding requires APPLIED_OPT_TOKEN or token (at least 16 characters)")
    path = Path(report_path).resolve() if report_path is not None else None
    if path is not None and not path.is_file():
        raise ValueError("Report path must be an existing file")
    return ReportServer((host, port), path, token)


def serve(
    report_path: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
) -> None:
    """Serve until interrupted. This function never executes submitted source."""
    with create_server(report_path=report_path, host=host, port=port, token=token) as server:
        print(f"Applied Opt dashboard: http://{host}:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
