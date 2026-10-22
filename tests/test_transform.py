import asyncio
import unittest

from applied_opt.transform import propose_parallel_patch


BASE = '''"""Keep this module docstring."""
from __future__ import annotations

async def pipeline(text: str, user_id: str):
    first = await fetch_context(user_id)  # preserve this comment
    second = await detect_typos(text)
    return first, second
'''


class TransformTests(unittest.IsolatedAsyncioTestCase):
    def patch(self, source=BASE):
        return propose_parallel_patch(source, ["fetch_context", "detect_typos"], "pipeline.py")

    async def test_patch_preserves_values_and_actually_overlaps_calls(self):
        events = []
        active = 0
        maximum = 0

        async def fetch_context(value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            events.append("context_start")
            await asyncio.sleep(0.01)
            events.append("context_end")
            active -= 1
            return {"user": value}

        async def detect_typos(value):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            events.append("typos_start")
            await asyncio.sleep(0.01)
            events.append("typos_end")
            active -= 1
            return [value]

        baseline = {"fetch_context": fetch_context, "detect_typos": detect_typos}
        exec(BASE, baseline)
        before = await baseline["pipeline"]("synthetic", "user-1")
        self.assertEqual(maximum, 1)
        events.clear()
        maximum = 0
        proposal = self.patch()
        self.assertEqual(proposal["applied"], 1)
        candidate = {"fetch_context": fetch_context, "detect_typos": detect_typos}
        exec(proposal["source"], candidate)
        after = await candidate["pipeline"]("synthetic", "user-1")
        self.assertEqual(before, after)
        self.assertEqual(maximum, 2)
        self.assertLess(events.index("typos_start"), events.index("context_end"))
        self.assertEqual(candidate["__doc__"], "Keep this module docstring.")
        self.assertIn("# preserve this comment", proposal["source"])

    async def test_failure_cancels_siblings_and_keeps_exception_type(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fetch_context(value):
            await started.wait()
            raise ValueError("synthetic failure")

        async def detect_typos(value):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        namespace = {"fetch_context": fetch_context, "detect_typos": detect_typos}
        exec(self.patch()["source"], namespace)
        with self.assertRaisesRegex(ValueError, "synthetic failure"):
            await namespace["pipeline"]("x", "u")
        self.assertTrue(cancelled.is_set())

    def test_empty_allowlist_never_modifies_source(self):
        result = propose_parallel_patch(BASE)
        self.assertFalse(result["changed"])
        self.assertEqual(result["source"], BASE)

    def test_true_dependency_is_refused(self):
        source = "async def f(x: str):\n    a = await fetch_context(x)\n    b = await detect_typos(a)\n    return b\n"
        result = self.patch(source)
        self.assertFalse(result["changed"])
        self.assertIn("dependency", result["refusals"][0]["reason"])

    def test_mutable_and_dynamic_arguments_are_refused(self):
        for signature, expression in (("x: dict", "x"), ("x", "x"), ("x: str", "x.strip()"), ("x: str", "x[0]")):
            source = f"async def f({signature}):\n    a = await fetch_context({expression})\n    b = await detect_typos({expression})\n"
            self.assertFalse(self.patch(source)["changed"])

    def test_mutation_invalidates_argument_annotation(self):
        source = "async def f(x: str):\n    x = {}\n    a = await fetch_context(x)\n    b = await detect_typos(x)\n"
        self.assertFalse(self.patch(source)["changed"])

    def test_nested_control_flow_and_methods_are_not_patched(self):
        nested = "async def f(x: str):\n    if x:\n        a = await fetch_context(x)\n        b = await detect_typos(x)\n"
        method = "async def f(x: str):\n    a = await client.fetch_context(x)\n    b = await detect_typos(x)\n"
        self.assertFalse(self.patch(nested)["changed"])
        self.assertFalse(self.patch(method)["changed"])

    def test_local_callable_shadow_is_refused(self):
        source = "async def f(fetch_context, x: str):\n    a = await fetch_context(x)\n    b = await detect_typos(x)\n"
        self.assertFalse(self.patch(source)["changed"])

    def test_source_headers_unicode_and_alias_collisions(self):
        source = '#!/usr/bin/env python3\n# coding: utf-8\n"""한국어 설명."""\nfrom __future__ import annotations\n_applied_opt_gather = 42\nasync def f():\n    가 = await fetch_context("합성")\n    나 = await detect_typos("예시")\n    return 가, 나\n'
        result = self.patch(source)
        self.assertTrue(result["changed"])
        self.assertTrue(result["source"].startswith("#!/usr/bin/env python3\n# coding: utf-8\n"))
        self.assertIn("as _applied_opt_gather_1", result["source"])
        compile(result["source"], "test", "exec")

    def test_comment_gap_semicolon_and_multiline_are_refused(self):
        for middle in ("\n    # explanation\n", "; ", "\n\n"):
            source = f"async def f():\n    a = await fetch_context(){middle}    b = await detect_typos()\n"
            self.assertFalse(self.patch(source)["changed"])
        source = "async def f():\n    a = await fetch_context(\n        'x'\n    )\n    b = await detect_typos('y')\n"
        self.assertFalse(self.patch(source)["changed"])

    def test_only_two_calls_per_parallel_batch(self):
        source = "async def f():\n    a = await fetch_context()\n    b = await detect_typos()\n    c = await fetch_context()\n    return a,b,c\n"
        result = self.patch(source)
        self.assertEqual(result["applied"], 1)
        self.assertIn("c = await fetch_context()", result["source"])
        self.assertEqual(result["max_parallel_per_batch"], 2)


if __name__ == "__main__":
    unittest.main()
