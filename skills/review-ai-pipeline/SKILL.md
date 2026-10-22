---
name: review-ai-pipeline
description: Review AI application and agent pipeline code for latency, sequential-versus-parallel tradeoffs, resource contention, conditional prompt/schema/reviewer routing, safe caching, retries, and functional regressions. Use for performance code reviews, AI pipeline optimization proposals, PR reviews, or requests to reduce LLM/STT/RAG processing time while preserving behavior. Produce file-and-line evidence, bounded patch proposals, and quality-gated measurements with Applied Opt.
---

# Review AI Pipeline

Perform an evidence-led code review. Use the static scanner to locate candidates;
read implementation and call sites yourself to assess actual behavior. Treat
scanner findings and scheduling estimates as hypotheses until measured.

## 1. Establish scope and contracts

- Resolve the requested repository, commit or diff, entry points, and review-only
  versus implementation scope. Read applicable repository instructions.
- Identify user-visible latency, background work, the request boundary, and
  required outputs. Preserve existing authorization; do not add confirmation
  steps for already authorized, reversible work.
- Inspect models, prompts, reference time, timezone, identity, authorization,
  data revisions, retries, and polling intervals.
- Prefer supplied fixtures and sanitized traces. Keep private code, production
  inputs, credentials, and personal data out of public reports.

## 2. Run the local static review

Invoke the bundled wrapper by its absolute location:

```bash
python3 /absolute/path/review-ai-pipeline/scripts/review.py /absolute/path/project --format json
```

Use `--help` to inspect supported report options. The wrapper locates the
framework relative to this skill or uses an installed package; it does not import
the reviewed project. If unavailable, continue with source inspection and report
the missing static check. Do not install or execute an untrusted project's
dependencies just to review it.

Inspect findings against source. Follow callers, dependency data, SDK
configuration, and failure paths. Dismiss unsupported candidates with a short
reason. Identify semantic issues that pattern rules cannot detect; in practice
the costly defects are semantic (a skipped stage that also produced another
output, a gate evaluated on an aggregate copy), not syntactic.

If an Applied Opt MCP connection is already available, use `review_code` for
supplied snippets, `plan_execution` for declared stage contracts, `compile_flow`
for graph/report previews, and `compare_runs` for saved evidence. Preserve the
returned input/graph revision when associating a diagram with a review. Do not
present an inferred source graph as a complete runtime trace.

## 3. Review execution choices

Read [the review rubric](references/rubric.md). Map stages, dependencies, shared
state, resources, calls, and cancellation boundaries. Compare sequential,
bounded-parallel, and conditional execution where each is feasible.

- Recommend parallelism only after establishing independence, resource capacity,
  exception behavior, and side-effect safety. Account for GPU sharing,
  connection pools, rate/TPM limits, queueing, and tail latency.
- Trace routing through prompt assembly, required output properties, downstream
  reviewers, and fallbacks. Retain all capabilities on uncertainty; preserve
  explicitly requested work and dependency closure.
- For every stage a route can skip, list all outputs it produces and confirm
  each is either unneeded or still produced. Evaluate gates per item.
- When a new selector replaces an older judgment, verify the old call is gone
  and count model round trips on the short path before and after.
- Reject input-length-only reasons to omit tasks or schedules. Review negation,
  dates, timezone, past/future interpretation, and ambiguous intent explicitly.
- Check exact cache identity and revision coverage. Preserve computation when
  scope or key safety is incomplete.
- Locate nested retries, uncancelled siblings, blocking work, duplicate
  generation, and polling delay. Assign one retry owner and a request deadline
  that includes scheduling, retries, and routing. A single owner still honors
  `retry-after` and jitters its backoff.
- When identical inputs give different outputs, look for a prompt rule and a
  code normalizer that disagree before blaming sampling.
- Preserve model, prompt meaning, reasoning settings, and output contracts.
  Treat their changes as separate experiments requiring explicit task scope and
  functional evaluation.

## 4. Propose a bounded change

Attach file/line evidence, confidence, expected mechanism, prerequisites, and
failure modes to each finding. Distinguish a proven bug from a performance
opportunity. State when sequential execution should remain.

Create a diff only within authorized scope. Apply the framework transformation
only to explicitly declared safe calls and supported syntax; review the diff.
A safe-call declaration is an engineer's assertion, not scanner-provided proof.
Do not force unsupported code through an optimizer or change tests merely to
obtain a passing result.

For complex changes, provide a concrete implementation proposal and acceptance
assertions. Do not execute arbitrary reviewed code, evaluation commands, or model
calls without establishing trust and task authorization. Do not post GitHub
comments, reviews, or messages without authorization for that communication.

## 5. Measure and gate

Run baseline and candidate on identical cases with controlled model, prompt,
account context, reference time, timezone, and cache state. Interleave A/B and
B/A runs; declare warmups, repetitions, and representative load. Measure
end-to-end latency and stage timings.

Use `applied_opt.evaluation.evaluate`. When the two variants are revisions of
the same package, give each its own process: a `ProcessRunner` starting the
deployed image with `applied_opt/worker.py` mounted, the candidate source
mounted over the deployed one, and production volumes read-only. Smoke-test one
case once per variant, then run the plan with `max_invocations` set to the
stated count; have runners raise `StopEvaluation` on quota or credential
errors. Guard harness scripts with `if __name__ == "__main__"`.

Before proposing the next optimization, replay the candidate's stage spans with
`planning.work_from_spans` and `sensitivity` (or `applied-opt plan
--sensitivity`) to find which stage reduction would be visible end to end.

Require stable named ground-truth checks on every result. Check exact task and
schedule content when required; equal item counts are insufficient. Block a
faster candidate on missing checks, functional failures, exceptions, cohort
regressions, or incompatible measurement conditions. Separate cold and warm
cache results. Treat small-sample p50/p95 as descriptive, not an SLA guarantee.

Export only appropriate metadata to the dashboard. Label simulations, synthetic
timings, and actual service measurements separately. Read [the reporting
contract](references/report-contract.md) before delivering the review.

## 6. Deliver

Lead with blockers and the recommended execution strategy. Separate verified
findings, measured results, hypotheses, and remaining limits. Include relevant
diffs/report paths and exact tests run. Stop optional testing after identified
risks and required gates are resolved. Claim publication, deployment, or skill
installation only after verifying the corresponding result.
