"""HTTP integration checks for the report server's trust boundaries."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from applied_opt.server import MAX_REQUEST_BYTES, create_server


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.report_path = Path(self.temp.name) / "report.json"
        self.report_path.write_text(json.dumps({"project": "original", "review": {"findings": []}}))
        self.env_patch = patch.dict(os.environ, {"APPLIED_OPT_TOKEN": ""})
        # Pass token=None with no env entry, rather than weakening empty-token validation.
        self.env_patch.start()
        os.environ.pop("APPLIED_OPT_TOKEN", None)
        self.server = create_server(self.report_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env_patch.stop()
        self.temp.cleanup()

    def request(self, method: str, path: str, body: bytes | str | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def json_request(self, data, headers=None):
        return self.request("POST", "/api/review", json.dumps(data), {"Content-Type": "application/json", **(headers or {})})

    def test_health_and_static_dashboard(self) -> None:
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["service"], "applied-opt")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Applied Opt", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_report_reloads_from_only_the_configured_file(self) -> None:
        status, _, body = self.request("GET", "/api/report")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project"], "original")
        self.report_path.write_text(json.dumps({"project": "changed"}))
        status, _, body = self.request("GET", "/demo-report.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project"], "changed")
        status, _, _ = self.request("GET", "/../../server.py")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/%2e%2e/server.py")
        self.assertEqual(status, 404)

    def test_invalid_and_nonfinite_report_does_not_leak_contents(self) -> None:
        for content in ("private secret malformed", '{"private": NaN}', "[]"):
            self.report_path.write_text(content)
            status, _, body = self.request("GET", "/api/report")
            self.assertEqual(status, 422)
            self.assertNotIn(b"private secret", body)

    def test_dns_rebinding_host_is_rejected(self) -> None:
        status, _, body = self.request("GET", "/api/report", headers={"Host": f"attacker.example:{self.server.server_port}"})
        self.assertEqual(status, 403)
        self.assertNotIn(b"original", body)

    def test_cross_origin_post_is_rejected(self) -> None:
        status, _, _ = self.json_request({"source": "x = 1"}, {"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.json_request({"source": "x = 1"}, {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.json_request({"source": "x = 1"}, {"Origin": f"https://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(status, 403)

    def test_review_input_is_forwarded_without_execution(self) -> None:
        module = types.ModuleType("applied_opt.review")
        seen = []
        def review_source(source, filename="submitted.py"):
            seen.append((source, filename))
            return {"findings": [{"title": "Observed", "path": filename}], "files": [filename]}
        module.review_source = review_source
        source = "raise RuntimeError('must never run')"
        with patch.dict("sys.modules", {"applied_opt.review": module}):
            status, _, body = self.json_request({"source": source, "filename": "<script>alert(1)</script>.py"}, {"Origin": f"http://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(status, 200)
        self.assertEqual(seen, [(source, "<script>alert(1)</script>.py")])
        self.assertEqual(json.loads(body)["findings"][0]["path"], "<script>alert(1)</script>.py")

    def test_invalid_body_and_content_type(self) -> None:
        status, _, _ = self.request("POST", "/api/review", "not json", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/review", "x=1", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        for data in ({"source": 1}, ["code"], {"source": "x=1", "filename": 2}, {"source": "x=1", "filename": "\x00"}):
            with self.subTest(data=data):
                status, _, _ = self.json_request(data)
                self.assertEqual(status, 400)

    def test_graph_compilation_escapes_malicious_labels(self) -> None:
        label = '</text><script>alert(1)</script> !include http://example.test'
        graph = {"kind": "declared", "nodes": [{"id": "a", "label": label, "duration_ms": None}, {"id": "b", "label": "Output", "duration_ms": 10}], "edges": [{"source": "a", "target": "b"}]}
        status, _, body = self.request("POST", "/api/graph", json.dumps({"graph": graph}), {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(len(result["revision"]), 12)
        self.assertNotIn("<script>", result["svg"])
        self.assertNotIn("<foreignObject", result["svg"])
        self.assertNotIn("!include", result["plantuml"])
        self.assertIn("flowchart", result["mermaid"])
        self.assertEqual(result["graph"]["kind"], "declared")

    def test_graph_rejects_cycles_and_malformed_options(self) -> None:
        graph = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]}
        status, _, _ = self.request("POST", "/api/graph", json.dumps({"graph": graph}), {"Content-Type": "application/json"})
        self.assertEqual(status, 422)
        for data in ({"graph": graph, "variant": []}, {"graph": graph, "format": {}}, {"graph": graph, "source": "x=1"}):
            with self.subTest(data=data):
                status, _, _ = self.request("POST", "/api/graph", json.dumps(data), {"Content-Type": "application/json"})
                self.assertEqual(status, 400)

    def test_graph_source_inference_never_executes_source(self) -> None:
        source = "raise RuntimeError('must never execute')\nasync def pipeline():\n    a = await first()\n    b = await second(a)\n"
        status, _, body = self.request("POST", "/api/graph", json.dumps({"source": source}), {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        graph = json.loads(body)["graph"]
        self.assertEqual(graph["kind"], "inferred")
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertTrue(all(node["duration_ms"] is None for node in graph["nodes"]))

    def test_request_size_limit_precedes_read_or_analysis(self) -> None:
        status, _, _ = self.request("POST", "/api/review", b"{}", {"Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BYTES + 1)})
        self.assertEqual(status, 413)

    def test_api_token_never_accepted_in_query(self) -> None:
        self.server.token = "a-valid-test-token-123"
        status, _, _ = self.request("GET", "/api/report?token=a-valid-test-token-123")
        self.assertEqual(status, 401)
        status, _, body = self.request("GET", "/api/report", headers={"Authorization": "Bearer a-valid-test-token-123"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["project"], "original")
        status, _, _ = self.json_request({"source": "x=1"})
        self.assertEqual(status, 401)

    def test_non_loopback_requires_token(self) -> None:
        with self.assertRaises(ValueError):
            create_server(host="0.0.0.0", port=0)
        with self.assertRaises(ValueError):
            create_server(host="0.0.0.0", port=0, token="short")

    def test_head_has_headers_and_no_body(self) -> None:
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)


if __name__ == "__main__":
    unittest.main()
