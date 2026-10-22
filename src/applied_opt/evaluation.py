"""Paired, interleaved latency measurement with explicit application assertions.

The harness runs only callables supplied by its caller. Reviewing source never
imports or executes that source. Reports omit payloads and outputs by default.
"""
from __future__ import annotations

import asyncio
import copy
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence


@dataclass(frozen=True)
class Case:
    id: str
    payload: Mapping[str, Any]
    expected: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    output: Any
    spans: list[dict[str, Any]] = field(default_factory=list)
    calls: int | None = None
    cost_usd: float | None = None


Runner = Callable[[Mapping[str, Any]], Awaitable[Observation | Any]]
Judge = Callable[[Any, Mapping[str, Any]], Mapping[str, bool]]


def _metadata_spans(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep known telemetry fields; caller-provided payload/output fields stay out."""
    string_fields = {"name", "status", "cache_status", "capability", "resource", "error_type"}
    number_fields = {"start_ms", "end_ms", "duration_ms", "attempts"}
    result = []
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        cleaned = {}
        for key, value in span.items():
            if key in string_fields and (value is None or isinstance(value, str)):
                cleaned[key] = value
            elif key in number_fields and type(value) in (int, float) and math.isfinite(value) and value >= 0:
                cleaned[key] = value
            elif key == "depends_on" and isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
                cleaned[key] = list(value)
        if cleaned:
            result.append(cleaned)
    return result


def percentile(values: Sequence[float], percentile_value: float) -> float:
    """Linear interpolation, including small samples; not a confidence bound."""
    if not values or not 0 <= percentile_value <= 100:
        raise ValueError("nonempty values and percentile in [0, 100] required")
    if any(not math.isfinite(x) or x < 0 for x in values):
        raise ValueError("latencies must be finite and nonnegative")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None, "mean_ms": None}
    return {
        "samples": len(values),
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "mean_ms": round(statistics.mean(values), 3),
    }


async def evaluate(
    baseline: Runner,
    candidate: Runner,
    cases: Sequence[Case],
    judge: Judge,
    *,
    repetitions: int = 5,
    warmups: int = 1,
    timeout_s: float = 30.0,
    max_p95_regression: float = 0.05,
    kind: str = "application",
) -> dict[str, Any]:
    """Measure A/B then B/A, without overlapping variants.

    Every measured result must satisfy named ground-truth checks. Exceptions
    and invalid/empty judges fail quality, without logging private exception
    messages. A speedup cannot override a quality failure. Cache states and
    model/reference-time controls remain the caller's responsibility.
    """
    if not cases or repetitions < 1 or warmups < 0:
        raise ValueError("cases and positive repetitions are required")
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("case IDs must be unique")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("positive timeout required")
    if not math.isfinite(max_p95_regression) or max_p95_regression < 0:
        raise ValueError("regression tolerance must be nonnegative")
    runners = {"baseline": baseline, "candidate": candidate}
    timings: dict[str, list[float]] = {k: [] for k in runners}
    all_checks: dict[str, list[bool]] = {k: [] for k in runners}
    failures: list[dict[str, Any]] = []
    traces: dict[str, list[dict[str, Any]]] = {k: [] for k in runners}
    per_case: list[dict[str, Any]] = []

    async def invoke(runner: Runner, case: Case) -> tuple[Observation, float, str | None]:
        start = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s):
                output = await runner(copy.deepcopy(dict(case.payload)))
            observation = output if isinstance(output, Observation) else Observation(output)
            return observation, (time.perf_counter() - start) * 1000, None
        except Exception as exc:
            return Observation(None), (time.perf_counter() - start) * 1000, type(exc).__name__

    for case in cases:
        required_check_names: set[str] | None = None
        for _ in range(warmups):
            for runner in runners.values():
                await invoke(runner, case)
        records: dict[str, list[dict[str, Any]]] = {k: [] for k in runners}
        for iteration in range(repetitions):
            order = ("baseline", "candidate") if iteration % 2 == 0 else ("candidate", "baseline")
            for variant in order:
                observation, elapsed, error = await invoke(runners[variant], case)
                checks: dict[str, bool]
                if error:
                    checks = {"execution_succeeded": False}
                else:
                    try:
                        judged = judge(observation.output, copy.deepcopy(dict(case.expected)))
                        if not isinstance(judged, Mapping) or not judged:
                            raise ValueError("judge must supply named checks")
                        if any(not isinstance(k, str) or type(v) is not bool for k, v in judged.items()):
                            raise ValueError("judge values must be booleans")
                        checks = dict(judged)
                        current_names = set(checks)
                        if required_check_names is None:
                            required_check_names = current_names
                        elif current_names != required_check_names:
                            # A judge that only loops over existing output fields
                            # must not let an omitted requirement silently pass.
                            checks["check_set_consistent"] = False
                    except Exception as exc:
                        checks = {"judge_valid": False}
                        error = type(exc).__name__
                timings[variant].append(elapsed)
                all_checks[variant].extend(checks.values())
                for name, passed in checks.items():
                    if not passed:
                        failures.append({"case_id": case.id, "variant": variant, "iteration": iteration, "check": name, "error_type": error})
                record = {"latency_ms": round(elapsed, 3), "checks": checks, "calls": observation.calls, "cost_usd": observation.cost_usd}
                records[variant].append(record)
                if not traces[variant] and observation.spans:
                    traces[variant] = _metadata_spans(observation.spans)
        per_case.append({"id": case.id, **{v: {**latency_summary([r["latency_ms"] for r in rows]), "runs": rows} for v, rows in records.items()}})

    summaries = {v: latency_summary(times) for v, times in timings.items()}
    quality_ok = all(all_checks["candidate"]) and bool(all_checks["candidate"])
    baseline_ok = all(all_checks["baseline"]) and bool(all_checks["baseline"])
    p95_ok = summaries["candidate"]["p95_ms"] <= summaries["baseline"]["p95_ms"] * (1 + max_p95_regression)
    # Aggregated p95 may conceal a regression in a small input cohort.
    case_regressions = [row["id"] for row in per_case if row["candidate"]["p95_ms"] > row["baseline"]["p95_ms"] * (1 + max_p95_regression)]
    reasons = []
    if not quality_ok:
        reasons.append("candidate_failed_required_checks")
    if not baseline_ok:
        reasons.append("baseline_failed_required_checks")
    if not p95_ok or case_regressions:
        reasons.append("p95_regression")
    return {
        "benchmark": {"kind": kind, "repetitions": repetitions, "warmups": warmups, "order": "alternating A/B and B/A per case", **summaries, "cases": per_case, "note": "Small-sample percentiles are descriptive. Validate with production-like load and held-out inputs."},
        "quality": {"passed": sum(all_checks["candidate"]), "total": len(all_checks["candidate"]), "baseline_passed": sum(all_checks["baseline"]), "baseline_total": len(all_checks["baseline"]), "baseline_failures": sum(not x for x in all_checks["baseline"]), "candidate_failures": sum(not x for x in all_checks["candidate"]), "failures": failures, "scope": "Only the supplied ground-truth assertions; not a universal correctness guarantee."},
        "gate": {"passed": not reasons, "reasons": reasons, "max_p95_regression": max_p95_regression, "case_regressions": case_regressions},
        "traces": traces,
    }


def compare_reports(before: Mapping[str, Any], after: Mapping[str, Any], *, tolerance: float = .05) -> dict[str, Any]:
    """Compare explicit latency samples and checks from two saved run reports.

    Input shape: {runs:[{case_id,latency_ms,checks:{name:bool}}]}. Missing
    checks, cases, differing check names, errors and non-finite data fail closed.
    """
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and nonnegative")
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for variant, report in (("baseline", before), ("candidate", after)):
        runs = report.get("runs")
        if not isinstance(runs, list) or not runs:
            raise ValueError(f"{variant}: nonempty runs required")
        groups[variant] = {}
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get("case_id"), str) or not run["case_id"]:
                raise ValueError("every run needs a case_id")
            duration = run.get("latency_ms")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                raise ValueError("finite nonnegative latency_ms required")
            checks = run.get("checks")
            if not isinstance(checks, dict) or not checks or any(not isinstance(k, str) or type(v) is not bool for k, v in checks.items()):
                raise ValueError("nonempty named boolean checks required")
            groups[variant].setdefault(run["case_id"], []).append(run)
    reasons: list[str] = []
    if groups["baseline"].keys() != groups["candidate"].keys():
        reasons.append("case_set_mismatch")
    rows = []
    for case_id in sorted(set(groups["baseline"]) | set(groups["candidate"])):
        variants = {v: groups[v].get(case_id, []) for v in groups}
        checks = [{tuple(sorted(run["checks"])) for run in rows_} for rows_ in variants.values()]
        if len(checks[0]) != 1 or checks[0] != checks[1]:
            reasons.append(f"{case_id}:check_set_mismatch")
        if len(variants["baseline"]) != len(variants["candidate"]):
            reasons.append(f"{case_id}:sample_count_mismatch")
        for v, runs in variants.items():
            if any(run.get("error") or run.get("error_type")
                   or ("status" in run and run["status"] not in ("completed", "success", "ok"))
                   for run in runs):
                reasons.append(f"{case_id}:{v}_execution_failure")
            if any(not all(run["checks"].values()) for run in runs):
                reasons.append(f"{case_id}:{v}_quality_failure")
        stats = {v: latency_summary([r["latency_ms"] for r in runs]) for v, runs in variants.items()}
        if all(stats[v]["samples"] for v in stats) and stats["candidate"]["p95_ms"] > stats["baseline"]["p95_ms"] * (1 + tolerance):
            reasons.append(f"{case_id}:p95_regression")
        rows.append({"id": case_id, **stats})
    return {"passed": not reasons, "reasons": reasons, "cases": rows, "tolerance": tolerance, "note": "Checks are caller-supplied evidence. Matching check names do not prove matching models, prompts, load, or reference time."}
