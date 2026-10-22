# Architecture

Applied Opt starts with a review finding and ends with a measurable acceptance decision. Its layers can be used independently.

| Layer | Input → output | Boundary |
| --- | --- | --- |
| `review` | Python source → evidence-backed findings | No imports or execution; heuristics can miss issues and produce false positives. |
| `transform` | Source + safe-call allowlist → diff | Only narrow pairs of independent awaits; no model/prompt rewrites. |
| `planning` | Durations + dependencies + capacities → scenarios | Estimates, not measured p95 or a queueing model. |
| `routing` | Payload + decision callback → `CompiledRoute` | One capability selection governs prompt, schema, and reviewer names. |
| `runtime` | Declared steps + context → outputs and metadata spans | Unknown concurrency is serialized; resource limits are per pipeline run. |
| `evaluation` | Two application adapters + labeled cases → gate | Only supplied assertions establish acceptance. |
| `server` / dashboard | Report JSON → review and timing views | No remote-code executor. |

## One route contract

Each `Capability` owns its prompt fragment, output property schema, required capabilities, and reviewer names. `Router.route()` accepts an async callback returning `Decision(selected, confidence, considered)`. The callback must explicitly consider every registered capability. Missing, malformed, unknown, timed-out, or low-confidence decisions retain all capabilities. Mandatory, forced, and evidence-backed capabilities cannot be omitted.

Adapters must actually use `route.prompt`, `route.response_schema`, and the selected reviewers. Returning a route object alone does not optimize an existing application. The runtime rejects an enabled step that depends on an omitted capability.

## Execution and data

`Step.concurrency_safe` is false by default. Side-effectful or undeclared steps execute exclusively within a run. Declared-safe steps become ready as dependencies finish, subject to concurrency and named resource limits. Cancellation is cooperative; blocking code must be handled by the application.

Retries default to one attempt. Additional attempts require declared idempotency and stay inside an overall request deadline. SDK retry settings remain the adapter's responsibility.

Caching requires explicit deterministic/read-only declarations and complete scope/revision fields. Exact keys include payload, dependency values, route revision, step version, and pipeline namespace. The in-memory cache is bounded, copies values, and supports tenant-scoped invalidation. It is not a distributed cache or a single-flight implementation.

Trace exports omit payloads, generated text, and exception messages. The reported dependency critical path excludes queueing and resource serialization, so it is a diagnostic lower bound rather than request latency.
