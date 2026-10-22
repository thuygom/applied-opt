"""Regression coverage for trace contracts and the process evaluation boundary."""
import asyncio
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from applied_opt import worker
from applied_opt.evaluation import Case, Observation, evaluate
from applied_opt.planning import estimate_schedule, work_from_spans
from applied_opt.process import ProcessRunner


class TraceContractTests(unittest.TestCase):
    def test_completed_spans_are_accepted(self):
        work = work_from_spans([{"name": "stage", "duration_ms": 10, "status": "completed"}])
        self.assertEqual(("stage", 10, False), (work[0].name, work[0].duration_ms, work[0].concurrency_safe))

    def test_success_aliases_and_invalid_samples(self):
        spans = [{"name": str(i), "duration_ms": 10, "status": status}
                 for i, status in enumerate((None, "completed", "success", "ok"))]
        spans += [{"name": "bad", "duration_ms": 10, "status": status}
                  for status in ("failed", "cancelled", "skipped", "error")]
        spans += [{"name": "bad", "duration_ms": duration}
                  for duration in (True, 0, -1, math.nan, math.inf, "10")]
        self.assertEqual(["0", "1", "2", "3"], [w.name for w in work_from_spans(spans)])

    def test_all_failed_spans_are_rejected(self):
        with self.assertRaises(ValueError):
            work_from_spans([{"name": "a", "duration_ms": 1, "status": "failed"}])

    def test_overlap_does_not_establish_safe_concurrency(self):
        spans = [{"name": name, "duration_ms": duration, "start_ms": 0, "concurrency_safe": True}
                 for name, duration in (("a", 100), ("b", 80))]
        work = work_from_spans(spans)
        self.assertEqual([False, False], [w.concurrency_safe for w in work])
        self.assertEqual(180, estimate_schedule(work, concurrency=2)["estimated_ms"])
        work = work_from_spans(spans, concurrency_safe={"a": True, "b": True})
        self.assertEqual(100, estimate_schedule(work, concurrency=2)["estimated_ms"])

    def test_partial_contract_keeps_other_stages_exclusive(self):
        spans = [{"name": name, "duration_ms": 10} for name in ("a", "b")]
        work = work_from_spans(spans, concurrency_safe={"a": True})
        self.assertEqual([True, False], [w.concurrency_safe for w in work])

    def test_contract_requires_actual_booleans(self):
        for value in (1, 0, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                work_from_spans([{"name": "a", "duration_ms": 1}], concurrency_safe={"a": value})

    def test_resource_limits_survive_trace_conversion(self):
        spans = [{"name": name, "duration_ms": duration, "resource": "gpu", "status": "completed"}
                 for name, duration in (("a", 100), ("b", 80))]
        work = work_from_spans(spans, concurrency_safe={"a": True, "b": True})
        self.assertEqual(["gpu", "gpu"], [w.resource for w in work])
        self.assertEqual(180, estimate_schedule(work, concurrency=2, resource_limits={"gpu": 1})["estimated_ms"])

    def test_conflicting_resources_require_explicit_override(self):
        spans = [{"name": "a", "duration_ms": 10, "resource": r} for r in ("gpu", "io")]
        with self.assertRaises(ValueError):
            work_from_spans(spans)
        self.assertEqual("gpu", work_from_spans(spans, resources={"a": "gpu"})[0].resource)

    def test_none_resource_defaults_to_io(self):
        self.assertEqual("io", work_from_spans([{"name": "a", "duration_ms": 1, "resource": None}])[0].resource)

    def test_invalid_resource_contract_is_rejected(self):
        for value in ("", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                work_from_spans([{"name": "a", "duration_ms": 1}], resources={"a": value})

    def test_dependencies_and_medians_remain_intact(self):
        work = work_from_spans([{"name": "a", "duration_ms": 10},
                                {"name": "b", "duration_ms": 20, "depends_on": ["a"]},
                                {"name": "b", "duration_ms": 30, "depends_on": ["a"]}])
        self.assertEqual((25, ("a",)), (work[1].duration_ms, work[1].depends_on))


HANDLERS = '''
import asyncio
from pathlib import Path

async def delay(payload):
    if payload.get("started"):
        Path(payload["started"]).touch()
    await asyncio.sleep(payload.get("delay", 0))
    return {"output": payload.get("value"), "spans": [{"name": "stage", "duration_ms": 1}], "calls": 1}

def quota(payload):
    from applied_opt.evaluation import StopEvaluation
    raise StopEvaluation("insufficient_quota")

def standalone_quota(payload):
    from worker import StopWorker
    try:
        raise ValueError("private-provider-detail")
    except ValueError:
        raise StopWorker("insufficient_quota") from None

def broken(payload):
    raise KeyError("private-provider-detail")
'''


class ProcessBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "handlers.py").write_text(HANDLERS, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, target="delay", **kwargs):
        # Absolute package path also works with unittest discovery from a checkout.
        env = dict(os.environ, PYTHONPATH=str(Path(worker.__file__).resolve().parents[1]))
        return ProcessRunner([sys.executable, worker.__file__, "handlers:" + target],
                             cwd=self.root, env=env, **kwargs)

    async def test_stop_signal_ends_both_variants_in_measurement_and_warmup(self):
        for warmups in (0, 1):
            with self.subTest(warmups=warmups):
                other_calls = []
                async def other(payload):
                    other_calls.append(payload)
                    return True
                async with self.runner("quota") as r:
                    report = await evaluate(r, other, [Case("x", {})], lambda o,e: {"ok": o is True},
                                            repetitions=2, warmups=warmups)
                self.assertEqual([], other_calls)
                self.assertEqual(1, report["benchmark"]["invocations"]["made"])
                self.assertEqual("insufficient_quota", report["benchmark"]["stopped"]["error_type"])
                self.assertIn("evaluation_stopped", report["gate"]["reasons"])
                self.assertFalse(report["gate"]["passed"])

    async def test_standalone_worker_stop_needs_no_framework_import(self):
        env = dict(os.environ, PYTHONPATH="")
        r = ProcessRunner([sys.executable, worker.__file__, "handlers:standalone_quota"], cwd=self.root, env=env)
        async with r:
            report = await evaluate(r, r, [Case("x", {})], lambda o,e: {"ok": True}, repetitions=2, warmups=0)
        self.assertEqual(1, report["benchmark"]["invocations"]["made"])
        self.assertEqual("insufficient_quota", report["benchmark"]["stopped"]["error_type"])
        self.assertNotIn("private-provider-detail", json.dumps(report))

    async def test_normal_errors_do_not_stop_the_evaluation(self):
        async with self.runner("broken") as r:
            report = await evaluate(r, r, [Case("x", {})], lambda o,e: {"ok": True}, repetitions=2, warmups=0)
        self.assertEqual(4, report["benchmark"]["invocations"]["made"])
        self.assertNotIn("evaluation_stopped", report["gate"]["reasons"])
        self.assertNotIn("private-provider-detail", json.dumps(report))
        self.assertEqual("KeyError", report["quality"]["failures"][0]["error_type"])

    async def test_explicit_error_type_policy_stops(self):
        async with self.runner("broken", stop_on_error_types=("KeyError",)) as r:
            report = await evaluate(r, r, [Case("x", {})], lambda o,e: {"ok": True}, repetitions=2, warmups=0)
        self.assertEqual(1, report["benchmark"]["invocations"]["made"])
        self.assertEqual("KeyError", report["benchmark"]["stopped"]["error_type"])

    async def test_worker_timing_is_separate_from_stage_spans(self):
        async with self.runner() as r:
            observation = await r({"value": 7})
            self.assertIsInstance(observation.worker_ms, (int, float))
            self.assertGreaterEqual(observation.worker_ms, 0)
            self.assertEqual(["stage"], [s["name"] for s in observation.spans])
            report = await evaluate(r, r, [Case("x", {"value": 7})], lambda o,e: {"ok": o == 7}, repetitions=1, warmups=0)
        for variant in ("baseline", "candidate"):
            run = report["benchmark"]["cases"][0][variant]["runs"][0]
            self.assertGreaterEqual(run["worker_ms"], 0)
            self.assertIn("latency_ms", run)
            self.assertEqual(["stage"], [s["name"] for s in report["traces"][variant]])

    async def test_timeout_discards_child_and_next_call_restarts(self):
        async with self.runner() as r:
            old_process = r._process
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(r({"value": 1, "delay": 1}), .03)
            self.assertIsNone(r._process)
            self.assertIsNotNone(old_process.returncode)
            self.assertEqual(2, (await asyncio.wait_for(r({"value": 2}), 5)).output)
            self.assertIsNot(r._process, old_process)

    async def test_cancellation_cleans_up_in_flight_request(self):
        async with self.runner() as r:
            started = self.root / "started"
            old_process = r._process
            task = asyncio.create_task(r({"started": str(started), "delay": 60}))
            try:
                async with asyncio.timeout(5):
                    while not started.exists():
                        await asyncio.sleep(.005)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertIsNone(r._process)
            self.assertIsNotNone(old_process.returncode)

    async def test_start_is_idempotent(self):
        async with self.runner() as r:
            process = r._process
            await r.start()
            self.assertIs(process, r._process)

    async def test_concurrent_first_calls_share_one_worker(self):
        r = self.runner()
        try:
            original = asyncio.create_subprocess_exec
            with patch("applied_opt.process.asyncio.create_subprocess_exec", wraps=original) as spawn:
                results = await asyncio.gather(*(r({"value": i}) for i in range(4)))
            self.assertEqual(1, spawn.call_count)
            self.assertEqual(list(range(4)), [o.output for o in results])
        finally:
            await r.close()

    async def test_readiness_failure_reaps_child(self):
        for code, error in (("import time; time.sleep(60)", TimeoutError),
                            (f"import time; print({worker.MARKER!r} + '[]', flush=True); time.sleep(60)", RuntimeError)):
            with self.subTest(code=code):
                r = ProcessRunner([sys.executable, "-c", code], ready_timeout_s=.05 if error is TimeoutError else 5)
                children = []
                original = asyncio.create_subprocess_exec
                async def spawn(*args, **kwargs):
                    p = await original(*args, **kwargs)
                    children.append(p)
                    return p
                try:
                    with patch("applied_opt.process.asyncio.create_subprocess_exec", side_effect=spawn):
                        with self.assertRaises(error):
                            await r.start()
                    self.assertIsNone(r._process)
                    self.assertIsNone(r.ready)
                    self.assertTrue(all(p.returncode is not None for p in children))
                finally:
                    await r.close(.01)

    async def test_invalid_reply_discards_child(self):
        for field in ({"id": 999}, {"worker_ms": math.nan}, {"worker_ms": True}, {"error_type": []}):
            with self.subTest(field=field):
                async with self.runner() as r:
                    old_process = r._process
                    reply = {"id": 1, "output": 1, **field}
                    with patch.object(r, "_read", return_value=reply):
                        with self.assertRaises(RuntimeError):
                            await r({})
                    self.assertIsNone(r._process)
                    self.assertIsNotNone(old_process.returncode)

    async def test_close_timeout_kills_child_without_leaking_timeout(self):
        code = f"import time; print({worker.MARKER!r} + '{{\"ready\":true}}', flush=True); time.sleep(60)"
        r = ProcessRunner([sys.executable, "-c", code])
        await r.start()
        process = r._process
        await r.close(.01)
        self.assertIsNone(r._process)
        self.assertIsNotNone(process.returncode)


class RuntimeTraceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_to_evaluator_to_planner(self):
        from applied_opt.runtime import Pipeline, RunContext, Step
        async def run_step(ctx, deps):
            await asyncio.sleep(.001)
            return 7
        pipeline = Pipeline([Step("a", run_step, concurrency_safe=True, resource="gpu"),
                             Step("b", run_step, depends_on=("a",), resource="gpu")])
        async def adapter(payload):
            result = await pipeline.run(RunContext(payload))
            return Observation(result.outputs["b"], spans=result.trace_dict()["spans"])
        report = await evaluate(adapter, adapter, [Case("x", {})], lambda o,e: {"ok": o == 7},
                                repetitions=1, warmups=0)
        work = work_from_spans(report["traces"]["baseline"],
                               concurrency_safe={s.name: not s.exclusive for s in pipeline.steps})
        self.assertEqual({"a", "b"}, {w.name for w in work})
        self.assertEqual([True, False], [w.concurrency_safe for w in work])
        self.assertEqual(["gpu", "gpu"], [w.resource for w in work])
        self.assertEqual(("a",), work[1].depends_on)
        self.assertGreater(estimate_schedule(work, concurrency=2)["estimated_ms"], 0)
