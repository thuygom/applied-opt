# Sequential versus parallel

Optimize the user-visible critical path under quality and resource constraints. “More concurrent” is not an objective by itself.

| Situation | Starting choice | Evidence to collect |
| --- | --- | --- |
| Independent network reads, spare provider capacity | Bounded parallel | End-to-end p50/p95, rate limits, connection wait, error rate. |
| Second stage requires the first output | Sequential | Dependency contract; opportunities inside each stage. |
| Two jobs share one GPU slot | Sequential on that resource | GPU memory, queue wait, throughput, competing requests. |
| Writes or shared mutable state | Sequential unless redesigned | Ordering, idempotency, transactional behavior, cancellation. |
| Optional expensive branch | Decide, then conditionally execute | Decision overhead, branch frequency, false-negative cost. |
| Independent speculative branch | Consider only with a budget | Wasted calls, billing after cancellation, failure cleanup. |

For independent stages of durations `a` and `b`, an ideal overlap changes `a + b` to approximately `max(a, b)`, plus orchestration overhead. This assumes spare capacity and unchanged service times. Shared provider queues can invalidate both assumptions. Parallelism does not reduce the number of required calls or generated tokens.

## A conditional branch

Let decision latency be `d`, branch latency `b`, and the probability of needing the branch `p`. A gated execution has expected latency `d + p*b`. Starting both together has expected latency `p*max(d,b) + (1-p)*d`, under the planner's stated assumptions.

The speculative branch is assumed fully billed even when unnecessary. This trades possible latency savings for more expected work; it does not establish that the routing decision is correct. Never speculate external side effects.

`planning.conditional_tradeoff()` exposes those assumptions. `compare_strategies()` compares concurrency levels, dependencies, capacities, launch overhead, and a supplied contention multiplier. Its multiplier is a scenario input; the planner does not infer live contention or predict production p95.

## Acceptance protocol

Fix model/prompt revisions, authorization/data snapshots, reference dates, cache state, input cohorts, and load. Alternate baseline/candidate order, include warmups, and measure enough repetitions for the decision being made. Short inputs, explicit appointments, negation, cancellation, ambiguous dates, long inputs, and failure paths need separate coverage.

Check required fields against labeled expectations, not merely against the previous model output. Count equality alone cannot detect a changed appointment time. A baseline failing its own assertions is not a valid quality reference. Inspect per-cohort p95 so aggregate improvements cannot hide regressions.

Cancellation details matter: Python's [`asyncio.gather`](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.gather) does not automatically cancel other awaitables when one raises. `gather_strict` cancels and drains siblings while retaining the propagated exception type. Completion-order failures can still differ from sequential execution. Review caller expectations before applying a patch.
