import json
import tempfile
import unittest
from pathlib import Path

from applied_opt.review import render_markdown, review_path, review_source


class ReviewTests(unittest.TestCase):
    def rules(self, source):
        return {finding["rule_id"] for finding in review_source(source)["findings"]}

    def test_independent_and_dependent_edges_are_distinguished(self):
        independent = "async def f(x):\n    a = await fetch(x)\n    b = await classify(x)\n    return a, b\n"
        dependent = "async def f(x):\n    a = await fetch(x)\n    b = await classify(a)\n    return b\n"
        self.assertIn("AO001", self.rules(independent))
        self.assertNotIn("AO002", self.rules(independent))
        self.assertIn("AO002", self.rules(dependent))
        self.assertNotIn("AO001", self.rules(dependent))

    def test_blocking_alias_and_nested_function_scope(self):
        source = "import time as tm\nasync def f():\n    tm.sleep(1)\n    def g():\n        tm.sleep(2)\n"
        report = review_source(source)
        blocking = [f for f in report["findings"] if f["rule_id"] == "AO003"]
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["line"], 3)

    def test_gather_fanout_and_cleanup_findings(self):
        source = "import asyncio\nasync def f(xs):\n    return await asyncio.gather(*(call(x) for x in xs))\n"
        self.assertTrue({"AO004", "AO008"}.issubset(self.rules(source)))
        bounded = "import asyncio\nasync def f(a,b):\n    return await asyncio.gather(a,b,return_exceptions=True)\n"
        self.assertNotIn("AO004", self.rules(bounded))
        self.assertNotIn("AO008", self.rules(bounded))

    def test_gather_with_cleanup_that_cancels_every_task_is_not_a_lifecycle_finding(self):
        cleaned = (
            "import asyncio\n"
            "async def f():\n"
            "    pending = [asyncio.create_task(a()), asyncio.create_task(b())]\n"
            "    try:\n"
            "        return await asyncio.gather(*pending)\n"
            "    finally:\n"
            "        for task in pending:\n"
            "            if not task.done():\n"
            "                task.cancel()\n"
            "        await asyncio.gather(*pending, return_exceptions=True)\n")
        rules = self.rules(cleaned)
        self.assertNotIn("AO008", rules)
        # A fixed two-task list and the drain in finally start no unbounded fan-out.
        self.assertNotIn("AO004", rules)
        named = ("import asyncio\nasync def f():\n    t1 = asyncio.create_task(a())\n    t2 = asyncio.create_task(b())\n"
                 "    try:\n        return await asyncio.gather(t1, t2)\n    finally:\n        t1.cancel()\n        t2.cancel()\n")
        self.assertNotIn("AO008", self.rules(named))

    def test_partial_cleanup_and_inline_coroutines_keep_the_lifecycle_finding(self):
        partial = ("import asyncio\nasync def f():\n    memory = asyncio.create_task(load())\n"
                   "    try:\n        return await asyncio.gather(fetch(), memory)\n"
                   "    finally:\n        memory.cancel()\n")
        self.assertIn("AO008", self.rules(partial))
        one_of_two = ("import asyncio\nasync def f():\n    t1 = asyncio.create_task(a())\n    t2 = asyncio.create_task(b())\n"
                      "    try:\n        return await asyncio.gather(t1, t2)\n    finally:\n        t1.cancel()\n")
        self.assertIn("AO008", self.rules(one_of_two))

    def test_semaphore_wrapper_and_fixed_list_bound_the_fanout(self):
        gated = ("import asyncio\nasync def f(items):\n    gate = asyncio.Semaphore(4)\n"
                 "    async def bounded(item):\n        async with gate:\n            return await work(item)\n"
                 "    return await asyncio.gather(*(bounded(i) for i in items), return_exceptions=True)\n")
        self.assertNotIn("AO004", self.rules(gated))
        ungated = ("import asyncio\nasync def f(items):\n    async def call(item):\n        return await work(item)\n"
                   "    return await asyncio.gather(*(call(i) for i in items), return_exceptions=True)\n")
        self.assertIn("AO004", self.rules(ungated))
        grown = ("import asyncio\nasync def f(items):\n    calls = [first()]\n    for item in items:\n"
                 "        calls.append(work(item))\n    return await asyncio.gather(*calls, return_exceptions=True)\n")
        self.assertIn("AO004", self.rules(grown))

    def test_shadowed_library_names_do_not_get_library_findings(self):
        sources = [
            "import time\nasync def f(time):\n    time.sleep(1)\n",
            "import asyncio\nasync def f(asyncio):\n    return await asyncio.gather(a,b)\n",
            "from asyncio import gather\nasync def f(gather):\n    return await gather(a,b)\n",
            "import time\ntime = CustomClock()\nasync def f():\n    time.sleep(1)\n",
            "async def f():\n    return await gather(a,b)\n",
        ]
        for source in sources:
            self.assertFalse({"AO003", "AO008"} & self.rules(source))

    def test_retry_evidence_does_not_claim_proven_multiplication(self):
        source = "client = Client(max_retries=2)\nasync def f():\n    for attempt in range(3):\n        try:\n            return await client.generate()\n        except TimeoutError:\n            continue\n"
        report = review_source(source)
        finding = next(f for f in report["findings"] if f["rule_id"] == "AO005")
        self.assertEqual(finding["confidence"], "medium")
        self.assertIn("if they apply", finding["why"])

    def test_route_and_repeat_heuristics_are_review_only(self):
        source = "async def f(text):\n    a = await final_review(text)\n    b = await final_review(text)\n    return a,b\n"
        report = review_source(source)
        self.assertTrue({"AO006", "AO009"}.issubset(self.rules(source)))
        self.assertTrue(all(not f["autofix"]["available"] for f in report["findings"]))

    def test_cache_scope_and_coroutine_lifecycle(self):
        source = "from functools import lru_cache\n@lru_cache()\nasync def analyze_note(text):\n    return await client.generate(text)\n"
        self.assertTrue({"AO007", "AO010"}.issubset(self.rules(source)))
        public_pure = "from functools import lru_cache\n@lru_cache()\ndef square(x):\n    return x*x\n"
        self.assertNotIn("AO007", self.rules(public_pure))

    def test_source_is_never_executed_and_literals_are_not_exported(self):
        secret = "private_token_DO_NOT_EXPORT_739"
        source = f"raise RuntimeError('{secret}')\nasync def f():\n    a=await fetch('{secret}')\n    b=await classify('{secret}')\n"
        report = review_source(source)
        self.assertNotIn(secret, json.dumps(report))
        self.assertNotIn(secret, render_markdown(report))
        self.assertIn("AO001", {f["rule_id"] for f in report["findings"]})

    def test_invalid_context_is_reported_without_snippet(self):
        report = review_source("return 'private_literal'\n", "bad.py")
        self.assertEqual(report["files"][0]["status"], "error")
        self.assertNotIn("private_literal", json.dumps(report))

    def test_path_walk_excludes_hidden_vendor_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = "async def f():\n    a=await fetch()\n    b=await classify()\n"
            (root / "app.py").write_text(source)
            for name in (".secret", "vendor", "generated", "node_modules"):
                (root / name).mkdir()
                (root / name / "hidden.py").write_text(source)
            (root / "link.py").symlink_to(root / "app.py")
            report = review_path(root)
            self.assertEqual(report["summary"]["files_scanned"], 1)
            self.assertEqual(report["files"][0]["path"], "app.py")
            self.assertGreaterEqual(report["skipped_entries"], 5)

    def test_scan_config_and_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "skip.py").write_text("pass\n")
            report = review_path(root, {"exclude": ["skip.py"]})
            self.assertEqual(report["summary"]["files_scanned"], 0)
            self.assertEqual(review_path(root / "missing.py")["summary"]["files_with_errors"], 1)
        with self.assertRaises(ValueError):
            review_path(".", {"exclude": "vendor/*"})

    def test_explicit_hidden_ancestor_and_symlink_ancestor_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".private").mkdir()
            (root / ".private" / "file.py").write_text("pass\n")
            self.assertEqual(review_path(root / ".private" / "file.py")["summary"]["files_with_errors"], 1)
            (root / "real").mkdir()
            (root / "real" / "file.py").write_text("pass\n")
            (root / "linked").symlink_to(root / "real", target_is_directory=True)
            self.assertEqual(review_path(root / "linked" / "file.py")["summary"]["files_with_errors"], 1)


if __name__ == "__main__":
    unittest.main()
