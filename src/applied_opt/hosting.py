"""Production WSGI hosting for the dashboard and its Python APIs.

Install ``applied-opt[hosting]`` and run ``applied-opt host``. The public origin
is explicit because HTTPS commonly terminates at the hosting provider or Caddy;
forwarded headers never choose a trusted origin. Secrets stay in environment
variables and are not placed in command-line arguments or access logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from importlib.util import find_spec
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from .server import Origin, ReportApplication, _error, _report_path, _token


def _integer(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    public_origin: Origin
    report_path: Path | None
    host: str
    port: int
    workers: int
    threads: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        token = _token(env.get("APPLIED_OPT_TOKEN"), required=True)
        public_url = env.get("APPLIED_OPT_PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL")
        if not public_url:
            raise ValueError("APPLIED_OPT_PUBLIC_URL is required (or Render's RENDER_EXTERNAL_URL)")
        try:
            origin = Origin.parse(public_url, configuration=True)
        except ValueError:
            raise ValueError("APPLIED_OPT_PUBLIC_URL must be an http(s) origin without a path, query or credentials") from None
        host = env.get("HOST", "0.0.0.0")
        if not host or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_:" for char in host):
            raise ValueError("HOST must be a hostname or IP address without a port")
        if ":" in host:
            import ipaddress
            try:
                ipaddress.IPv6Address(host)
            except ValueError:
                raise ValueError("HOST must be a hostname or IP address without a port") from None
        return cls(
            token=token,  # required=True establishes a nonempty string.
            public_origin=origin,
            report_path=_report_path(env.get("APPLIED_OPT_REPORT") or None),
            host=host,
            port=_integer(env, "PORT", 8787, 1, 65535),
            workers=_integer(env, "WEB_CONCURRENCY", 2, 1, 32),
            threads=_integer(env, "APPLIED_OPT_THREADS", 4, 1, 32),
        )


class WSGIApplication:
    """WSGI adapter; all routes and trust decisions live in ReportApplication."""

    def __init__(self, settings: Settings) -> None:
        self.application = ReportApplication(
            settings.report_path, settings.token, port=settings.port,
            public_origin=settings.public_origin,
        )

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        headers = {key[5:].replace("_", "-").lower(): value for key, value in environ.items() if key.startswith("HTTP_")}
        if "CONTENT_TYPE" in environ:
            headers["content-type"] = environ["CONTENT_TYPE"]
        if environ.get("CONTENT_LENGTH") not in {None, ""}:
            headers["content-length"] = environ["CONTENT_LENGTH"]
        method = environ.get("REQUEST_METHOD", "GET")
        # SCRIPT_NAME and all X-Forwarded-* values are deliberately unused.
        # Hosted at the public origin root; no forwarding header can reroute it.
        target = environ.get("PATH_INFO", "/")
        try:
            response = self.application.handle(method, target, headers, environ["wsgi.input"].read)
        except Exception:
            response = _error(HTTPStatus.INTERNAL_SERVER_ERROR, "Request could not be processed")
        start_response(f"{int(response.status)} {HTTPStatus(response.status).phrase}", response.headers)
        return [] if method == "HEAD" else [response.body]


def create_app(environ: Mapping[str, str] | None = None) -> WSGIApplication:
    """Gunicorn factory: ``applied_opt.hosting:create_app()``."""
    return WSGIApplication(Settings.from_env(environ))


def main() -> int:
    """Validate deployment configuration and replace this process with Gunicorn."""
    try:
        settings = Settings.from_env()
        if find_spec("gunicorn") is None:
            raise ValueError("Hosting requires Gunicorn; install with: pip install 'applied-opt[hosting]'")
        bind_host = f"[{settings.host}]" if ":" in settings.host else settings.host
        command = [
            sys.executable, "-m", "gunicorn", "applied_opt.hosting:create_app()",
            "--bind", f"{bind_host}:{settings.port}",
            "--workers", str(settings.workers),
            "--worker-class", "gthread", "--threads", str(settings.threads),
            "--timeout", "30", "--graceful-timeout", "30", "--keep-alive", "5",
            "--limit-request-line", "4094", "--limit-request-fields", "50",
            "--limit-request-field_size", "8190",
            "--forwarded-allow-ips", "", "--forwarder-headers", "",
            "--error-logfile", "-",
            # Do not discover/execute a gunicorn.conf.py from an arbitrary cwd.
            "--config", os.devnull,
        ]
        os.execv(sys.executable, command)
    except (OSError, ValueError) as exc:
        print(f"applied-opt host: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
