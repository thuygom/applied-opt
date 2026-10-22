import json
import sys
import tempfile
import unittest
from pathlib import Path

from applied_opt import worker
from applied_opt.evaluation import Case, evaluate
from applied_opt.process import ProcessRunner

HANDLERS = '''
import asyncio
print("application log line on stdout")

def double(payload):
    print("another log line")
    return {"output": {"value": payload["x"] * 2}, "calls": 1, "spans": [{"name": "work", "duration_ms": 1.0}]}

async def revision_b(payload):
    await asyncio.sleep(0)
    return {"value": payload["x"] * 2}

def broken(payload):
    raise KeyError("private detail that must not leave the worker")
'''


class ProcessRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "handlers.py").write_text(HANDLERS)
        self.stdout = self.root / "stdout.log"

    def tearDown(self):
        self.directory.cleanup()

    def runner(self, target):
        return ProcessRunner([sys.executable, worker.__file__, f"handlers:{target}"], cwd=self.root,
                             stdout_log=self.stdout)

    async def test_two_revisions_in_separate_processes_go_through_the_gate(self):
        judge = lambda output, expected: {"value": isinstance(output, dict) and output.get("value") == expected["value"]}
        async with self.runner("double") as baseline, self.runner("revision_b") as candidate:
            self.assertEqual("handlers:double", baseline.ready["handler"])
            report = await evaluate(baseline, candidate, [Case("x", {"x": 21}, {"value": 42})], judge,
                                    repetitions=2, warmups=0, max_invocations=4)
        self.assertTrue(report["gate"]["passed"] or report["gate"]["reasons"] == ["p95_regression"])
        self.assertEqual(4, report["quality"]["passed"] + report["quality"]["baseline_passed"])
        self.assertEqual({"planned": 4, "made": 4, "budget": 4}, report["benchmark"]["invocations"])
        self.assertIn("application log line", self.stdout.read_text())

    async def test_handler_error_reports_only_its_type(self):
        judge = lambda output, expected: {"value": output == expected}
        async with self.runner("broken") as broken, self.runner("revision_b") as candidate:
            report = await evaluate(broken, candidate, [Case("x", {"x": 1}, {"value": 2})], judge,
                                    repetitions=1, warmups=0)
        failure = report["quality"]["failures"][0]
        self.assertEqual(("baseline", "KeyError"), (failure["variant"], failure["error_type"]))
        self.assertNotIn("private detail", json.dumps(report))

    def test_worker_serves_marked_replies_and_ignores_blank_lines(self):
        import io
        source = io.StringIO('{"id": 1, "payload": {"x": 2}}\n\n')
        sink = io.StringIO()
        worker.serve(lambda payload: payload["x"] + 1, stdin=source, stdout=sink)
        lines = [json.loads(line[len(worker.MARKER):]) for line in sink.getvalue().splitlines()]
        self.assertTrue(lines[0]["ready"])
        self.assertEqual((1, 3), (lines[1]["id"], lines[1]["output"]))


if __name__ == "__main__":
    unittest.main()
