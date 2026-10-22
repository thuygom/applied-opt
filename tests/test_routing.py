import asyncio
import unittest

from applied_opt.routing import Capability, Decision, Router


def capabilities():
    return [
        Capability("summary", "Summarize without inventing facts.", {"type": "string"}, mandatory=True),
        Capability("tasks", "Extract explicit tasks.", {"type": "array"}, reviewers=("check_tasks",)),
        Capability("schedule", "Preserve explicit dates.", {"type": "array"}, reviewers=("check_dates",)),
    ]


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_decision_controls_prompt_schema_and_reviewers(self):
        calls = 0

        async def decide(payload, names):
            nonlocal calls
            calls += 1
            return Decision(("summary",), 0.99, names)

        route = await Router(capabilities()).route({"text": "A private observation."}, decide)
        self.assertEqual(calls, 1)
        self.assertEqual(route.enabled, ("summary",))
        self.assertNotIn("Extract explicit tasks", route.prompt)
        self.assertEqual(set(route.response_schema["properties"]), {"summary"})
        self.assertEqual(route.reviewers, ())
        self.assertFalse(route.fallback)

    async def test_explicit_evidence_and_forced_capabilities_cannot_be_omitted(self):
        async def decide(payload, names):
            return Decision((), 0.999, names)

        route = await Router(capabilities()).route(
            {"text": "Tomorrow at 3. Bring the report."}, decide,
            evidence=("schedule",), forced=("tasks",),
        )
        self.assertEqual(set(route.enabled), {"summary", "tasks", "schedule"})
        self.assertIn("check_dates", route.reviewers)

    async def test_uncertain_incomplete_unknown_and_malformed_decisions_fall_back(self):
        decisions = [
            Decision(("summary",), 0.1, ("summary", "tasks", "schedule")),
            Decision(("summary",), 0.99, ("summary",)),
            Decision(("unknown",), 1, ("summary", "tasks", "schedule")),
            {"selected": "summary", "confidence": 1, "considered": ["summary", "tasks", "schedule"]},
            Decision(("summary",), float("nan"), ("summary", "tasks", "schedule")),
            Decision(("summary",), True, ("summary", "tasks", "schedule")),
        ]
        for decision in decisions:
            async def decide(payload, names):
                return decision
            route = await Router(capabilities()).route({"text": "x"}, decide)
            self.assertTrue(route.fallback)
            self.assertEqual(len(route.enabled), 3)

    async def test_missing_error_timeout_fall_back_but_cancellation_propagates(self):
        router = Router(capabilities(), timeout_s=0.01)
        missing = await router.route({})
        self.assertEqual(missing.reason, "missing_decider")

        async def fail(payload, names):
            raise RuntimeError("private payload must not be returned")

        failed = await router.route({}, fail)
        self.assertEqual(failed.reason, "decision_error")
        self.assertNotIn("private payload", repr(failed))
        cleaned = asyncio.Event()

        async def slow(payload, names):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        timed_out = await router.route({}, slow)
        self.assertEqual(timed_out.reason, "timeout")
        self.assertTrue(cleaned.is_set())

        async def cancel(payload, names):
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await router.route({}, cancel)

    async def test_capability_dependency_closure_keeps_its_contract(self):
        router = Router([
            Capability("summary", "Summarize", {"type": "string"}, True),
            Capability("dates", "Find dates", {"type": "array"}),
            Capability("schedule", "Calendar", {"type": "array"}, requires=("dates",)),
        ])

        async def decide(payload, names):
            return Decision(("schedule",), 1, names)

        route = await router.route({}, decide)
        self.assertEqual(route.enabled, ("summary", "dates", "schedule"))
        with self.assertRaises(ValueError):
            await router.route({}, decide, forced=("nonexistent",))


if __name__ == "__main__":
    unittest.main()
