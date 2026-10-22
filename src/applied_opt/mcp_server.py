"""Small, dependency-free MCP stdio server for bounded, read-only analysis.

Implements the initialize-era 2025-11-25 and 2025-06-18 protocol subset:
initialize, ping, tools/list, tools/call, and notifications/initialized. This is
not a complete MCP SDK or a remote HTTP service. See docs/mcp.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Mapping
from typing import Any, BinaryIO, TextIO

from . import __version__
from .evaluation import compare_reports
from .planning import Work, compare_strategies
from .review import review_source


SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18")
MAX_MESSAGE_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 4_194_304
MAX_DEPTH = 64


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_NAME = {"type": "string", "minLength": 1, "maxLength": 128}
_SOURCE = {"type": "string", "maxLength": 250_000}
_REVISION = {"type": "string", "minLength": 64, "maxLength": 64}
_STEP = _object({
    "name": _NAME,
    "duration_ms": {"type": "number", "exclusiveMinimum": 0, "maximum": 1_000_000_000},
    "depends_on": {"type": "array", "items": _NAME, "maxItems": 100},
    "resource": _NAME,
    "concurrency_safe": {"type": "boolean", "default": False},
    "cost_usd": {"type": "number", "minimum": 0, "maximum": 1_000_000},
}, ("name", "duration_ms"))
_RUN = _object({
    "case_id": _NAME,
    "latency_ms": {"type": "number", "minimum": 0, "maximum": 1_000_000_000},
    "checks": {"type": "object", "additionalProperties": {"type": "boolean"}, "minProperties": 1, "maxProperties": 100},
    "error_type": {"type": ["string", "null"], "maxLength": 128},
    "error": {"type": ["string", "boolean", "null"], "maxLength": 256},
    "status": {"type": "string", "maxLength": 64},
}, ("case_id", "latency_ms", "checks"))
_RUN_REPORT = _object({"runs": {"type": "array", "items": _RUN, "minItems": 1, "maxItems": 1000}}, ("runs",))
_OUTPUT_SCHEMA = _object({
    "schema_version": {"type": "string", "const": "1.0"},
    "tool": {"type": "string"},
    "input_revision": {"type": "string"},
    "ok": {"type": "boolean"},
    "data": {"type": ["object", "null"]},
    "error": {"type": ["object", "null"]},
}, ("schema_version", "tool", "input_revision", "ok", "data", "error"))


def _tool(name: str, description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "outputSchema": _OUTPUT_SCHEMA,
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    }


_COMPILE_SCHEMA = _object({
    "source": _SOURCE,
    "filename": {"type": "string", "maxLength": 256},
    "graph": {"type": "object"},
    "report": {"type": "object"},
    "variant": {"type": "string", "enum": ["baseline", "candidate"], "default": "candidate"},
    "format": {"type": "string", "enum": ["mermaid", "plantuml", "svg"], "default": "mermaid"},
    "expected_revision": _REVISION,
})
_COMPILE_SCHEMA["oneOf"] = [{"required": [name]} for name in ("source", "graph", "report")]

TOOLS = tuple(sorted((
    _tool("review_code", "Inspect supplied Python source without importing or running it. Findings are static hypotheses, not measured speedups.",
          _object({"source": _SOURCE, "filename": {"type": "string", "maxLength": 256},
                   "expected_revision": _REVISION}, ("source",))),
    _tool("plan_execution", "Estimate sequential versus bounded parallel scheduling from explicit durations, dependencies and resource contracts. Does not execute stages.",
          _object({
              "steps": {"type": "array", "items": _STEP, "minItems": 1, "maxItems": 100},
              "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 128, "default": 4},
              "resource_limits": {"type": "object", "maxProperties": 100,
                                  "additionalProperties": {"type": "integer", "minimum": 1, "maximum": 128}},
              "contention_multiplier": {"type": "object", "maxProperties": 100,
                                        "additionalProperties": {"type": "number", "minimum": 1, "maximum": 1000}},
              "launch_overhead_ms": {"type": "number", "minimum": 0, "maximum": 1_000_000_000},
              "min_improvement": {"type": "number", "minimum": 0, "exclusiveMaximum": 1},
              "expected_revision": _REVISION,
          }, ("steps",))),
    _tool("compile_flow", "Compile one supplied graph, Python source, or report snapshot into Mermaid, PlantUML or SVG. Returns a graph revision; never renders remote content or runs source.", _COMPILE_SCHEMA),
    _tool("compare_runs", "Compare saved baseline/candidate latency samples and stable named checks. Missing cases, failed checks, explicit errors and p95 regressions block acceptance.",
          _object({"baseline": _RUN_REPORT, "candidate": _RUN_REPORT,
                   "tolerance": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.05},
                   "expected_revision": _REVISION}, ("baseline", "candidate"))),
), key=lambda item: item["name"]))
_BY_NAME = {tool["name"]: tool for tool in TOOLS}


class InputError(ValueError):
    """A bounded message that does not include submitted source or values."""


def _validate(value: Any, schema: Mapping[str, Any], path: str = "arguments", depth: int = 0) -> None:
    """Validate the JSON-schema keywords used by this fixed tool registry."""
    if depth > MAX_DEPTH:
        raise InputError("Input nesting exceeds the supported limit")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {
        "object": type(value) is dict, "array": type(value) is list,
        "string": type(value) is str, "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "null": value is None,
    }
    if types and not any(matches.get(kind, False) for kind in types):
        raise InputError(f"{path}: invalid JSON type")
    if "const" in schema and value != schema["const"]:
        raise InputError(f"{path}: unsupported constant")
    if "enum" in schema and value not in schema["enum"]:
        raise InputError(f"{path}: unsupported option")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", MAX_MESSAGE_BYTES):
            raise InputError(f"{path}: string length is outside the supported limit")
    elif type(value) in (int, float):
        if not math.isfinite(value):
            raise InputError(f"{path}: finite numbers are required")
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise InputError(f"{path}: number is outside the supported range")
        if value <= schema.get("exclusiveMinimum", -math.inf) or value >= schema.get("exclusiveMaximum", math.inf):
            raise InputError(f"{path}: number is outside the supported range")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 2000):
            raise InputError(f"{path}: list length is outside the supported limit")
        for item in value:
            _validate(item, schema.get("items", {}), f"{path}[]", depth + 1)
    elif isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", 2000):
            raise InputError(f"{path}: object size is outside the supported limit")
        if any(key not in value for key in schema.get("required", [])):
            raise InputError(f"{path}: required field is missing")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if not isinstance(key, str):
                raise InputError("JSON object keys must be strings")
            if key in properties:
                _validate(item, properties[key], f"{path}.{key}", depth + 1)
            elif additional is False:
                raise InputError(f"{path}: unknown field")
            else:
                _validate(item, additional if isinstance(additional, dict) else {}, path, depth + 1)
    if "oneOf" in schema:
        matched = 0
        for option in schema["oneOf"]:
            try:
                _validate(value, option, path, depth + 1)
                matched += 1
            except InputError:
                pass
        if matched != 1:
            raise InputError(f"{path}: supply exactly one source, graph, or report")


def revision(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _result(name: str, input_revision: str, *, data: dict[str, Any] | None = None,
            error: str | None = None, code: str = "invalid_arguments") -> dict[str, Any]:
    structured = {
        "schema_version": "1.0", "tool": name, "input_revision": input_revision,
        "ok": error is None, "data": data,
        "error": None if error is None else {"code": code, "message": error},
    }
    return {"content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, allow_nan=False)}],
            "structuredContent": structured, "isError": error is not None}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call one known tool directly; validate all inputs and never execute source."""
    if name not in _BY_NAME:
        raise KeyError("Unknown tool")
    input_revision = ""
    try:
        _validate(arguments, _BY_NAME[name]["inputSchema"])
        payload = {key: value for key, value in arguments.items() if key != "expected_revision"}
        input_revision = revision(payload)
        if "expected_revision" in arguments and arguments["expected_revision"] != input_revision:
            return _result(name, input_revision, error="Input snapshot changed; use the current revision before retrying.", code="revision_mismatch")
        if name == "review_code":
            data = review_source(payload["source"], payload.get("filename", "input.py"))
        elif name == "plan_execution":
            steps = [Work(**{**item, "depends_on": tuple(item.get("depends_on", ()))}) for item in payload["steps"]]
            options = {key: value for key, value in payload.items() if key != "steps"}
            data = compare_strategies(steps, **options)
        elif name == "compare_runs":
            data = compare_reports(payload["baseline"], payload["candidate"], tolerance=payload.get("tolerance", .05))
        else:
            from .diagrams import compile_graph, graph_from_report, graph_from_source, normalize_graph
            if "source" in payload:
                graph = graph_from_source(payload["source"], payload.get("filename", "input.py"))
            elif "report" in payload:
                graph = graph_from_report(payload["report"], variant=payload.get("variant", "candidate"))
            else:
                graph = payload["graph"]
            graph = normalize_graph(graph)
            fmt = payload.get("format", "mermaid")
            data = {"graph": graph, "graph_revision": revision(graph), "format": fmt,
                    "compiled": compile_graph(graph, format=fmt)}
        return _result(name, input_revision, data=data)
    except InputError as exc:
        return _result(name, input_revision, error=str(exc))
    except (ValueError, TypeError, KeyError, RecursionError):
        return _result(name, input_revision, error="Input does not satisfy the tool contract; check its schema, dependencies, graph references and numeric values.")
    except Exception:
        return _result(name, input_revision, error="Analysis could not complete; inspect the local implementation and retry with a smaller valid snapshot.", code="analysis_failed")


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise InputError("Message nesting exceeds the supported limit")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)


class MCPServer:
    """One stdio connection; tool calls themselves are stateless calculations."""

    def __init__(self) -> None:
        self.state = "new"
        self.protocol_version: str | None = None

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "Expected one JSON-RPC object; batching is unsupported")
        # A notification never receives a response, even if unknown. Do not run
        # tools from a notification or treat it as initialization.
        if "id" not in message and isinstance(message.get("method"), str):
            if message.get("jsonrpc") == "2.0" and message["method"] == "notifications/initialized" and self.state == "initializing" and isinstance(message.get("params", {}), dict):
                self.state = "ready"
            return None
        if message.get("jsonrpc") == "2.0" and "method" not in message and ("result" in message or "error" in message):
            # No server-to-client requests are issued; ignore unsolicited replies.
            return None
        request_id = message.get("id")
        if type(request_id) not in (str, int, float) or (isinstance(request_id, float) and not math.isfinite(request_id)):
            return _error(None, -32600, "Request id must be a string or finite number")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(request_id, -32600, "Invalid JSON-RPC request")
        try:
            _check_depth(message)
        except (InputError, RecursionError):
            return _error(request_id, -32600, "Message nesting exceeds the supported limit")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Parameters must be an object")
        method = message["method"]
        if method == "ping":
            result = {}
        elif method == "initialize":
            if self.state != "new":
                return _error(request_id, -32600, "Connection is already initialized")
            client = params.get("clientInfo")
            requested = params.get("protocolVersion")
            if not isinstance(requested, str) or not requested or not isinstance(params.get("capabilities"), dict) or not isinstance(client, dict) or not isinstance(client.get("name"), str) or not isinstance(client.get("version"), str):
                return _error(request_id, -32602, "Initialization requires protocolVersion, capabilities and clientInfo name/version")
            self.protocol_version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
            self.state = "initializing"
            result = {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "applied-opt", "version": __version__},
                "instructions": "Review supplied code and compare execution evidence. All tools are read-only local calculations. Source is never executed. Treat static findings and schedule estimates as hypotheses; preserve functional checks before accepting changes.",
            }
        elif self.state != "ready":
            return _error(request_id, -32002, "Complete initialize and notifications/initialized first")
        elif method == "tools/list":
            if "cursor" in params:
                return _error(request_id, -32602, "This bounded tool list does not use cursors")
            result = {"tools": list(TOOLS)}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, -32602, "Tool call requires a name and object arguments")
            if name not in _BY_NAME:
                return _error(request_id, -32602, "Unknown tool")
            if "task" in params:
                return _error(request_id, -32602, "Task-augmented execution is unsupported")
            result = call_tool(name, arguments)
        else:
            return _error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def serve(stdin: BinaryIO, stdout: TextIO) -> int:
    server = MCPServer()
    while True:
        raw = stdin.readline(MAX_MESSAGE_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_MESSAGE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stdin.readline(MAX_MESSAGE_BYTES + 1)
            response = _error(None, -32600, "Message exceeds the one MiB limit")
        else:
            try:
                message = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_pairs)
            except (UnicodeDecodeError, ValueError, RecursionError):
                response = _error(None, -32700, "Invalid UTF-8 JSON message")
            else:
                response = server.handle(message)
        if response is None:
            continue
        try:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
                encoded = json.dumps(_error(response.get("id"), -32603, "Result exceeds the four MiB limit; use a smaller input"))
            stdout.write(encoded + "\n")
            stdout.flush()
        except BrokenPipeError:
            return 0


def main() -> int:
    return serve(sys.stdin.buffer, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
