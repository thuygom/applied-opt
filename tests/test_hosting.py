"""Production WSGI behavior and reverse-proxy trust-boundary checks."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from applied_opt.cli import main as cli_main
from applied_opt.hosting import Settings, create_app, main
from applied_opt.server import MAX_REQUEST_BYTES, ReportApplication

TOKEN = "test-hosting-token-123456789"
CONFIG = {"APPLIED_OPT_TOKEN": TOKEN, "APPLIED_OPT_PUBLIC_URL": "https://opt.example.test", "PORT": "8787"}


class HostingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report = Path(self.temp.name) / "report.json"
        self.report.write_text(json.dumps({"project": "private report", "review": {"findings": []}}))
        self.app = create_app({**CONFIG, "APPLIED_OPT_REPORT": str(self.report)})

    def request(self, method="GET", path="/", *, data=None, body=None, headers=None, extra=None, app=None):
        raw = json.dumps(data).encode() if data is not None else (body or b"")
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
            "SERVER_NAME": "127.0.0.1", "SERVER_PORT": "8787",
            "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(raw), "HTTP_HOST": "opt.example.test",
        }
        if data is not None or body is not None:
            environ.update({"CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(raw))})
        for key, value in (headers or {}).items():
            normalized = key.upper().replace("-", "_")
            environ[normalized if normalized in {"CONTENT_TYPE", "CONTENT_LENGTH"} else "HTTP_" + normalized] = value
        environ.update(extra or {})
        start = Mock()
        result = (app or self.app)(environ, start)
        status, response_headers = start.call_args.args
        return int(status.split()[0]), dict(response_headers), b"".join(result)

    def authorized(self, **extra):
        return {"Authorization": "Bearer " + TOKEN, **extra}

    def test_shell_and_health_public_but_report_and_apis_require_token(self) -> None:
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"Applied Opt", body)
        self.assertNotIn(TOKEN.encode(), body)
        self.assertNotIn(b"private report", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, _, body = self.request(path="/healthz")
        self.assertEqual(status, 200)
        health = json.loads(body)
        self.assertEqual(health["mode"], "hosted")
        self.assertIs(health["auth_required"], True)
        for path in ("/api/report", "/demo-report.json"):
            self.assertEqual(self.request(path=path)[0], 401)
            status, _, body = self.request(path=path, headers=self.authorized())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["project"], "private report")
        for path in ("/api/review", "/api/graph"):
            self.assertEqual(self.request("POST", path, data={"source": "x = 1"})[0], 401)

    def test_https_public_origin_works_through_http_proxy(self) -> None:
        status, _, body = self.request("POST", "/api/review", data={"source": "x = 1"}, headers=self.authorized(Origin="https://opt.example.test"))
        self.assertEqual(status, 200)
        self.assertIn("findings", json.loads(body))
        for origin in ("http://opt.example.test", "https://attacker.test", "https://opt.example.test/", "null"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/review", data={"source": "x = 1"}, headers=self.authorized(Origin=origin))[0], 403)
        self.assertEqual(self.request("POST", "/api/review", data={"source": "x = 1"}, headers=self.authorized(**{"Sec-Fetch-Site": "cross-site"}))[0], 403)

    def test_forwarded_headers_cannot_change_the_trusted_host_or_origin(self) -> None:
        headers = self.authorized(Host="attacker.test", **{"X-Forwarded-Host": "opt.example.test", "X-Forwarded-Proto": "https", "Forwarded": "host=opt.example.test;proto=https"})
        self.assertEqual(self.request(path="/api/report", headers=headers)[0], 403)
        headers = self.authorized(Origin="http://opt.example.test", **{"X-Forwarded-Proto": "http"})
        self.assertEqual(self.request("POST", "/api/review", data={"source": "x = 1"}, headers=headers)[0], 403)
        self.assertEqual(self.request(path="/unavailable", headers={"X-Forwarded-Path": "/healthz", "X-Original-URL": "/healthz"})[0], 404)

    def test_internal_health_exception_never_opens_the_apis(self) -> None:
        for host in ("127.0.0.1:8787", "localhost:8787", "[::1]:8787"):
            with self.subTest(host=host):
                self.assertEqual(self.request(path="/healthz", headers={"Host": host})[0], 200)
                self.assertEqual(self.request(path="/api/report", headers=self.authorized(Host=host))[0], 403)
                self.assertEqual(self.request(path="/", headers={"Host": host})[0], 403)
        for host in ("attacker.test:8787", "localhost:9999", "127.0.0.1:443", "opt.example.test:8787", "", "user@opt.example.test", "opt.example.test/path", "opt.example.test?", "opt.example.test#"):
            with self.subTest(host=host):
                self.assertEqual(self.request(path="/healthz", headers={"Host": host})[0], 403)

    def test_explicit_http_origin_supports_local_container_preview(self) -> None:
        app = create_app({**CONFIG, "APPLIED_OPT_PUBLIC_URL": "http://terminal.local:4173", "PORT": "4173"})
        headers = self.authorized(Host="terminal.local:4173", Origin="http://terminal.local:4173")
        self.assertEqual(self.request("POST", "/api/review", data={"source": "x = 1"}, headers=headers, app=app)[0], 200)
        self.assertEqual(self.request(path="/healthz", headers={"Host": "127.0.0.1:4173"}, app=app)[0], 200)

    def test_tokens_in_query_or_wrong_bearer_are_not_accepted(self) -> None:
        self.assertEqual(self.request(path="/api/report", extra={"QUERY_STRING": "token=" + TOKEN})[0], 401)
        self.assertEqual(self.request(path="/api/report", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request(path="/api/report", headers={"Authorization": TOKEN})[0], 401)

    def test_review_and_graph_never_execute_submitted_source(self) -> None:
        marker = Path(self.temp.name) / "executed"
        source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nasync def pipeline():\n    a = await first()\n    b = await second(a)\n"
        for route in ("/api/review", "/api/graph"):
            with self.subTest(route=route):
                status, _, body = self.request("POST", route, data={"source": source}, headers=self.authorized())
                self.assertEqual(status, 200)
                result = json.loads(body)
                if route.endswith("graph"):
                    self.assertEqual(result["graph"]["kind"], "inferred")
                    self.assertEqual(len(result["graph"]["nodes"]), 2)
                    self.assertTrue(all(node["duration_ms"] is None for node in result["graph"]["nodes"]))
                    self.assertTrue(all(key in result for key in ("svg", "mermaid", "plantuml", "revision")))
        self.assertFalse(marker.exists())

    def test_body_limit_and_auth_are_checked_before_reading(self) -> None:
        stream = Mock()
        stream.read.side_effect = AssertionError("Request body must not be read")
        extra = {"wsgi.input": stream, "CONTENT_LENGTH": str(MAX_REQUEST_BYTES + 1), "CONTENT_TYPE": "application/json"}
        self.assertEqual(self.request("POST", "/api/review", headers=self.authorized(), extra=extra)[0], 413)
        self.assertEqual(self.request("POST", "/api/review", extra=extra)[0], 401)
        self.assertEqual(self.request("POST", "/api/review", headers=self.authorized(Origin="https://attacker.test"), extra=extra)[0], 403)
        stream.read.assert_not_called()

    def test_body_framing_and_content_type_fail_cleanly(self) -> None:
        cases = [
            ({}, 415),
            ({"CONTENT_TYPE": "application/json"}, 411),
            ({"CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "-1"}, 400),
            ({"CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "2,2"}, 400),
            ({"CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "20", "wsgi.input": io.BytesIO(b"{}")}, 400),
            ({"HTTP_TRANSFER_ENCODING": "chunked"}, 400),
        ]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                self.assertEqual(self.request("POST", "/api/review", headers=self.authorized(), extra=extra)[0], expected)
        for raw in (b"not JSON", b'{"source":NaN}', b'{"source":"\\ud800"}', b"[]"):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/api/review", body=raw, headers=self.authorized())[0], 400)

    def test_head_has_matching_headers_but_no_body(self) -> None:
        status, headers, body = self.request("HEAD", "/healthz")
        get_status, get_headers, get_body = self.request("GET", "/healthz")
        self.assertEqual((status, headers), (get_status, get_headers))
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["Content-Length"]), len(get_body))
        self.assertEqual(self.request("HEAD", "/api/report")[0], 401)

    def test_report_reload_and_errors_do_not_disclose_contents(self) -> None:
        self.report.write_text('{"project":"updated"}')
        status, _, body = self.request(path="/api/report", headers=self.authorized())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project"], "updated")
        self.report.write_text("private malformed data")
        status, _, body = self.request(path="/api/report", headers=self.authorized())
        self.assertEqual(status, 422)
        self.assertNotIn(b"private", body)
        self.assertEqual(self.request("DELETE", "/api/report", headers=self.authorized())[0], 405)
        self.assertEqual(self.request(path="/../../server.py", headers=self.authorized())[0], 404)

    def test_local_and_hosted_transports_share_api_results(self) -> None:
        local = ReportApplication(self.report, TOKEN, port=8787)
        requests = [("/api/review", {"source": "async def f():\n    a = await first()\n    b = await second()\n"}),
                    ("/api/graph", {"graph": {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b"}]}})]
        for path, data in requests:
            raw = json.dumps(data).encode()
            local_response = local.handle("POST", path, {"Host": "127.0.0.1:8787", "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", "Content-Length": str(len(raw))}, io.BytesIO(raw).read)
            status, headers, body = self.request("POST", path, data=data, headers=self.authorized())
            self.assertEqual(status, local_response.status)
            self.assertEqual(headers, dict(local_response.headers))
            self.assertEqual(json.loads(body), json.loads(local_response.body))


class HostingConfigurationTests(unittest.TestCase):
    def test_required_configuration_and_invalid_values(self) -> None:
        for config in ({}, {"APPLIED_OPT_TOKEN": TOKEN}, {**CONFIG, "APPLIED_OPT_TOKEN": "short"}, {**CONFIG, "APPLIED_OPT_TOKEN": TOKEN + "\n"}):
            with self.subTest(config_keys=sorted(config)):
                with self.assertRaises(ValueError):
                    create_app(config)
        for public_url in ("ftp://opt.example", "https://user:secret@opt.example", "https://opt.example/path", "https://opt.example?q=1", "https://opt.example?", "https://opt.example/#section", "https://opt.example:0", "https://opt.example\\attacker", " https://opt.example"):
            with self.subTest(public_url=public_url):
                with self.assertRaises(ValueError):
                    create_app({**CONFIG, "APPLIED_OPT_PUBLIC_URL": public_url})
        for key, value in (("PORT", "0"), ("PORT", "65536"), ("PORT", "abc"), ("HOST", "0.0.0.0:8787"), ("WEB_CONCURRENCY", "0"), ("APPLIED_OPT_THREADS", "33")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    Settings.from_env({**CONFIG, key: value})

    def test_render_fallback_and_explicit_origin_override(self) -> None:
        fallback = Settings.from_env({"APPLIED_OPT_TOKEN": TOKEN, "RENDER_EXTERNAL_URL": "https://example.onrender.com/"})
        self.assertEqual(fallback.public_origin.host, "example.onrender.com")
        self.assertEqual((fallback.host, fallback.port, fallback.workers, fallback.threads), ("0.0.0.0", 8787, 2, 4))
        explicit = Settings.from_env({**CONFIG, "RENDER_EXTERNAL_URL": "https://example.onrender.com"})
        self.assertEqual(explicit.public_origin.host, "opt.example.test")

    def test_launcher_uses_port_and_proxy_bounds_without_secret_arguments(self) -> None:
        env = {**CONFIG, "PORT": "4173", "WEB_CONCURRENCY": "1", "APPLIED_OPT_THREADS": "2"}
        with patch.dict(os.environ, env, clear=True), patch("applied_opt.hosting.find_spec", return_value=object()), patch("applied_opt.hosting.os.execv") as execute:
            self.assertEqual(main(), 0)
        executable, command = execute.call_args.args
        self.assertEqual(command[0], executable)
        self.assertIn("0.0.0.0:4173", command)
        self.assertIn("applied_opt.hosting:create_app()", command)
        self.assertEqual(command[command.index("--forwarder-headers") + 1], "")
        self.assertEqual(command[command.index("--forwarded-allow-ips") + 1], "")
        self.assertNotIn(TOKEN, " ".join(command))
        self.assertNotIn("--access-logfile", command)

    def test_launcher_reports_missing_dependency_and_cli_dispatches(self) -> None:
        with patch.dict(os.environ, CONFIG, clear=True), patch("applied_opt.hosting.find_spec", return_value=None), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(), 2)
            self.assertIn("applied-opt[hosting]", stderr.getvalue())
            self.assertNotIn(TOKEN, stderr.getvalue())
        with patch("applied_opt.hosting.main", return_value=0) as host:
            self.assertEqual(cli_main(["host"]), 0)
            host.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
