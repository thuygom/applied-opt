# Independent skill trial: AI pipeline review

## Decision

Preserve the `fetch_context → generate_note` dependency and evaluate overlapping that branch with `detect_typos`, with at most two active stage calls per request. Retain the typo result. Fix the blocking sleep, cache value aliasing, and batch concurrency/lifecycle contract before treating this fixture as a reusable application pattern.

The code supports these proposals, but this review establishes **no measured speedup and no functional-equivalence pass**. The reviewed file was neither imported nor executed. All timing figures below are estimates derived from explicit synthetic sleep durations.

## Scope and evidence

- Reviewed file: `examples/review_target.py`, all 58 lines, relative to `/workspace/scratch/bcc9e0e4ef37/applied-opt`.
- Source SHA-256: `cc94e30a3f9446367776cb093ab60097ec4326aa4ec2f93c823ae7d3adaf99fe`.
- Skill SHA-256: `6941d5d85680b19ac68f96189288f61483d55dad07028a57bdc5d213775a4b61`.
- No Git revision was available: `git rev-parse HEAD` reported that the checkout is not a Git repository. No applicable `AGENTS.md` was found in the inspected file's ancestor directories.
- Read `skills/review-ai-pipeline/SKILL.md`, `references/rubric.md`, and `references/report-contract.md`; inspected the wrapper, relevant scanner implementation, and repository references to the reviewed functions.
- The fixture explicitly identifies itself as synthetic (`examples/review_target.py:1–5`). There are no model clients, prompts, task/schedule extraction, production subjects, authorization rules, retries, polling, or application request handlers in this file. The function-entry-to-return boundary is the only available latency boundary; real user-visible latency is unknown.
- The bundled static review completed successfully: 1 file, 0 scan errors, 8 findings, 4 warnings, 4 informational findings. No Applied Opt MCP tools were available in the current registry.

## Top actionable findings

### 1. Cached results expose shared mutable state

**Evidence:** `examples/review_target.py:48–58` stores `result` directly in `_cache`, returns that same object on a miss, and returns the cached object on a hit. `generate_note` creates a mutable dictionary (`:21–23`). No copy or immutable representation intervenes.

**Label and confidence:** Verified code behavior; high confidence in aliasing. Actual corruption by a caller has not been observed because callers were not supplied or executed. Whether shared identity is intended is an unresolved API-contract question.

**Impact:** A caller that changes the returned `summary` or `subject` changes what a later caller receives for the same text. The unbounded module-global dictionary also retains every distinct input/result pair for the process lifetime (`:48`, `:57`). Concurrent misses can both pass `:52–54`, suspend at `:56`, and perform duplicate generation before either stores a result.

**Bounded recommendation:** Store an independent value and return an independent value on both miss and hit paths. For the current two-field dictionary with string values, shallow copies on insertion and return are sufficient; revisit this if nested mutable outputs are introduced. Specify a maximum cache size and an eviction policy. Introduce per-key in-flight deduplication only if duplicate work is material; define who owns cancellation, ensure failed in-flight entries are removed, and avoid making one cancelled waiter cancel work needed by other waiters.

**Optimization conditions and uncertainty:** The visible operation is deterministic and read-only, with the subject fixed to `"synthetic"` at `:56`. Consequently, the text key is sufficient for the visible value computation apart from mutation/retention issues. There is no evidence of a present cross-tenant leak or stale model output. If real contextual generation replaces the stub, require an explicit namespace and all actual dependencies in the key: subject/tenant and authorization scope, data revisions, model/prompt/route version, and relevant reference time. Disable reuse when required scope is unavailable. TTL should reflect actual freshness requirements; an arbitrary TTL is not a fix for incomplete identity.

**Acceptance checks:** `cache_value_isolation` mutates both a miss result and a hit result, then verifies later results retain the exact original values. `cache_capacity` inserts more than the declared capacity and verifies bounded retained entries and valid recomputation. If deduplication is implemented, `same_key_singleflight` verifies one underlying call for concurrent equivalent requests, while `different_keys_independent` verifies unrelated keys are not serialized. Failure and waiter-cancellation cases must leave no poisoned cache entry. If scope is added, `cache_scope_separation` must use identical text with distinct subjects, authorization revisions, and model/prompt versions and assert no cross-scope reuse.

### 2. The batch eagerly creates work for every item and has no sibling-cleanup contract

**Evidence:** `examples/review_target.py:44–45` expands all `subject_ids` into `asyncio.gather` arguments. There is no input-size limit, worker bound, semaphore, or cleanup handler in the function. The only current child implementation sleeps and returns a dictionary (`:11–13`).

**Label and confidence:** Verified unbounded fan-out in the implementation; high confidence. Operational overload and leftover work following a child exception are conditional risks. The current synthetic child has no normal provider-error path, and no workload sizes or external callers are supplied.

**Impact:** Task creation and in-flight calls scale with batch size. A semaphore around each call would bound active work but still create one task per input. Default `gather` propagates a child exception without automatically cancelling its remaining siblings, so replacing the stub with fallible I/O could leave request work running after failure. Cancellation of the aggregate itself is a different case and must be tested separately.

**Bounded recommendation:** Use a fixed worker pool with a bounded queue, or bounded windows if their barrier overhead is acceptable. Preserve input order and duplicates by indexing each input/result. Bound the number of created tasks as well as active calls; the returned list still necessarily uses memory proportional to output size. On failure or external cancellation, cancel and await workers before completing the request, and specify whether the API fails as a whole or returns partial results. Preserve the existing exception contract unless a change is explicitly accepted; `TaskGroup` can expose an `ExceptionGroup` where the current function exposes an individual exception.

**Optimization conditions:** Choose the bound from measured capacity and resource limits. The synthetic sleeping implementation demonstrates no GPU, connection-pool, or provider pressure, so it cannot justify a production concurrency value. Prefer concurrency 1 if a shared non-reentrant client, exclusive GPU, mutable state, or provider budget requires serialization. Apply a shared capacity limiter across requests if per-request bounds still overload a common resource.

**Acceptance checks:** `batch_order_and_duplicates`, `batch_empty`, `max_active_calls`, and `max_worker_tasks`; injected single and simultaneous child errors; caller cancellation; and `no_pending_children_on_exit`. Check the outward exception type, partial-result policy, and cleanup completion. Sweep concurrency 1, 2, 4, and 8 only within declared resource limits, measuring queueing, active calls, p95, errors, and memory. Require actual provider evidence before making rate-limit or billing claims.

### 3. A synchronous sleep blocks the event loop

**Evidence:** `examples/review_target.py:39–41` is an async function containing `time.sleep(0.01)` without a suspension point.

**Label and confidence:** Verified code behavior; high confidence. Interference with another request is conditional on concurrent use of the same event loop.

**Impact:** When awaited, the coroutine occupies the event-loop thread during its sleep, delaying other runnable tasks. The literal 10 ms is an intended sleep duration, not a measured scheduling stall or guaranteed upper bound.

**Bounded recommendation:** Replace this synthetic timer with `await asyncio.sleep(0.01)`. A thread pool is unnecessary for a timer. If the sleep represents real blocking work, choose its async API first, or separately evaluate a bounded executor with thread-safety and cancellation limitations.

**Acceptance checks:** `blocking_return_value` must preserve `"synthetic"`; `event_loop_progress` runs a peer coroutine and verifies it can advance during the wait. Under representative concurrent load, compare event-loop lag and unrelated-request tail latency. Do not require a standalone-call latency reduction: an async sleep improves scheduling while retaining the nominal delay. Cancellation becomes observable at the new await, so include `blocking_wait_cancellation` and accept that contract deliberately.

### 4. The typo check unnecessarily extends the critical path

**Evidence:** `examples/review_target.py:26–30` awaits context, then typos, then note generation. The typo function consumes only `text` and returns a fresh list (`:16–18`); it neither produces nor mutates the context. Generation consumes the context from `fetch_context` (`:21–23`, `:27`, `:29`) and does not consume `typos`. Both results are required by the returned object (`:30`).

**Label and confidence:** Verified independence of the visible synthetic data flow; high confidence. Latency improvement is an estimate, and independence/capacity must be reassessed for real implementations.

**Bounded recommendation:** Run two branches: (a) fetch context, then generate the note; (b) detect typos. Join their results into the unchanged return object. Keep no more than two stage calls active per request, and cancel/drain the other branch on failure or cancellation. An initial patch that overlaps only context and typos is smaller and reaches the same estimate for these particular durations. The two-branch structure avoids a needless typo barrier before generation when real typo latency exceeds context latency.

**Prerequisites and failure modes:** The current strings are immutable, the context is local to its generation branch, and these stubs have no external side effects. Real service replacements need review of shared sessions, model/GPU capacity, rate/TPM limits, side effects, and timeouts. Parallel start can cause work to begin that the sequential implementation would never start after an earlier error. Simultaneous failures can change which error surfaces first. Explicitly choose and test error precedence rather than claiming that result equality alone preserves behavior. A request deadline must include capacity waiting and cleanup; cancelling a coroutine does not prove that remote work or billing stops.

**Acceptance checks:** `pipeline_exact_result` checks both full output values, including `note.summary == text`, `note.subject == subject_id`, and `typos == []` for this fixture. `context_before_generation` asserts generation begins only after context completes. `typo_branch_independence` exercises durations in both relative orders. Failure/cancellation checks assert the agreed error surface, no returned partial object, no unawaited task, and no pending sibling after completion. Hold models, prompts, reference time, output schema, and reasoning settings fixed if real services are introduced.

## Execution comparison

Let C be context duration, T typo duration, and G generation duration. The explicit sleeps are C = 60 ms, T = 40 ms, G = 30 ms (`examples/review_target.py:12`, `:17`, `:22`). These values exclude scheduling overhead, contention, queues, and all real service variability.

| Strategy | Dependency-preserving estimate | Fixture estimate | Decision |
| --- | --- | ---: | --- |
| Current serial pipeline | C + T + G | 130 ms | Baseline |
| Context and typos together, then generation | max(C, T) + G | 90 ms | Small bounded candidate |
| Context first, then typos and generation together | C + max(T, G) | 100 ms | Feasible but leaves more delay for these values |
| Context → generation branch alongside typos | max(C + G, T) | 90 ms | Preferred general dependency layout |
| Start generation before context is ready | Invalid dependency | Not applicable | Reject |

The 130 → 90 ms comparison estimates a 40 ms reduction, approximately 31%, or 1.44× speedup, under the declared synthetic assumptions. It is **not a benchmark**. Total stage work remains 130 ms, and the pipeline still makes three stage calls. No provider cost saving is established. Under contention, bounded parallel execution can be slower or less reliable.

Keep `dependent_pipeline` sequential: `examples/review_target.py:34–35` passes the actual context result into generation, with no independent branch to overlap. Its idealized 90 ms chain has no overlap opportunity demonstrated in this source.

## Scanner disposition and semantic limits

| Scanner candidate | Disposition after source inspection |
| --- | --- |
| AO001 at lines 27–28 and 28–29 | Merge into finding 4. Both adjacent pairs are candidates, but one coherent dependency layout should replace piecemeal transformations. |
| AO002 at lines 34–35 | Accept: preserve the context-to-generation dependency. |
| AO003 at line 40 | Accept: finding 3. |
| AO004 and AO008 at line 45 | Accept the fan-out/lifecycle concerns; group into finding 2 and distinguish current behavior from future fallible-provider risks. |
| AO006 at line 28 | Do not omit the typo capability. The return contract includes `typos`, and the empty synthetic implementation proves no product-level optionality. There is no validated router or schema/reviewer chain to inspect. |
| AO007 at lines 51–58 | Conditional contextual-cache warning, not evidence of a present tenant leak. Finding 1 adds the directly visible aliasing, capacity, and concurrent-miss issues. |

No input-length-based routing is justified. If conditional routing is proposed later, it requires a complete prompt/schema/reviewer/fallback contract, preserving explicitly requested capabilities and all dependencies. Date, timezone, negation, short explicit requests, and task/schedule content checks are not executable against this fixture because those capabilities are absent. They should become required when those capabilities enter scope, rather than being presented as tests passed here.

## Concrete before/after verification plan

This is a proposed plan for a separately authorized implementation/evaluation; it was not executed during this review.

1. **Freeze cases and contracts.** Use an immutable source snapshot, the same Python/runtime environment, and named checks listed above. Include empty, short, long, Unicode, repeated-text, and distinct-subject cases. Add batch sizes 0, 1, 2, and a stress size chosen for the declared memory/capacity budget. Record the required error surface and cache ownership semantics before comparing variants. Existing baseline defects must remain visible; evaluate intended fixes against ground truth rather than copying defective baseline behavior into the expected output.
2. **Separate changes.** Evaluate the blocking fix, cache fix, batch bound, and pipeline overlap independently before combining them. This makes regressions attributable. Use instrumented fake services for deterministic ordering, error, cancellation, mutation, and capacity checks. A future execution of the supplied sleeps is a synthetic demonstration, not a live-service benchmark.
3. **Control runs.** Measure function entry through final return/raise, including queueing and cleanup. Record stage start/end times, queue wait, event-loop lag, peak active calls/tasks, calls per request, errors, and checks on every result. For synthetic timing, use 5 warmups per variant and 100 measured paired repetitions per cohort; alternate A/B and B/A order. Compare request concurrency 1 and 8, and label those as chosen laboratory loads rather than production-representative without workload evidence.
4. **Separate cache conditions.** Cold runs start from independently reset caches; warm runs prime both variants equivalently. Add simultaneous misses as a separate cohort. Do not compare a warm candidate with a cold baseline. Count cache hits/misses and actual underlying calls. For real-service evaluation, also fix model/prompt/reasoning configuration, account and authorization context, data revisions, timezone, reference time, and routing; record usage/cost when available and mark billing unknown otherwise.
5. **Predeclare gates.** All named checks must be present and pass for every applicable result. Any functional failure, unhandled exception, changed required error contract, unfinished sibling, or exceeded declared resource bound fails acceptance. For a proposed timing gate, require at least 10% lower p50 in the primary pipeline cohort and no more than 5% worse p95 in any cohort, with zero new errors; agree different tolerances before collecting data if the workload requires them. Report both cohort and aggregate results. Small-sample percentiles remain descriptive; obtain enough representative-load evidence before making an SLA claim.
6. **Report the outcome.** Publish baseline/candidate p50 and p95, case counts and repetitions, warmups, concurrency, cache state, errors, each named check's outcome, call/cost differences, and the exact measurement boundary. Reject a faster candidate with missing checks or a regressed cohort. Investigate a broken baseline without weakening its ground-truth checks.

### Current measurement status

| Field | Baseline | Candidate |
| --- | --- | --- |
| p50 / p95 | Not measured | Not measured |
| Runtime repetitions | 0 | 0 |
| Functional checks executed | 0 | 0 |
| Runtime/provider errors | Unknown; no execution | Unknown; no execution |
| Provider calls / billing | None made in this review | None made in this review |
| Acceptance gate | Not run | Not run; no patch applied |

## Checks actually performed

- Read the skill, both referenced review documents, all reviewed source, and the wrapper. Searched repository references for the function names; found examples, scanner heuristics, and separate transformation tests, but no additional application caller establishing a production contract for this file.
- Ran the bundled read-only static wrapper with the absolute target and `--format json`, without a policy override or optimizer invocation. It exited 0 and emitted 8 findings. The scanner reads source and parses/compiles an AST without evaluating the code object (`src/applied_opt/review.py:115–129`, `:390–393`).
- To satisfy the trial's strict no-project-writes constraint, invoked the wrapper through `runpy.run_path` with an in-memory `subprocess.run` shim that adds Python's `-B` flag to child Python commands. This changes bytecode-file behavior only; the review target and scanner arguments were unchanged. The effective child command used `python3 -B -I -c <the wrapper's bootstrap> <checkout>/src review <absolute-target> --format json --fail-on none`.
- Recorded the source and skill hashes. No source patch, optimizer transformation, runtime test, evaluation model call, external communication, or deployment was performed. The only authored artifact is this report.

## Skill usability findings

**Overall:** The skill was usable without extra clarification. Its strongest instructions were to inspect call implementations, separate static candidates from benchmarks, preserve dependencies and output capabilities, and classify cache concerns by actual evidence. Those instructions prevented an unconditional typo-skip recommendation and an unsupported claim of a cross-tenant cache defect.

**Nonblocking issue — strict read-only execution needs a child bytecode flag.** `skills/review-ai-pipeline/scripts/review.py:24`, `:26`, and `:31` start Python children without `-B`. A parent invocation with `python3 -B` does not propagate that interpreter flag, and `-I` ignores `PYTHON*` environment configuration. Importing the framework can therefore create/update its `__pycache__` files in an otherwise source-read-only review. This was a potential write side effect identified from the wrapper, not a write observed in the trial, because the shim prevented it. Add `-B` to the generated child/probe arguments, or explicitly narrow the wrapper's read-only promise to reviewed source. A first-class option is preferable to requiring a user-side shim.

**Minor report ergonomics:** A single-file scan reports `review_target.py` rather than the repository-relative `examples/review_target.py`. The reporting contract requires repository-relative evidence, so the reviewer must restore that prefix. A documented `--repo-root` or explicit displayed scan root would make this less error-prone when different directories contain the same filename.

**No other blocking issue found.** Required references resolved, the wrapper located its framework, and the scanner completed with no installation or target-code execution. The skill's fallback behavior was adequate when no Applied Opt MCP connection was available.

## Maintainer follow-up

Added `-B` to the wrapper child and probe interpreter arguments after this trial; the updated wrapper succeeded from an unrelated working directory. The original trial records the pre-fix snapshot.
