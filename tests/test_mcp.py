import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from applied_opt.mcp_server import (
    MAX_MESSAGE_BYTES, MCPServer, SUPPORTED_VERSIONS,
    TOOLS, call_tool, serve,
)


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def initialize(server, version="2025-11-25"):
    result = server.handle(request("initialize", {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"},
    }))
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return result


class MCPProtocolTests(unittest.TestCase):
    def test_initialization_version_negotiation_and_ready_gate(self):
        server = MCPServer()
        self.assertEqual(server.handle(request("tools/list"))["error"]["code"], -32002)
        self.assertEqual(server.handle(request("ping"))["result"], {})
        response = initialize(server, "unsupported-client-version")
        self.assertEqual(response["result"]["protocolVersion"], SUPPORTED_VERSIONS[0])
        self.assertEqual(server.state, "ready")
        self.assertEqual(initialize(MCPServer(), "2025-06-18")["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(server.handle(request("initialize"))["error"]["code"], -32600)

    def test_listed_tools_have_read_only_annotations_and_output_schema(self):
        server = MCPServer()
        initialize(server)
        tools = server.handle(request("tools/list"))["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["compare_runs", "compile_flow", "plan_execution", "review_code"])
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
            self.assertEqual(tool["outputSchema"]["type"], "object")

    def test_notifications_and_responses_have_no_response_or_tool_effect(self):
        server = MCPServer()
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(server.state, "new")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "tools/call",
                                         "params": {"name": "review_code", "arguments": {"source": "pass"}}}))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "id": 99, "result": {}}))

    def test_invalid_requests_and_unknown_tools_use_protocol_errors(self):
        server = MCPServer()
        initialize(server)
        self.assertEqual(server.handle([])["error"]["code"], -32600)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": True, "method": "ping"})["error"]["code"], -32600)
        self.assertEqual(server.handle(request("unknown"))["error"]["code"], -32601)
        self.assertEqual(server.handle(request("tools/call", {"name": "unknown"}))["error"]["code"], -32602)
        self.assertEqual(server.handle(request("tools/call", {"name": "review_code", "arguments": []}))["error"]["code"], -32602)

    def test_tool_validation_errors_are_actionable_structured_results(self):
        result = call_tool("plan_execution", {"steps": [{"name": "x", "duration_ms": -1}]})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"], json.loads(result["content"][0]["text"]))
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid_arguments")
        self.assertTrue(call_tool("review_code", {"source": "pass", "path": "/private/file"})["isError"])

    def test_input_revision_is_stable_and_conflicts_fail_without_work(self):
        first = call_tool("review_code", {"source": "pass"})["structuredContent"]
        same = call_tool("review_code", {"source": "pass", "expected_revision": first["input_revision"]})
        self.assertFalse(same["isError"])
        stale = call_tool("review_code", {"source": "x = 1", "expected_revision": first["input_revision"]})
        self.assertTrue(stale["isError"])
        self.assertEqual(stale["structuredContent"]["error"]["code"], "revision_mismatch")

    def test_review_never_executes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "should-not-exist"
            source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
            result = call_tool("review_code", {"source": source})
            self.assertFalse(result["isError"])
            self.assertFalse(marker.exists())
            self.assertNotIn("should-not-exist", result["content"][0]["text"])

    def test_planner_and_comparison_are_available_without_execution(self):
        plan = call_tool("plan_execution", {"steps": [
            {"name": "a", "duration_ms": 10, "concurrency_safe": True},
            {"name": "b", "duration_ms": 20, "concurrency_safe": True},
        ]})
        self.assertFalse(plan["isError"])
        self.assertEqual(plan["structuredContent"]["data"]["selected_concurrency"], 2)
        base = {"runs": [{"case_id": "x", "latency_ms": 10, "checks": {"schedule": True}}]}
        candidate = {"runs": [{"case_id": "x", "latency_ms": 1, "checks": {"schedule": False}}]}
        result = call_tool("compare_runs", {"baseline": base, "candidate": candidate})
        self.assertFalse(result["isError"])
        self.assertFalse(result["structuredContent"]["data"]["passed"])

    def test_compile_flow_returns_graph_revision_and_requires_one_input(self):
        result = call_tool("compile_flow", {"source": "async def run(x):\n    y = await fetch(x)\n    return y\n", "format": "mermaid"})
        self.assertFalse(result["isError"], result)
        data = result["structuredContent"]["data"]
        self.assertEqual(len(data["graph_revision"]), 64)
        self.assertIsInstance(data["compiled"], str)
        self.assertTrue(call_tool("compile_flow", {"source": "pass", "graph": {}})["isError"])

    def test_stdio_parse_errors_limits_and_notifications(self):
        messages = [request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "1"}}),
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    request("tools/list", request_id=2)]
        data = b"not-json\n" + b"\n".join(json.dumps(m).encode() for m in messages) + b"\n"
        output = io.StringIO()
        self.assertEqual(serve(io.BytesIO(data), output), 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[-1]["id"], 2)
        output = io.StringIO()
        serve(io.BytesIO(b"x" * (MAX_MESSAGE_BYTES + 20) + b"\n" + json.dumps(request("ping")).encode() + b"\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["result"], {})

    def test_duplicate_keys_and_nonfinite_json_are_rejected(self):
        for line in (b'{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}\n',
                     b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}\n'):
            output = io.StringIO()
            serve(io.BytesIO(line), output)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], -32700)

    def test_depth_limit_covers_protocol_metadata(self):
        nested = {}
        for _ in range(70):
            nested = {"child": nested}
        message = request("ping", {"_meta": nested})
        self.assertEqual(MCPServer().handle(message)["error"]["code"], -32600)

    def test_malformed_initialized_notification_does_not_mark_ready(self):
        server = MCPServer()
        server.handle(request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}}))
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized", "params": []}))
        self.assertEqual(server.state, "initializing")

    def test_module_stdout_contains_only_protocol_messages(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-m", "applied_opt.mcp_server"],
                                input=json.dumps(request("ping")) + "\n", text=True,
                                capture_output=True, env=env, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"], {})


if __name__ == "__main__":
    unittest.main()
