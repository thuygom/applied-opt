import copy
import unittest

from applied_opt.demo import FIXTURES, _judge, _run
from applied_opt.diagrams import graph_from_report


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_variants_preserve_output_and_export_valid_trace_dependencies(self):
        # Exercise the baseline-only serialized review edges that previously
        # raised after successful execution, invalidating every baseline run.
        fixture = FIXTURES[1]
        traces = {}
        for variant, optimized in (("baseline", False), ("candidate", True)):
            with self.subTest(variant=variant):
                observation = await _run({"fixture": copy.deepcopy(fixture)}, optimized=optimized)
                self.assertTrue(all(_judge(observation.output, fixture).values()))
                self.assertTrue(observation.spans)
                traces[variant] = observation.spans
                spans = {span["name"]: span for span in observation.spans}
                self.assertEqual("summary_review" in spans["task_review"]["depends_on"], not optimized)
                self.assertEqual("task_review" in spans["schedule_review"]["depends_on"], not optimized)
                for span in observation.spans:
                    self.assertIsInstance(span["depends_on"], list)
                    for dependency in span["depends_on"]:
                        predecessor = spans[dependency]
                        self.assertLessEqual(
                            predecessor["start_ms"] + predecessor["duration_ms"],
                            span["start_ms"] + 0.001,
                        )
                graph = graph_from_report({"traces": traces}, variant)
                self.assertTrue(graph["nodes"])
