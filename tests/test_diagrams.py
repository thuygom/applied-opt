import json
import unittest
import xml.etree.ElementTree as ET

from applied_opt.diagrams import (
    compile_graph, graph_from_plan, graph_from_report, graph_from_source,
    normalize_graph,
)
from applied_opt.planning import Work


class DiagramTests(unittest.TestCase):
    def graph(self):
        return graph_from_plan([
            Work("context", 20, concurrency_safe=True),
            Work("classify", 30, concurrency_safe=True),
            Work("generate", 50, depends_on=("context", "classify")),
        ])

    def test_plan_converts_work_and_mapping_and_marks_estimates(self):
        graph = self.graph()
        self.assertEqual(graph["kind"], "declared")
        self.assertEqual(len(graph["edges"]), 2)
        self.assertIn("estimate 20.0 ms", compile_graph(graph))
        mapping = graph_from_plan([{"name": "a", "duration_ms": None}])
        self.assertIsNone(mapping["nodes"][0]["duration_ms"])

    def test_measured_trace_preserves_only_declared_dependencies(self):
        report = {"traces": {"candidate": [
            {"name": "a", "start_ms": 0, "end_ms": 10, "status": "completed"},
            {"name": "b", "start_ms": 10, "end_ms": 15, "status": "completed"},
            {"name": "c", "duration_ms": 5, "depends_on": ["a"], "status": "completed"},
        ]}}
        graph = graph_from_report(report)
        self.assertEqual(graph["kind"], "measured")
        self.assertEqual(graph["nodes"][0]["duration_ms"], 10)
        self.assertEqual(graph["edges"], [{"source": "a", "target": "c", "kind": "dependency"}])
        self.assertIn("not p95", graph["caveat"])

    def test_source_distinguishes_lexical_order_from_data_dependency(self):
        source = "async def note(x):\n    a = await fetch(x)\n    b = await classify(x)\n    c = await generate(a)\n    return c\n"
        graph = graph_from_source(source)
        self.assertEqual(graph["kind"], "inferred")
        self.assertEqual(graph["mode"], "static_estimate")
        self.assertTrue(all(node["duration_ms"] is None for node in graph["nodes"]))
        self.assertEqual([edge["kind"] for edge in graph["edges"]], ["order", "dependency", "order"])

    def test_source_reassignment_invalidates_old_producer_and_never_executes(self):
        source = "raise RuntimeError('secret_984')\nasync def f(x):\n    a = await fetch(x)\n    a = x\n    b = await analyze(a)\n"
        graph = graph_from_source(source)
        self.assertFalse(any(edge["kind"] == "dependency" for edge in graph["edges"]))
        self.assertNotIn("secret_984", json.dumps(graph))

    def test_nested_control_flow_is_not_fabricated(self):
        source = "async def f(x):\n    for item in x:\n        a = await fetch(item)\n    if x:\n        b = await analyze(x)\n"
        graph = graph_from_source(source)
        self.assertEqual(graph["nodes"], [])
        self.assertIn("No supported stages", compile_graph(graph, "svg"))

    def test_all_renderers_escape_directives_and_ids(self):
        label = '\"]\n!include https://evil.invalid/a\n@enduml\n<script onload="x"> %%{init: bad}%%'
        graph = {"kind": "declared", "title": label,
                 "nodes": [{"id": label, "label": label}], "edges": []}
        mermaid = compile_graph(graph, "mermaid")
        plantuml = compile_graph(graph, "plantuml")
        svg = compile_graph(graph, "svg")
        self.assertTrue(mermaid.startswith("flowchart TD\n  n0["))
        self.assertNotIn("%%{", mermaid)
        self.assertNotIn("<script", mermaid)
        self.assertNotIn("!include", plantuml)
        self.assertEqual(plantuml.count("@enduml"), 1)
        self.assertNotIn("<script", svg)
        root = ET.fromstring(svg)
        self.assertEqual(root.attrib["role"], "img")
        for element in root.iter():
            self.assertFalse(any(key.startswith("on") or key in {"href", "src"} for key in element.attrib))

    def test_svg_is_deterministic_accessible_and_locally_complete(self):
        a = compile_graph(self.graph(), "svg")
        self.assertEqual(a, compile_graph(self.graph(), "svg"))
        tree = ET.fromstring(a)
        self.assertEqual(tree.attrib["aria-labelledby"], "graph-title graph-desc")
        ns = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual(len(tree.findall("s:g", ns)), 3)
        self.assertNotIn("http://", a.replace("http://www.w3.org/2000/svg", ""))

    def test_cycle_unknown_reference_duplicate_and_invalid_duration_rejected(self):
        for graph in (
            {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "a"}]},
            {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "b"}]},
            {"nodes": [{"id": "a"}, {"id": "a"}]},
        ):
            with self.assertRaises(ValueError):
                normalize_graph(graph)
        for duration in (True, float("nan"), float("inf"), -1, 10**1000):
            with self.assertRaises(ValueError):
                normalize_graph({"nodes": [{"id": "a", "duration_ms": duration}]})

    def test_bounds_and_bad_sources(self):
        with self.assertRaises(ValueError):
            normalize_graph({"nodes": [{"id": str(i)} for i in range(201)]})
        with self.assertRaises(ValueError):
            graph_from_source("return 'private'\n")
        with self.assertRaises(ValueError):
            graph_from_source("#" * 1_000_001)
        with self.assertRaises(ValueError):
            graph_from_report({"traces": {"candidate": []}}, "unknown")
        with self.assertRaises(ValueError):
            compile_graph(self.graph(), "html")


if __name__ == "__main__":
    unittest.main()
