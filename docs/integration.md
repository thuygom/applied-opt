# Integrating an application

Start with read-only review, then instrument a small application adapter. Keep existing prompts and model settings fixed while testing scheduling changes.

## Review policy

`examples/review_policy.toml` uses this exact structure:

```toml
[parallel]
safe_calls = ["fetch_context", "detect_typos"]
max_parallel_per_batch = 2

[review]
exclude = ["generated/*", "vendor/*"]
max_file_bytes = 1000000
```

The transformer currently emits two-call batches. The policy documents the safe-call contract; a function name alone does not prove safety. Review confidence describes a static finding's evidence, not a calibrated correctness probability.

## Declare a small DAG

```python
from applied_opt.runtime import Pipeline, RunContext, Step

async def context(ctx, deps):
    return await application.fetch_context(ctx.payload["subject_id"])

async def classify(ctx, deps):
    return await application.classify(ctx.payload["text"])

async def generate(ctx, deps):
    return await application.generate(
        ctx.payload["text"], deps["context"], deps["classify"]
    )

pipeline = Pipeline([
    Step("context", context, concurrency_safe=True, resource="db"),
    Step("classify", classify, concurrency_safe=True, resource="llm"),
    Step("generate", generate, depends_on=("context", "classify"),
         concurrency_safe=True, resource="llm"),
], max_concurrency=2, resource_limits={"llm": 1})

# Inside an async application function:
result = await pipeline.run(RunContext(payload), timeout_s=15)
metadata = result.trace_dict()
```

`application` and `payload` are your existing application objects. Mark safe concurrency only after examining shared clients, mutations, and side effects. Resource limits above apply within this run; use a service-wide admission controller for multiple requests or workers. A 15-second deadline is a timeout budget, not a latency guarantee.

For routing, define `Capability` objects, call `Router.route(payload, decider, evidence=...)`, and pass the resulting `CompiledRoute` into `RunContext`. Use the compiled prompt/schema in your model adapter. The callback can wrap a local classifier or decision service; no vendor-specific Decisions API is bundled. The demo callback uses fixtures and is not a production NLP classifier.

## Plan from observed durations

Save this as `work.json`, then run `applied-opt plan work.json --output plan.json`:

```json
{
  "steps": [
    {"name": "context", "duration_ms": 25, "concurrency_safe": true},
    {"name": "classify", "duration_ms": 40, "resource": "llm", "concurrency_safe": true},
    {"name": "generate", "duration_ms": 100, "depends_on": ["context", "classify"], "resource": "llm", "concurrency_safe": true}
  ],
  "max_concurrency": 4,
  "resource_limits": {"llm": 1},
  "launch_overhead_ms": 1,
  "min_improvement": 0.05
}
```

## Compare saved runs

Both `baseline.json` and `candidate.json` use:

```json
{"runs":[{"case_id":"explicit-meeting","latency_ms":120.5,"checks":{"schedule_datetime_preserved":true,"tasks_preserved":true}}]}
```

Include one record per measured repetition, with matching case IDs, sample counts, and named boolean checks. Missing checks, mismatched cases, execution failures, failed assertions, and excessive per-case p95 regression fail the gate. Define assertions against expected behavior; do not generate the check list from fields that happen to be present in an output.

For live adapters, use `evaluation.Case`, `Observation`, and `evaluate()`. Keep input and output content outside shared reports. Explicitly control cold/warm caches; use held-out cases and representative traffic before rollout.
