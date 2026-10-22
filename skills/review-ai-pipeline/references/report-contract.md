# Review report contract

## Findings

Provide repository-relative file/lines, observed evidence, impact, confidence,
concrete recommendation, prerequisites, and acceptance tests for each actionable
finding. Omit private input text and secret values. Reserve high confidence for
directly established behavior. Group duplicate causes; lead with correctness
failures, followed by material performance findings. Avoid unsupported claims.

## Evidence labels

| Label | Required support |
| --- | --- |
| Verified code behavior | Relevant implementation and call path inspected. |
| Measured result | Executed baseline/candidate; boundary, cases, and run count. |
| Synthetic demonstration | Controlled fake workload; no service SLA claim. |
| Estimate | Declared dependency, duration, capacity, and contention assumptions. |
| Hypothesis | Plausible mechanism and a concrete verification step. |

Do not present estimates as benchmarks or test counts as proof that model output
is universally unchanged.

## Measurements

Report baseline/candidate p50/p95, repetitions, errors, required-check outcomes,
and known call/cost differences. Include input cohorts so long inputs cannot
hide short-input regressions. State warmups, load/concurrency, model/prompt,
reference time, and cache state. Mark unknown usage or billing as unknown.

State whether providers are real, mocked, or synthetic sleeps. Give the gate
outcome and concrete reasons. Fail acceptance on missing/failed checks.

## Scope

Record the reviewed revision/files, existing defects versus new regressions,
tests not executed, and material coverage limits. Keep findings in the requested
report; posting PR comments or sending messages requires user authorization.
