# Changelog

## 0.2.0 — 2026-10-22

Lessons from applying the framework to a production note-generation pipeline.

- Review: AO008 no longer flags a `gather` whose enclosing `try` cleanup cancels every gathered task; AO004 no longer flags fixed-size literal batches, semaphore-gated wrappers, or drains inside `finally`. Partial cleanup, inline coroutines, and grown lists still trigger.
- Evaluation: `ProcessRunner` and the stdlib-only `applied_opt/worker.py` compare two revisions of one package in separate processes (for example, a deployed image with the candidate source mounted).
- Evaluation: `evaluate(max_invocations=...)` refuses plans above a paid-call budget; `StopEvaluation` ends both variants early and fails the gate with `evaluation_stopped`. Reports include planned and made invocations.
- Planning: `work_from_spans`, `what_if`, and `sensitivity` replay measured stage traces to show which stage reduction would be visible end to end; `applied-opt plan --sensitivity FRACTION` adds the table to a declared plan.
- Skill and rubric: inventory every output of a skippable stage, gate per item, keep the deployed order as a floor, count round trips when a selector replaces a judgment, keep `retry-after` and jitter when taking over SDK retries, remember explicit tier rejections and ledger the served tier, and check prompt/normalizer conflicts when outputs flip between runs. Measurement guidance covers read-only production mounts, paid-run budgets, and guarded harness entry points.

## 0.1.0 — 2026-10-22

- Initial Python AST review rules and line-addressable reports.
- Explicit-contract parallel patch proposals with cancellation cleanup.
- Dependency/resource scheduling estimates and conditional-branch trade-offs.
- Conservative routing, bounded DAG execution, retry budgets, and opt-in scoped caching.
- Paired evaluation, required assertions, and per-case latency gates.
- Reporting dashboard, local web server, synthetic demonstrations, and agent skill source.
- Common graph compiler for inferred source, declared plans, and measured traces, with Mermaid, PlantUML, SVG, and JSON exports.
- Local stdio MCP server with four read-only tools and input/graph snapshot revisions.
- Import-shadowing checks for library-specific async review rules and escaped, bounded diagram exports.
- Same-origin hosted dashboard and Python API with a Gunicorn entry point, explicit public-origin checks, and required token authentication.
- Repository-owned Docker hosting, local bootstrap, standalone Caddy HTTPS VPS stack, and Render Docker Blueprint.

Alpha scope: Python code, narrow source transformations, application-supplied quality checks, and a documented MCP protocol subset. Diagram languages are export formats, not general parsers. Hosting assets support a shared-token web service; they do not provide tenant isolation or provision an external hosting account. No universal optimization or production SLA guarantee.
