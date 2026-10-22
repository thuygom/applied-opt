import asyncio
import math
import json
import unittest

from applied_opt.evaluation import Case, Observation, StopEvaluation, compare_reports, evaluate, percentile
from applied_opt.planning import (Work, compare_strategies, conditional_tradeoff, estimate_schedule,
                                  sensitivity, what_if, work_from_spans)


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_missing_schedule_is_blocked(self):
        async def before(payload):
            await asyncio.sleep(.001)
            return {"schedules": ["2030-11-02T16:00:00+09:00"]}
        async def after(payload):
            return {"schedules": []}
        def judge(output, expected):
            return {"schedule_preserved": output["schedules"] == expected["schedules"]}
        result = await evaluate(before, after, [Case("meeting", {}, {"schedules": ["2030-11-02T16:00:00+09:00"]})], judge, repetitions=2, warmups=0)
        self.assertFalse(result["gate"]["passed"])
        self.assertIn("candidate_failed_required_checks", result["gate"]["reasons"])
        self.assertEqual(result["quality"]["candidate_failures"], 2)

    async def test_payload_isolated_and_order_alternates(self):
        calls = []
        async def before(payload):
            calls.append("A")
            payload["items"].append(2)
            return True
        async def after(payload):
            calls.append("B")
            self.assertEqual(payload["items"], [1])
            return True
        result = await evaluate(before, after, [Case("x", {"items": [1]})], lambda o, e: {"ok": o is True}, repetitions=2, warmups=0, max_p95_regression=100)
        self.assertEqual(calls, ["A", "B", "B", "A"])
        self.assertEqual(result["quality"]["passed"], 2)

    async def test_empty_judge_cannot_pass(self):
        async def run(payload): return {}
        report = await evaluate(run, run, [Case("x", {})], lambda o, e: {}, repetitions=1, warmups=0)
        self.assertFalse(report["gate"]["passed"])

    async def test_judge_cannot_silently_drop_required_check(self):
        async def baseline(payload):
            return {"summary": True, "schedule": True}
        async def candidate(payload):
            return {"summary": True}
        report = await evaluate(baseline, candidate, [Case("meeting", {})],
                                lambda output, expected: output,
                                repetitions=1, warmups=0, max_p95_regression=100)
        self.assertFalse(report["gate"]["passed"])
        self.assertTrue(any(f["check"] == "check_set_consistent" for f in report["quality"]["failures"]))

    async def test_span_payload_and_outputs_are_not_exported(self):
        async def run(payload):
            return Observation(True, spans=[{"name": "stage", "start_ms": 0, "duration_ms": 1,
                                             "payload": "private-input", "output": "private-output"}])
        report = await evaluate(run, run, [Case("x", {})], lambda o, e: {"ok": o},
                                repetitions=1, warmups=0, max_p95_regression=100)
        self.assertNotIn("private-input", json.dumps(report))
        self.assertNotIn("private-output", json.dumps(report))
        self.assertEqual(report["traces"]["baseline"][0]["name"], "stage")


class BudgetAndStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_refuses_before_any_paid_call(self):
        calls = []
        async def runner(payload):
            calls.append(payload)
            return payload
        cases = [Case(str(i), {"i": i}) for i in range(4)]
        with self.assertRaises(ValueError):
            await evaluate(runner, runner, cases, lambda o, e: {"ok": True}, repetitions=3, warmups=1, max_invocations=31)
        self.assertEqual([], calls)

    async def test_stop_ends_both_variants_and_fails_the_gate(self):
        calls = {"baseline": 0, "candidate": 0}
        async def baseline(payload):
            calls["baseline"] += 1
            return payload
        async def candidate(payload):
            calls["candidate"] += 1
            if payload["i"] == 1:
                raise StopEvaluation("insufficient_quota")
            return payload
        cases = [Case(str(i), {"i": i}) for i in range(3)]
        report = await evaluate(baseline, candidate, cases, lambda o, e: {"ok": o is not None},
                                repetitions=2, warmups=0)
        self.assertIn("evaluation_stopped", report["gate"]["reasons"])
        self.assertFalse(report["gate"]["passed"])
        self.assertEqual({"case_id": "1", "error_type": "insufficient_quota"}, report["benchmark"]["stopped"])
        # Case 0 ran fully (2+2); case 1 stopped on its first candidate call; case 2 never ran.
        self.assertEqual({"baseline": 3, "candidate": 3}, calls)
        self.assertEqual(6, report["benchmark"]["invocations"]["made"])
        self.assertEqual(["0", "1"], [row["id"] for row in report["benchmark"]["cases"]])


class CompareTests(unittest.TestCase):
    def test_missing_case_and_check_fails(self):
        a = {"runs": [{"case_id": "a", "latency_ms": 10, "checks": {"schedule": True}}]}
        b = {"runs": [{"case_id": "b", "latency_ms": 1, "checks": {"summary": True}}]}
        self.assertFalse(compare_reports(a, b)["passed"])

    def test_regression_not_hidden_by_aggregate(self):
        def r(x,y): return {"runs": [{"case_id":"short","latency_ms":x,"checks":{"ok":True}},{"case_id":"long","latency_ms":y,"checks":{"ok":True}}]}
        result=compare_reports(r(10,1000),r(20,100))
        self.assertIn("short:p95_regression", result["reasons"])

    def test_nonfinite_rejected(self):
        with self.assertRaises(ValueError): percentile([math.nan], 50)

    def test_explicit_execution_errors_cannot_pass_positive_checks(self):
        baseline = {"runs": [{"case_id": "x", "latency_ms": 10, "checks": {"ok": True}}]}
        for error in ({"error_type": "TimeoutError"}, {"error": "failed"}, {"status": "cancelled"}):
            candidate = {"runs": [{"case_id": "x", "latency_ms": 1, "checks": {"ok": True}, **error}]}
            self.assertIn("x:candidate_execution_failure", compare_reports(baseline, candidate)["reasons"])


class PlanningTests(unittest.TestCase):
    def test_independent_io_benefits(self):
        result = compare_strategies([Work("a",100,concurrency_safe=True),Work("b",80,concurrency_safe=True)])
        self.assertEqual(result["selected_concurrency"],2)
        self.assertEqual(result["estimated_parallel_ms"],100)

    def test_shared_gpu_one_slot_no_benefit(self):
        result=compare_strategies([Work("a",100,resource="gpu",concurrency_safe=True),Work("b",80,resource="gpu",concurrency_safe=True)],resource_limits={"gpu":1})
        self.assertEqual(result["recommendation"],"sequential")

    def test_contention_can_make_sequential_best(self):
        result=compare_strategies([Work("a",100,concurrency_safe=True),Work("b",80,concurrency_safe=True)],contention_multiplier={"io":3})
        self.assertEqual(result["recommendation"],"sequential")

    def test_dependency_is_respected(self):
        result=estimate_schedule([Work("a",100),Work("b",80,("a",))],concurrency=4)
        self.assertEqual(result["estimated_ms"],180)

    def test_cycles_rejected(self):
        with self.assertRaises(ValueError): estimate_schedule([Work("a",1,("b",)),Work("b",1,("a",))],concurrency=2)

    def test_unknown_concurrency_defaults_to_sequential(self):
        result = compare_strategies([Work("a", 100), Work("b", 80)])
        self.assertEqual(result["recommendation"], "sequential")

    def test_exclusive_step_is_not_leapfrogged(self):
        result = estimate_schedule([Work("a", 10, concurrency_safe=True),
                                    Work("exclusive", 50),
                                    Work("c", 100, concurrency_safe=True)], concurrency=3)
        self.assertEqual(result["estimated_ms"], 160)
        starts = {s["name"]: s["start_ms"] for s in result["spans"]}
        self.assertEqual(starts["exclusive"], 10)
        self.assertEqual(starts["c"], 60)

    def test_trace_replay_shows_which_stage_reduction_is_visible(self):
        spans = [{"name": "select", "duration_ms": 350}, {"name": "classify", "duration_ms": 2800},
                 {"name": "typo", "duration_ms": 11000}, {"name": "typo", "duration_ms": 12400},
                 {"name": "generate", "duration_ms": 5700}, {"name": "review", "duration_ms": 5700},
                 {"name": "failed", "duration_ms": 900, "status": "error"}]
        deps = {"classify": ["select"], "typo": ["select"], "generate": ["select", "typo"],
                "review": ["generate", "classify", "typo"], "unobserved": ["select"]}
        work = work_from_spans(spans, deps)
        self.assertEqual({"select", "classify", "typo", "generate", "review"}, {w.name for w in work})
        self.assertEqual(11700, next(w for w in work if w.name == "typo").duration_ms)
        stages = {row["name"]: row for row in sensitivity(work)["stages"]}
        self.assertEqual(5850, stages["typo"]["visible_saving_ms"])
        # Classification overlaps the serial correction: shortening it is invisible.
        self.assertEqual(0, stages["classify"]["visible_saving_ms"])
        overlap = what_if(work, drop_dependencies=[("generate", "typo")])
        self.assertEqual(23450, overlap["baseline_ms"])
        self.assertEqual(17750, overlap["scenario_ms"])
        with self.assertRaises(ValueError):
            what_if(work, durations={"missing": 1})

    def test_speculation_cost_tradeoff(self):
        r=conditional_tradeoff(decision_ms=10,branch_ms=100,needed_probability=.1,branch_cost_usd=1)
        self.assertAlmostEqual(r["gated"]["expected_ms"],20)
        self.assertAlmostEqual(r["speculative"]["expected_ms"],19)
        self.assertAlmostEqual(r["gated"]["expected_cost_usd"],.1)
        self.assertAlmostEqual(r["speculative"]["expected_cost_usd"],1)
