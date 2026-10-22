import asyncio
import dataclasses
import json
import unittest

from applied_opt.cache import CachePolicy, MemoryCache
from applied_opt.routing import Capability, Decision, Router
from applied_opt.runtime import (
    Pipeline, RetryPolicy, RoutingDependencyError, RunContext,
    Step, StepExecutionError, gather_strict,
)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_steps_overlap_and_dependents_start_without_wave_barrier(self):
        child_ran = asyncio.Event()
        slow_started = asyncio.Event()

        async def slow(context, dependencies):
            slow_started.set()
            await child_ran.wait()
            return "slow"

        async def fast(context, dependencies):
            await slow_started.wait()
            return "fast"

        async def child(context, dependencies):
            self.assertEqual(dict(dependencies), {"fast": "fast"})
            child_ran.set()
            return "child"

        result = await Pipeline([
            Step("slow", slow, concurrency_safe=True),
            Step("fast", fast, concurrency_safe=True),
            Step("child", child, depends_on=("fast",), concurrency_safe=True),
        ], max_concurrency=2).run(RunContext({}), timeout_s=1)
        self.assertEqual(result.outputs["child"], "child")
        spans = {span.name: span for span in result.spans}
        self.assertLess(spans["child"].start_ms, spans["slow"].end_ms)

    async def test_unknown_concurrency_and_side_effects_are_exclusive(self):
        running = 0
        maximum = 0

        async def work(context, dependencies):
            nonlocal running, maximum
            running += 1
            maximum = max(maximum, running)
            await asyncio.sleep(0.001)
            running -= 1

        await Pipeline([Step("a", work), Step("b", work),
                        Step("c", work, concurrency_safe=True, side_effects=True)]).run(RunContext({}))
        self.assertEqual(maximum, 1)

    async def test_resource_limit_allows_other_resources_to_progress(self):
        cpu_ran = asyncio.Event()
        gpu_running = 0
        max_gpu = 0

        async def gpu(context, dependencies):
            nonlocal gpu_running, max_gpu
            gpu_running += 1
            max_gpu = max(max_gpu, gpu_running)
            await cpu_ran.wait()
            await asyncio.sleep(0)
            gpu_running -= 1

        async def cpu(context, dependencies):
            cpu_ran.set()

        await Pipeline([
            Step("gpu1", gpu, concurrency_safe=True, resource="gpu"),
            Step("gpu2", gpu, concurrency_safe=True, resource="gpu"),
            Step("cpu", cpu, concurrency_safe=True, resource="cpu"),
        ], max_concurrency=3, resource_limits={"gpu": 1}).run(RunContext({}), timeout_s=1)
        self.assertEqual(max_gpu, 1)

    async def test_failure_cancels_and_drains_siblings(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def waiting(context, dependencies):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def fail(context, dependencies):
            await started.wait()
            raise ValueError("secret input")

        with self.assertRaises(StepExecutionError) as caught:
            await Pipeline([Step("a", waiting, concurrency_safe=True),
                            Step("b", fail, concurrency_safe=True)]).run(RunContext({}), timeout_s=1)
        self.assertTrue(stopped.is_set())
        self.assertNotIn("secret input", str(caught.exception))

    async def test_request_deadline_bounds_retries_and_waiting(self):
        stopped = asyncio.Event()
        calls = 0

        async def waiting(context, dependencies):
            nonlocal calls
            calls += 1
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        pipeline = Pipeline([Step("a", waiting, idempotent=True,
                                  retry=RetryPolicy(attempts=5), timeout_s=1)])
        with self.assertRaises(TimeoutError):
            await pipeline.run(RunContext({}), timeout_s=0.01)
        self.assertTrue(stopped.is_set())
        self.assertEqual(calls, 1)

    async def test_retries_are_explicit_and_counted(self):
        calls = 0

        async def transient(context, dependencies):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError
            return "ok"

        with self.assertRaises(ValueError):
            Step("unsafe", transient, retry=RetryPolicy(attempts=2))
        result = await Pipeline([Step("safe", transient, idempotent=True,
                                      retry=RetryPolicy(attempts=2))]).run(RunContext({}))
        self.assertEqual(result.outputs["safe"], "ok")
        self.assertEqual(result.spans[0].attempts, 2)

    async def test_route_skips_optional_steps_and_rejects_broken_dependencies(self):
        router = Router([Capability("summary", "Summary", {"type": "string"}, True),
                         Capability("schedule", "Schedule", {"type": "array"})])

        async def decide(payload, names):
            return Decision(("summary",), 1, names)

        route = await router.route({}, decide)
        executed = []

        async def work(context, deps):
            executed.append("work")
            return []

        result = await Pipeline([Step("summary", work, capability="summary"),
                                 Step("schedule", work, capability="schedule")]).run(RunContext({}, route=route))
        self.assertEqual(len(executed), 1)
        self.assertEqual(result.skipped, ("schedule",))
        with self.assertRaises(RoutingDependencyError):
            await Pipeline([Step("summary", work, capability="summary", depends_on=("schedule",)),
                            Step("schedule", work, capability="schedule")]).run(RunContext({}, route=route))
        self.assertEqual(len(executed), 1)

    async def test_cache_is_scoped_and_missing_scope_bypasses_without_content_loss(self):
        calls = 0

        async def work(context, dependencies):
            nonlocal calls
            calls += 1
            return {"result": [calls]}

        pipeline = Pipeline([Step("lookup", work, cache=CachePolicy(deterministic=True, read_only=True))])
        context = RunContext({"query": "private"}, "tenant", "subject", "auth", "data", "model", "prompt", "2026-01-01")
        first = await pipeline.run(context)
        first.outputs["lookup"]["result"].append(100)
        second = await pipeline.run(context)
        self.assertEqual(second.outputs["lookup"], {"result": [1]})
        self.assertEqual(second.spans[0].cache_status, "hit")
        third = await pipeline.run(dataclasses.replace(context, subject_id="someone-else"))
        self.assertEqual(third.outputs["lookup"], {"result": [2]})
        unscoped = await pipeline.run(RunContext({"query": "private"}))
        self.assertEqual(unscoped.spans[0].cache_status, "bypass_unsafe_key")
        self.assertEqual(unscoped.outputs["lookup"], {"result": [3]})
        self.assertNotIn("private", json.dumps(second.trace_dict()))

    async def test_gather_strict_preserves_exception_type_and_drains(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def waiting():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def failing():
            await started.wait()
            raise LookupError("test error")

        with self.assertRaises(LookupError):
            await gather_strict(waiting(), failing())
        self.assertTrue(stopped.is_set())
        self.assertEqual(await gather_strict(asyncio.sleep(0, result=1), asyncio.sleep(0, result=2)), [1, 2])

    async def test_distinct_pipelines_do_not_collide_in_a_shared_cache(self):
        cache = MemoryCache()
        policy = CachePolicy(deterministic=True, read_only=True)

        async def first(context, dependencies):
            return "first"

        async def second(context, dependencies):
            return "second"

        a = Pipeline([Step("lookup", first, cache=policy)], cache=cache)
        b = Pipeline([Step("lookup", second, cache=policy)], cache=cache)
        ctx = RunContext({}, "t", "s", "a", "d", "m", "p", "2026-01-01")
        self.assertEqual((await a.run(ctx)).outputs["lookup"], "first")
        self.assertEqual((await b.run(ctx)).outputs["lookup"], "second")

    async def test_dag_and_policy_validation(self):
        async def work(context, dependencies):
            return None

        with self.assertRaises(ValueError):
            Pipeline([Step("a", work, depends_on=("b",)), Step("b", work, depends_on=("a",))])
        with self.assertRaises(ValueError):
            Pipeline([Step("a", work, depends_on=("missing",))])
        with self.assertRaises(ValueError):
            Step("a", work, side_effects=True, cache=CachePolicy(deterministic=True, read_only=True))


if __name__ == "__main__":
    unittest.main()
