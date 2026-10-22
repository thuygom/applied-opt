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

## Compare two revisions in separate processes

Two revisions of one application usually share module names, so they cannot be imported into one interpreter. Run each variant in its own process and let `evaluate()` call both. `applied_opt/worker.py` uses only the standard library and no relative imports, so you can mount it into the deployed image without installing Applied Opt there.

```python
# handler.py, mounted next to worker.py in both containers
async def handle(payload):
    response = await application.analyze(payload)          # the variant's own code
    return {"output": project(response), "spans": stage_spans(response)}
```

```python
from applied_opt.evaluation import Case, StopEvaluation, evaluate
from applied_opt.process import ProcessRunner

image = "registry.example/app:deployed"
common = ["docker", "run", "--rm", "-i", "--volumes-from", "app:ro",
          "-v", "/opt/applied-opt/src/applied_opt/worker.py:/eval/worker.py:ro",
          "-v", "/opt/eval/handler.py:/eval/handler.py:ro", "-w", "/eval", "--entrypoint", "python"]
baseline = ProcessRunner(common + [image, "worker.py", "handler:handle"], stderr_log="baseline.err")
candidate = ProcessRunner(common + ["-v", "/src/candidate:/candidate:ro", "-e", "PYTHONPATH=/candidate",
                                    image, "worker.py", "handler:handle"], stderr_log="candidate.err")

async with baseline, candidate:
    report = await evaluate(baseline, candidate, cases, judge, repetitions=3, warmups=1,
                            max_invocations=len(cases) * 4 * 2)
```

The baseline is the deployed artifact itself; the candidate differs only by its mounted source. Mount production volumes read-only and point side-effect files (audit logs, local caches) at scratch paths through environment variables. Application logs may share stdout; only marker-prefixed lines are protocol messages. A handler exception reaches the report as its type only.

## Budget paid runs

`max_invocations` refuses to start when `cases × (warmups + repetitions) × 2` exceeds it, so a typo in a case list cannot multiply provider spend. A runner that raises `StopEvaluation` (for example on an exhausted quota or rejected credentials) ends the evaluation; the report keeps the runs already made, records `benchmark.stopped` and `benchmark.invocations`, and fails the gate with `evaluation_stopped`. Smoke-test one case once per variant before the full plan, and guard harness scripts with `if __name__ == "__main__":` so importing a helper never starts a run.

## Find the visible bottleneck from a trace

Summed stage time is work; only the critical path is latency. Replay measured spans to see which reduction would show up end to end:

```python
from applied_opt.planning import sensitivity, what_if, work_from_spans

work = work_from_spans(candidate_spans, {"generate": ["select", "correct"],
                                         "review": ["generate", "classify", "correct"]})
print(sensitivity(work, fraction=0.5)["stages"])      # visible saving per stage
print(what_if(work, drop_dependencies=[("generate", "correct")]))  # overlap a stage instead
```

A stage that overlaps longer work shows a visible saving near zero; making it faster changes nothing for the user. `applied-opt plan work.json --sensitivity 0.5` prints the same table for a declared plan. Model every join: if post-processing waits for a stage, keep that edge when you drop another.
