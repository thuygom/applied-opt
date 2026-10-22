"""Transparent scheduling estimates. These are hypotheses, not measurements."""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Work:
    name: str
    duration_ms: float
    depends_on: tuple[str, ...] = ()
    resource: str = "io"
    concurrency_safe: bool = False
    cost_usd: float = 0.0


def _validate(work: Sequence[Work], limits: Mapping[str, int]) -> None:
    if not work or len({w.name for w in work}) != len(work):
        raise ValueError("nonempty work with unique names required")
    names = {w.name for w in work}
    for w in work:
        if not w.name or not math.isfinite(w.duration_ms) or w.duration_ms <= 0:
            raise ValueError("positive finite work durations required")
        if not math.isfinite(w.cost_usd) or w.cost_usd < 0:
            raise ValueError("nonnegative finite cost required")
        if not set(w.depends_on) <= names or w.name in w.depends_on:
            raise ValueError("invalid dependency")
    if any(type(v) is not int or v < 1 for v in limits.values()):
        raise ValueError("resource limits must be positive integers")


def estimate_schedule(work: Sequence[Work], *, concurrency: int, resource_limits: Mapping[str, int] | None = None, contention_multiplier: Mapping[str, float] | None = None, launch_overhead_ms: float = 0.0) -> dict[str, Any]:
    """Greedy DAG list scheduling with hard capacities and exclusive unsafe work.

    A resource's explicit contention multiplier applies at concurrency > 1.
    This deliberately simple scenario model does not predict queueing or p95.
    """
    limits = dict(resource_limits or {})
    multipliers = dict(contention_multiplier or {})
    _validate(work, limits)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("positive integer concurrency required")
    if not math.isfinite(launch_overhead_ms) or launch_overhead_ms < 0:
        raise ValueError("finite nonnegative overhead required")
    if any(not math.isfinite(v) or v < 1 for v in multipliers.values()):
        raise ValueError("contention multipliers must be finite and >= 1")
    remaining = {w.name: w for w in work}
    active: list[tuple[float, str, Work]] = []
    completed: set[str] = set()
    counts: dict[str, int] = {}
    clock = 0.0
    spans = []
    while remaining or active:
        for name, w in list(remaining.items()):
            if len(active) >= concurrency or not set(w.depends_on) <= completed:
                continue
            if active and (not w.concurrency_safe or any(not x[2].concurrency_safe for x in active)):
                # Match the runtime's conservative exclusive-step ordering.
                break
            if counts.get(w.resource, 0) >= limits.get(w.resource, concurrency):
                continue
            multiplier = multipliers.get(w.resource, 1.0) if concurrency > 1 and limits.get(w.resource, concurrency) > 1 else 1.0
            duration = w.duration_ms * multiplier + (launch_overhead_ms if concurrency > 1 else 0)
            heapq.heappush(active, (clock + duration, name, w))
            counts[w.resource] = counts.get(w.resource, 0) + 1
            spans.append({"name": name, "start_ms": round(clock, 3), "duration_ms": round(duration, 3), "depends_on": list(w.depends_on), "resource": w.resource})
            del remaining[name]
        if not active:
            raise ValueError("dependency cycle")
        clock = active[0][0]
        while active and active[0][0] <= clock:
            _, name, w = heapq.heappop(active)
            completed.add(name)
            counts[w.resource] -= 1
    return {"concurrency": concurrency, "estimated_ms": round(clock, 3), "calls": len(work), "cost_usd": round(sum(w.cost_usd for w in work), 8), "spans": spans}


def compare_strategies(work: Sequence[Work], *, max_concurrency: int = 4, resource_limits: Mapping[str, int] | None = None, contention_multiplier: Mapping[str, float] | None = None, launch_overhead_ms: float = 0.0, min_improvement: float = .05) -> dict[str, Any]:
    if type(max_concurrency) is not int or not 1 <= max_concurrency <= 128:
        raise ValueError("max_concurrency must be in [1,128]")
    if not 0 <= min_improvement < 1:
        raise ValueError("min_improvement must be in [0,1)")
    candidates = [estimate_schedule(work, concurrency=c, resource_limits=resource_limits, contention_multiplier=contention_multiplier, launch_overhead_ms=launch_overhead_ms) for c in range(1, max_concurrency + 1)]
    baseline = candidates[0]
    best = min(candidates, key=lambda c: (c["estimated_ms"], c["concurrency"]))
    improvement = 1 - best["estimated_ms"] / baseline["estimated_ms"]
    selected = best if improvement >= min_improvement else baseline
    return {"kind": "estimate", "recommendation": "bounded_parallel" if selected["concurrency"] > 1 else "sequential", "selected_concurrency": selected["concurrency"], "estimated_sequential_ms": baseline["estimated_ms"], "estimated_parallel_ms": selected["estimated_ms"], "improvement_fraction": round(1 - selected["estimated_ms"] / baseline["estimated_ms"], 4), "candidates": candidates, "assumptions": ["All listed calls are needed; parallelism does not reduce call count or token cost.", "Dependencies and resource capacities are supplied by the engineer.", "Contention is a supplied scenario multiplier; rate-limit retries and queueing require load measurements.", "Validate candidate quality, cancellation and per-cohort p95 before applying."], "reason": "Choose the smallest concurrency with the best estimated latency, subject to explicit dependencies, capacities and minimum benefit."}


def conditional_tradeoff(*, decision_ms: float, branch_ms: float, needed_probability: float, branch_cost_usd: float, decision_cost_usd: float = 0) -> dict[str, Any]:
    """Expected latency/cost for gating vs speculating one conditional branch.

    The two start together for speculation; unnecessary work is assumed billed
    in full, even if cancellation is requested. No quality equivalence implied.
    """
    values = (decision_ms, branch_ms, branch_cost_usd, decision_cost_usd)
    if any(not math.isfinite(x) or x < 0 for x in values) or not 0 <= needed_probability <= 1:
        raise ValueError("finite nonnegative values and probability in [0,1] required")
    p = needed_probability
    return {"kind": "estimate", "gated": {"expected_ms": decision_ms + p * branch_ms, "expected_cost_usd": decision_cost_usd + p * branch_cost_usd}, "speculative": {"expected_ms": p * max(decision_ms, branch_ms) + (1 - p) * decision_ms, "expected_cost_usd": decision_cost_usd + branch_cost_usd}, "assumptions": ["Side effects are prohibited for speculative work.", "The branch can run without the decision output.", "Unneeded speculative work is fully billed; client cancellation need not cancel provider billing.", "Routing correctness must be validated separately."]}
