# AI pipeline review rubric

## Sequential versus parallel

Inspect dependency and execution behavior, not merely adjacent `await` syntax.

| Question | Evidence | Consequence |
| --- | --- | --- |
| Does B use A's result? | Arguments, closures, shared state, database writes | Preserve the dependency. |
| Do calls share a resource? | GPU/model instance, session, pool, worker queue | Bound concurrency per resource; measure contention. |
| Is failure behavior preserved? | Exceptions, cancellation, compensation, partial output | Drain siblings and preserve required errors. |
| Can mutations race? | Aliased objects, in-place normalization, singletons | Isolate state or retain serial execution. |
| Do parallel calls increase rejection? | Rate/TPM limits, burst size, SDK retries, queues | Compare errors and p95 under load. |
| Is there critical-path benefit? | Stage durations and dependency DAG | Avoid overhead or contention exceeding the benefit. |

Treat summed stage time as work and the longest dependency path as a latency
lower bound. Include queues, routing, delivery, and polling in the user-visible
boundary. Do not subtract overlapping stages to manufacture a speedup.

For decision latency `d`, branch latency `b`, and probability `p` that a branch
is needed, use these only as scenario estimates:

- Gated latency: `d + p*b`; branch cost: `p*branch_cost`.
- Speculative latency: `p*max(d,b) + (1-p)*d`; branch cost: full `branch_cost`.

Require independent startup, no speculative side effects, and validated routing.
Assume provider work may remain billable after cancellation. Prefer gating when
avoiding cost or contention matters more than speculative latency savings.

## Routing and prompt assembly

Trace one decision through the complete pipeline:

1. Determine available and mandatory capabilities.
2. Require explicit consideration of every optional capability.
3. Preserve evidence-backed capabilities and dependency closure.
4. Assemble matching prompt fragments and output properties.
5. Run reviewers for the same enabled capabilities.
6. Fall back on timeout, malformed output, unknown/incomplete selections, or
   uncertainty.

Do not infer task absence solely from empty extraction; check whether the prompt
or schema already disabled extraction. Prevent later stages from silently
re-enabling every optional analysis. Never omit a dependency of enabled work.

Before gating a stage, list every output it produces, not only its main one. A
final review that also proposes follow-ups or normalizes dates must run whenever
any of those outputs may be needed; ask for that need in the same decision call
instead of a second round trip. Gate post-processing on every item, not on an
aggregate copy such as a top-level field mirroring the first of several notes.
A stage that already returns early without a model call for empty input saves
nothing when gated, but gating it can drop response fields.

When a new selector replaces an existing judgment, remove the old one and count
model round trips on the short path before and after; stacking both is slower.
Keep the deployed stage order as a floor: a routing rewrite must not quietly
serialize stages the deployed version overlapped. Treat order changes as their
own measured experiments.

Use a provider-neutral callback unless a chosen provider's interface is verified
from installed code or official documentation. Do not invent API endpoints or
assume that model-reported confidence is calibrated.

Test short explicit actions/dates, negation, cancellations, past/future dates,
timezone boundaries, ambiguity, and plain observations. Hold reference-time
rules constant across baseline and candidate.

## Caching

Require exact identity covering operation/version, arguments, dependencies,
tenant, subject, authorization, data/context, model, prompt, route, and relevant
reference time. Isolate applications/pipelines with explicit namespaces.

- Cache only explicitly deterministic, read-only operations.
- Define TTL, bounds, invalidation, and missing-scope fallback.
- Return independent values so callers cannot mutate later cached results.
- Separate provider prompt caching from application result caching.
- Treat semantic caching as a behavioral change needing independent validation.
- Exclude partial, failed, or authorization-incompatible results.

Compare cold and warm measurements separately. Do not attribute a warm-candidate
versus cold-baseline difference entirely to a code change.

## Retries and cancellation

Count SDK, application, proxy, and format-repair attempts together. Budget their
combined attempts/backoffs. Require idempotency for retried operations and
explicit contracts for externally visible actions.

Cancel and await sibling tasks on failure. Account for executor threads and
provider work that may survive an async timeout. Do not claim hard deadlines
when underlying code blocks or suppresses cancellation.

When the application takes over retries from an SDK, keep what the SDK did for
it: honor `retry-after` (capped) and add jitter so overlapped calls do not retry
in lockstep. An explicit rejection of an optional request parameter, such as a
service tier, should be remembered for the process instead of costing one extra
round trip per call. Record the tier actually served in the usage ledger, since
tiers are priced differently.

## Determinism and conflicting rules

When identical inputs flip between runs, look for two owners of one rule: a
prompt instruction and a deterministic normalizer that disagree, or that cover
different conditions (for example a prompt asking for the previous day while
the normalizer skips inputs with a clock time). Give the rule one owner and make
the other consistent. Confirm the product rule with its owner; do not invent it.

## Functional gates

Require stable named assertions on every sample; check values beyond shape and
item count.

| Capability | Useful assertions |
| --- | --- |
| Summary | Required facts retained; no invented claims; negation preserved. |
| Tasks | Actor, action, deadline, and cancellation preserved. |
| Schedules | Exact date/time/timezone, participants, and past/future rules. |
| RAG | Required evidence and authorization retained. |
| Text-to-SQL | Permitted schema, row scope, and result meaning preserved. |
| Speech | Segment coverage and required speaker attribution preserved. |

Investigate broken baselines without weakening checks. Separate quality and
latency failures. Record held-out cases and sample size; avoid general claims of
functional equivalence from a small synthetic suite.

Read existing tests against the contract before trusting a green suite: a test
that asserts current behavior (for example that a classifier is never called
when a list is empty) can pin the defect under review.

## Measuring live services

- Compare two revisions of one package in separate processes: run the deployed
  artifact as the baseline and mount the candidate source into the same image
  (`ProcessRunner` with `applied_opt/worker.py`). Mount production volumes
  read-only and redirect side-effect files, such as audit logs, to scratch paths.
- Before paid runs, state the planned invocations
  (cases × (warmups + repetitions) × 2), pass `max_invocations`, and run one case
  once per variant first. Raise `StopEvaluation` on quota, credential, or
  environment errors.
- Guard harness entry points with `if __name__ == "__main__"`; importing a
  harness to reuse a helper must never start a paid run.
- Before optimizing a stage, replay a measured trace (`work_from_spans`,
  `sensitivity`, or `applied-opt plan --sensitivity`). A stage hidden behind
  parallel work shows no visible saving, so shortening it changes nothing.
