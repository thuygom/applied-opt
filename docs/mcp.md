# MCP connection

Applied Opt includes a dependency-free, local **stdio MCP server**. An MCP client
can review a supplied Python snippet, compare execution strategies, compile a
flow diagram, and compare saved benchmark runs. No reviewed code is imported or
executed. No model is called, no source is uploaded, and no repository is changed.

## Connect a client

Install this trusted checkout into a Python environment, then configure an MCP
client using its absolute interpreter path:

```json
{
  "mcpServers": {
    "applied-opt": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["-m", "applied_opt.mcp_server"]
    }
  }
}
```

The CLI also accepts `applied-opt mcp`. Client configuration keys differ; choose
the client's stdio/subprocess transport. Do not place a shell wrapper that prints
banners before this command. Standard output contains only newline-delimited
JSON-RPC messages. End the connection by closing stdin.

## Tools

| Tool | Inputs | Result |
| --- | --- | --- |
| `review_code` | `source`, optional `filename` | Static findings with file/line evidence and confidence. |
| `plan_execution` | `steps`, optional concurrency, resource and contention settings | Explicit scheduling estimates; no execution. |
| `compile_flow` | Exactly one of `source`, `graph`, `report`; optional `format` and report `variant` | Normalized graph, graph revision, and Mermaid/PlantUML/SVG text. |
| `compare_runs` | `baseline`, `candidate`, optional `tolerance` | Per-case latency and named-check gate. |

All tools declare `readOnlyHint: true`, `destructiveHint: false`,
`idempotentHint: true`, and `openWorldHint: false`. These describe this server's
behavior; annotations are not a substitute for trusting the installed program.

Each step supplied to `plan_execution` requires `name` and `duration_ms`.
`depends_on`, `resource`, `cost_usd`, and `concurrency_safe` are optional;
**concurrency is unsafe by default**. A run report uses this shape:

```json
{
  "runs": [
    {
      "case_id": "explicit-meeting",
      "latency_ms": 123.4,
      "checks": {"schedule_preserved": true, "task_preserved": true}
    }
  ]
}
```

Supply checks that were actually executed against ground truth. A faster
candidate with missing or failed checks cannot pass. Tool success only means
the comparison completed; inspect `data.passed` for its acceptance outcome.

## Result and snapshot contract

Every tool returns the same object in `structuredContent` and a JSON text block:

```json
{
  "schema_version": "1.0",
  "tool": "review_code",
  "input_revision": "<SHA-256 of the canonical request arguments>",
  "ok": true,
  "data": {},
  "error": null
}
```

An optional `expected_revision` must match the current arguments, excluding
`expected_revision` itself. A mismatch returns a tool error instead of analyzing
a different snapshot. This is a stateless integrity check; it does not lock or
persist a shared project. The diagram result also contains `graph_revision`, a
SHA-256 of its normalized graph, allowing a UI to associate a preview with the
exact graph that produced it.

This source/preview alignment takes inspiration from the public
[Paper Studio MCP workflow](https://github.com/thuygom/paper-studio-mcp/blob/main/docs/mcp.md).
The implementation here is independent and read-only.

## Protocol coverage and limits

This implementation intentionally supports the initialize-era revisions
**2025-11-25** and **2025-06-18**, with `initialize`, `ping`, `tools/list`,
`tools/call`, and `notifications/initialized`. If a client proposes a different
revision during `initialize`, the response offers 2025-11-25; the client must
support that revision to continue. Wait for initialization, send the initialized
notification, then invoke tools.

This is a small protocol subset, not a full MCP SDK. It does not implement the
newer `server/discover` protocol era, Streamable HTTP, OAuth, remote access,
resources, prompts, sampling, task-augmented execution, progress streams, or
batch requests. Tool calls are short, bounded, synchronous local calculations.
Cancellation notifications receive no response; they do not interrupt a running
calculation. For hosted or long-running integration, use an official SDK and
implement the necessary transport, authorization, and cancellation behavior.

Requests are limited to 1 MiB per newline-delimited message and a nesting depth
of 64. Source arguments are limited to 250,000 characters; execution plans to
100 steps; run reports to 1,000 samples each. Diagram limits are 200 nodes and
1,000 edges. Responses are bounded at 4 MiB. Oversized, malformed, duplicate-key,
or non-finite JSON is rejected; data is never silently truncated.

Protocol failures use JSON-RPC errors. Known-tool validation/calculation failures
use `isError: true` with a structured error. Exceptions do not echo source or
private argument values. Unknown notifications do not receive responses.

## Specification references

- [Lifecycle and version negotiation](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
- [Stdio framing and output rules](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Tools, structured results and errors](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

The stdio framing, handshake, and tool envelopes follow those versioned
specifications. Protocol tests cover negotiation, notification handling, invalid
requests, bounds, structured/text result agreement, and non-execution of source.
