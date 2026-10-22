"""Reproducible synthetic fixtures: real wall times, simulated AI service work.

No model API, company code, operational logs, or user recordings are used.
This validates scheduling mechanics and named invariants, not an LLM's accuracy.
"""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .evaluation import Case, Observation, evaluate
from .planning import Work, compare_strategies, conditional_tradeoff
from .routing import Capability, Decision, Router
from .runtime import Pipeline, RunContext, Step, gather_strict


CAPABILITIES = (
    Capability("summary", "Summarize only supported facts, preserving negation.", {"type": "string"}, mandatory=True, reviewers=("summary_review",)),
    Capability("tasks", "Extract explicit commitments and deadlines; exclude cancelled tasks.", {"type": "array", "items": {"type": "string"}}, reviewers=("task_review",)),
    Capability("schedules", "Extract explicit appointments with date and timezone; preserve cancellation.", {"type": "array", "items": {"type": "string"}}, reviewers=("schedule_review",)),
)

FIXTURES = [
    {"id": "simple-record", "text": "오늘 산책을 마쳤다.", "selected": ["summary"], "confidence": .99, "summary": "산책 완료", "tasks": [], "schedules": []},
    {"id": "explicit-meeting", "text": "2030년 11월 2일 16시에 미팅. 전날까지 보고서를 준비한다.", "selected": ["summary", "tasks", "schedules"], "confidence": .99, "summary": "미팅 및 보고서 준비", "tasks": ["보고서 준비:2030-11-01"], "schedules": ["2030-11-02T16:00:00+09:00"]},
    {"id": "negation-cancel", "text": "보고서를 보내지 마세요. 다음 주 일정도 취소됐습니다.", "selected": ["summary", "tasks", "schedules"], "confidence": .99, "summary": "보고서 발송 금지 및 일정 취소", "tasks": [], "schedules": []},
    {"id": "ambiguous-fallback", "text": "나중에 한번 볼까 싶은데 아직 미정이다.", "selected": ["summary"], "confidence": .55, "summary": "만남 여부 미정", "tasks": [], "schedules": []},
]


async def _run(payload: Mapping[str, Any], *, optimized: bool) -> Observation:
    fixture = payload["fixture"]
    origin = time.perf_counter()
    spans: list[dict[str, Any]] = []

    async def prefix(name: str, seconds: float, value: Any) -> Any:
        started = (time.perf_counter() - origin) * 1000
        await asyncio.sleep(seconds)
        ended = (time.perf_counter() - origin) * 1000
        dependencies = ["account_context"] if not optimized and name == "memory" else []
        spans.append({"name": name, "start_ms": started, "duration_ms": ended - started, "status": "completed", "depends_on": dependencies, "cache_status": "disabled"})
        return value

    router = Router(CAPABILITIES)

    async def fixture_decider(value, names):
        await asyncio.sleep(.006)
        return Decision(tuple(fixture["selected"]), fixture["confidence"], names)

    async def route_request():
        start = (time.perf_counter() - origin) * 1000
        route = await router.route(payload, fixture_decider if optimized else None)
        end = (time.perf_counter() - origin) * 1000
        spans.append({"name": "route" if optimized else "full_contract", "start_ms": start, "duration_ms": end - start, "status": "fallback" if route.fallback else "completed", "depends_on": [] if optimized else ["memory"], "cache_status": "disabled"})
        return route

    if optimized:
        _, _, route = await gather_strict(prefix("account_context", .022, {}), prefix("memory", .018, {}), route_request())
    else:
        await prefix("account_context", .022, {})
        await prefix("memory", .018, {})
        route = await route_request()

    async def normalize(context, deps):
        await asyncio.sleep(.008)
        return fixture["text"].strip()

    async def classify(context, deps):
        await asyncio.sleep(.012)
        return "note"

    async def generate(context, deps):
        # Simulated token work scales with the compiled prompt contract.
        await asyncio.sleep(.030 + .020 * (len(route.enabled) - 1))
        return {name: copy.deepcopy(fixture[name]) for name in route.enabled}

    async def review_summary(context, deps):
        await asyncio.sleep(.010)
        return True

    async def review_tasks(context, deps):
        await asyncio.sleep(.014)
        return True

    async def review_schedule(context, deps):
        await asyncio.sleep(.016)
        return True

    steps = [
        Step("normalize", normalize, concurrency_safe=True),
        Step("classify", classify, concurrency_safe=True),
        Step("generate", generate, depends_on=("normalize", "classify"), concurrency_safe=True),
        Step("summary_review", review_summary, depends_on=("generate",), capability="summary", concurrency_safe=True),
        Step("task_review", review_tasks, depends_on=("generate",), capability="tasks", concurrency_safe=True),
        Step("schedule_review", review_schedule, depends_on=("generate",), capability="schedules", concurrency_safe=True),
    ]
    pipeline = Pipeline(steps, max_concurrency=3 if optimized else 1)
    offset = (time.perf_counter() - origin) * 1000
    result = await pipeline.run(RunContext(payload, route=route), timeout_s=5)
    for span in result.trace_dict()["spans"]:
        # Runtime dependency tuples are immutable; this demo adds serialized
        # scheduling edges to a separate, mutable trace representation.
        span["depends_on"] = list(span["depends_on"])
        span["start_ms"] += offset
        span["end_ms"] += offset
        span["cache_hit"] = span["cache_status"] == "hit"
        if span["name"] in {"normalize", "classify"}:
            span["depends_on"] = ["account_context", "memory", "route" if optimized else "full_contract"]
            if not optimized and span["name"] == "classify":
                span["depends_on"].append("normalize")
        if not optimized and span["name"] == "task_review":
            span["depends_on"].append("summary_review")
        if not optimized and span["name"] == "schedule_review":
            span["depends_on"].append("task_review")
        spans.append(span)
    generated = result.outputs["generate"]
    # Public response shape stays stable even when the internal schema shrinks.
    output = {"summary": generated["summary"], "tasks": generated.get("tasks", []), "schedules": generated.get("schedules", []), "enabled": list(route.enabled), "fallback": route.fallback}
    return Observation(output, sorted(spans, key=lambda s: s["start_ms"]), calls=len([s for s in spans if s["status"] != "skipped"]))


def _judge(output: Any, expected: Mapping[str, Any]) -> Mapping[str, bool]:
    return {"summary_correct": output["summary"] == expected["summary"], "tasks_preserved": output["tasks"] == expected["tasks"], "schedule_datetime_preserved": output["schedules"] == expected["schedules"]}


async def create_demo_report(*, repetitions: int = 7, warmups: int = 1) -> dict[str, Any]:
    cases = [Case(f["id"], {"fixture": copy.deepcopy(f)}, {k: f[k] for k in ("summary", "tasks", "schedules")}) for f in FIXTURES]
    async def baseline(payload): return await _run(payload, optimized=False)
    async def candidate(payload): return await _run(payload, optimized=True)
    result = await evaluate(baseline, candidate, cases, _judge, repetitions=repetitions, warmups=warmups, kind="synthetic")
    result.update({"schema_version": "1.0", "project": "Applied Opt · synthetic note pipeline", "generated_at": datetime.now(timezone.utc).isoformat(), "description": "Measured local wall time around synthetic async service delays. No real model/API/STT performance claim.", "review": {"findings": []}, "routing": [
        {"case_id": f["id"], "enabled": f["selected"] if f["confidence"] >= .95 else ["summary", "tasks", "schedules"], "fallback": f["confidence"] < .95, "decision_source": "synthetic fixture; not an NLP classifier"} for f in FIXTURES
    ], "cache": {"enabled": False, "reason": "Baseline and candidate both run without result caching."}, "tradeoffs": [
        {"name": "Independent I/O", **compare_strategies([Work("context",22,concurrency_safe=True),Work("memory",18,concurrency_safe=True),Work("route",6,concurrency_safe=True)],max_concurrency=4)},
        {"name": "One GPU slot", **compare_strategies([Work("stt",100,resource="gpu",concurrency_safe=True),Work("speaker",80,resource="gpu",concurrency_safe=True)],resource_limits={"gpu":1})},
        {"name": "Contended provider", **compare_strategies([Work("a",100,resource="llm",concurrency_safe=True),Work("b",80,resource="llm",concurrency_safe=True)],contention_multiplier={"llm":2.5})},
    ], "conditional": conditional_tradeoff(decision_ms=10,branch_ms=100,needed_probability=.1,branch_cost_usd=.01), "controls": {"reference_time": "2030-10-01T09:00:00+09:00", "model": "synthetic deterministic fixture", "cache": "disabled", "load": "one request at a time", "raw_inputs_in_report": False}})
    return result
