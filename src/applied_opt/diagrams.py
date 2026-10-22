"""Compile bounded pipeline metadata into local Mermaid, PlantUML and SVG.

No remote renderer, HTML, code execution, or model call is used. A common graph
distinguishes measured traces, declared plans, and inferred lexical structure.
"""

from __future__ import annotations

import ast
import dataclasses
import math
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .review import MAX_SOURCE_BYTES, _await_assignment, _call_name, _loads


MAX_NODES = 200
MAX_EDGES = 1000
MAX_LABEL = 160
_CAVEATS = {
    "measured": "One selected trace; stage durations are measured observations, not p95 or a production SLA. Edges use declared dependencies only.",
    "declared": "Declared dependencies and supplied duration estimates; this is not an execution measurement.",
    "inferred": "Static lexical estimate: direct awaited assignments only. Order edges show source order, not proven runtime dependency or concurrency safety. Branches, loops and cross-function calls are not reconstructed.",
}


def _text(value: Any, limit: int = MAX_LABEL) -> str:
    if not isinstance(value, str):
        raise ValueError("Graph labels must be strings")
    value = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    return value[:limit] + ("…" if len(value) > limit else "")


def _duration(value: Any) -> float | None:
    if value is None:
        return None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("duration_ms must be a finite nonnegative number or null")
    return float(value)


def normalize_graph(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a DAG, bound labels and discard unsupported metadata.

    Node IDs remain stable in normalized JSON; compilers remap them to fixed
    internal identifiers so user strings cannot become diagram directives.
    """
    if not isinstance(graph, Mapping):
        raise ValueError("graph must be an object")
    kind = graph.get("kind", "declared")
    if not isinstance(kind, str) or kind not in _CAVEATS:
        raise ValueError("graph kind must be measured, declared or inferred")
    raw_nodes, raw_edges = graph.get("nodes"), graph.get("edges", [])
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES:
        raise ValueError("graph requires a list of at most 200 nodes")
    if not isinstance(raw_edges, list) or len(raw_edges) > MAX_EDGES:
        raise ValueError("graph edges must be a list of at most 1000 items")
    nodes, ids = [], set()
    for raw in raw_nodes:
        if not isinstance(raw, Mapping):
            raise ValueError("each node must be an object")
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            raise ValueError("node IDs must be nonempty strings of at most 256 characters")
        if identifier in ids:
            raise ValueError("duplicate graph node ID")
        ids.add(identifier)
        node = {"id": identifier, "label": _text(raw.get("label", identifier)),
                "duration_ms": _duration(raw.get("duration_ms")),
                "status": _text(raw.get("status", "unknown"), 32)}
        if raw.get("resource") is not None:
            node["resource"] = _text(raw["resource"], 64)
        nodes.append(node)
    index = {node["id"]: i for i, node in enumerate(nodes)}
    edges, seen = [], set()
    for raw in raw_edges:
        if not isinstance(raw, Mapping):
            raise ValueError("each edge must be an object")
        source, target = raw.get("source"), raw.get("target")
        if not isinstance(source, str) or not isinstance(target, str) or source not in ids or target not in ids:
            raise ValueError("edge references an unknown node")
        edge_kind = raw.get("kind", "dependency")
        if not isinstance(edge_kind, str) or edge_kind not in {"dependency", "order"}:
            raise ValueError("edge kind must be dependency or order")
        key = source, target, edge_kind
        if key not in seen:
            seen.add(key)
            edges.append({"source": source, "target": target, "kind": edge_kind})
    edges.sort(key=lambda edge: (index[edge["source"]], index[edge["target"]], edge["kind"]))
    normalized = {"schema_version": "1.0", "kind": kind,
                  "title": _text(graph.get("title", "Applied Opt flow")),
                  "nodes": nodes, "edges": edges,
                  "caveat": _CAVEATS[kind]}
    if kind == "inferred":
        normalized["mode"] = "static_estimate"
    _depths(normalized)  # Reject cycles before any renderer gets the input.
    return normalized


def _depths(graph: Mapping[str, Any]) -> dict[str, int]:
    incoming = {node["id"]: 0 for node in graph["nodes"]}
    children: dict[str, list[str]] = defaultdict(list)
    for edge in graph["edges"]:
        incoming[edge["target"]] += 1
        children[edge["source"]].append(edge["target"])
    depth = {node: 0 for node in incoming}
    ready = [node for node in incoming if incoming[node] == 0]
    visited = 0
    while ready:
        node = ready.pop(0)
        visited += 1
        for target in children[node]:
            depth[target] = max(depth[target], depth[node] + 1)
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(incoming):
        raise ValueError("graph contains a cycle")
    return depth


def graph_from_report(report: Mapping[str, Any], variant: str = "candidate") -> dict[str, Any]:
    if not isinstance(variant, str) or variant not in {"baseline", "candidate"}:
        raise ValueError("variant must be baseline or candidate")
    if not isinstance(report, Mapping) or not isinstance(report.get("traces"), Mapping):
        raise ValueError("report must contain traces")
    spans = report["traces"].get(variant)
    if not isinstance(spans, list):
        raise ValueError("selected trace must be a list")
    if len(spans) > MAX_NODES:
        raise ValueError("trace exceeds 200 nodes")
    nodes, edges = [], []
    for span in spans:
        if not isinstance(span, Mapping) or not isinstance(span.get("name"), str):
            raise ValueError("trace spans require a name")
        duration = span.get("duration_ms")
        if duration is None and "start_ms" in span and "end_ms" in span:
            start, end = _duration(span["start_ms"]), _duration(span["end_ms"])
            if start is None or end is None or end < start:
                raise ValueError("trace interval must have end_ms >= start_ms")
            duration = end - start
        nodes.append({"id": span["name"], "label": span["name"],
                      "duration_ms": duration, "status": span.get("status", "unknown"),
                      "resource": span.get("resource")})
        dependencies = span.get("depends_on", [])
        if not isinstance(dependencies, (list, tuple)) or any(not isinstance(name, str) for name in dependencies):
            raise ValueError("span depends_on must be a list of node names")
        if len(edges) + len(dependencies) > MAX_EDGES:
            raise ValueError("trace exceeds 1000 dependency edges")
        edges.extend({"source": name, "target": span["name"], "kind": "dependency"} for name in dependencies)
    project = _text(report.get("project", "Applied Opt"), 100)
    return normalize_graph({"kind": "measured", "title": f"{project} · {variant}", "nodes": nodes, "edges": edges})


def graph_from_plan(steps: Iterable[Any]) -> dict[str, Any]:
    """Use declared Work records or their JSON-compatible mapping equivalent."""
    if isinstance(steps, (str, bytes, Mapping)):
        raise ValueError("steps must be an iterable of Work records")
    nodes, edges = [], []
    for step in steps:
        if len(nodes) >= MAX_NODES:
            raise ValueError("plan exceeds 200 nodes")
        raw = dataclasses.asdict(step) if dataclasses.is_dataclass(step) and not isinstance(step, type) else step
        if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str):
            raise ValueError("plan steps require a name")
        nodes.append({"id": raw["name"], "label": raw["name"],
                      "duration_ms": raw.get("duration_ms"), "status": "declared", "resource": raw.get("resource")})
        dependencies = raw.get("depends_on", [])
        if not isinstance(dependencies, (list, tuple)) or any(not isinstance(name, str) for name in dependencies):
            raise ValueError("step depends_on must be a list of names")
        if len(edges) + len(dependencies) > MAX_EDGES:
            raise ValueError("plan exceeds 1000 dependency edges")
        edges.extend({"source": name, "target": raw["name"], "kind": "dependency"} for name in dependencies)
    return normalize_graph({"kind": "declared", "title": "Declared execution plan", "nodes": nodes, "edges": edges})


def graph_from_source(source: str, filename: str = "input.py") -> dict[str, Any]:
    """Extract conservative lexical order and local data edges without execution."""
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("source must be a string within the 1 MB limit")
    try:
        tree = ast.parse(source)
        compile(tree, filename, "exec")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError("source has invalid syntax or excessive nesting") from None
    nodes, edges = [], []
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
        producers: dict[str, str] = {}
        previous: str | None = None
        for stmt in function.body:
            awaited = _await_assignment(stmt)
            if awaited:
                if len(nodes) >= MAX_NODES:
                    raise ValueError("source flow exceeds 200 nodes")
                target, call = awaited
                identifier = f"{function.name}@{function.lineno}:{stmt.lineno}:{stmt.col_offset}"
                label = f"{function.name}: {target} ← await {_call_name(call)}"
                nodes.append({"id": identifier, "label": label, "duration_ms": None, "status": "inferred"})
                dependencies = {producers[name] for name in _loads(call) if name in producers}
                for producer in sorted(dependencies):
                    edges.append({"source": producer, "target": identifier, "kind": "dependency"})
                if previous is not None and previous not in dependencies:
                    edges.append({"source": previous, "target": identifier, "kind": "order"})
                producers[target] = identifier
                previous = identifier
            else:
                # Conditional/mutable reassignment means an earlier assignment
                # can no longer safely be identified as the producer.
                writes = {node.id for node in ast.walk(stmt) if isinstance(node, ast.Name)
                          and isinstance(node.ctx, (ast.Store, ast.Del))}
                for name in writes:
                    producers.pop(name, None)
    return normalize_graph({"kind": "inferred", "title": f"Static await structure · {_text(filename, 100)}",
                            "nodes": nodes, "edges": edges})


def _display(node: Mapping[str, Any], kind: str) -> str:
    label = node["label"]
    if node["duration_ms"] is not None:
        qualifier = "" if kind == "measured" else "estimate "
        label += f" · {qualifier}{node['duration_ms']:.1f} ms"
    if node["status"] != "unknown":
        label += f" · {node['status']}"
    return label


def _mermaid_text(value: str) -> str:
    # Numeric entities retain visible punctuation without emitting quote,
    # directive, HTML, markdown, or escape syntax from untrusted labels.
    return "".join(f"#{ord(ch)};" if ch in '"&<>{}[]|`%!#\\' else ch for ch in value)


def _plantuml_text(value: str) -> str:
    # Physical newlines/control characters were removed during normalization.
    # Encode directive and Creole/link metacharacters, including !include.
    return "".join(f"&#{ord(ch)};" if ch in '"&<>[]@!%\\' else ch for ch in value)


def _wrap(value: str, width: int = 30, limit: int = 3) -> list[str]:
    lines, current, units = [], "", 0
    for char in value:
        weight = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if units + weight > width and current:
            lines.append(current)
            current, units = "", 0
            if len(lines) == limit:
                lines[-1] = lines[-1][:-1] + "…"
                return lines
        current += char
        units += weight
    if current:
        lines.append(current)
    return lines or [""]


def _svg(graph: Mapping[str, Any]) -> str:
    nodes, edges = graph["nodes"], graph["edges"]
    depths = _depths(graph)
    levels: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        levels[depths[node["id"]]].append(node)
    columns = min(4, max((len(level) for level in levels.values()), default=1))
    width, y = max(520, columns * 260 + 40), 82
    positions = {}
    for depth in sorted(levels):
        group = levels[depth]
        for index, node in enumerate(group):
            positions[node["id"]] = (24 + index % 4 * 260, y + index // 4 * 126)
        y += math.ceil(len(group) / 4) * 126
    height = max(235, y + 55)
    svg = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg", "viewBox": f"0 0 {width} {height}",
                             "role": "img", "aria-labelledby": "graph-title graph-desc"})
    ET.SubElement(svg, "title", {"id": "graph-title"}).text = graph["title"]
    ET.SubElement(svg, "desc", {"id": "graph-desc"}).text = graph["caveat"]
    ET.SubElement(svg, "rect", {"width": str(width), "height": str(height), "fill": "#f6f8fb"})
    defs = ET.SubElement(svg, "defs")
    marker = ET.SubElement(defs, "marker", {"id": "arrow", "viewBox": "0 0 10 10", "refX": "9", "refY": "5",
                                            "markerWidth": "6", "markerHeight": "6", "orient": "auto-start-reverse"})
    ET.SubElement(marker, "path", {"d": "M 0 0 L 10 5 L 0 10 z", "fill": "#7b8ba5"})
    ET.SubElement(svg, "text", {"x": "24", "y": "30", "font-size": "18", "font-family": "sans-serif", "fill": "#13213b"}).text = graph["title"][:65]
    ET.SubElement(svg, "text", {"x": "24", "y": "53", "font-size": "12", "font-family": "sans-serif", "fill": "#52637d"}).text = f"{graph['kind'].upper()} · {len(nodes)} stages · solid dependency / dashed source order"
    for edge in edges:
        sx, sy = positions[edge["source"]]
        tx, ty = positions[edge["target"]]
        sx, sy, tx = sx + 118, sy + 90, tx + 118
        middle = (sy + ty) / 2
        attrs = {"d": f"M {sx} {sy} C {sx} {middle:g}, {tx} {middle:g}, {tx} {ty}",
                 "fill": "none", "stroke": "#7b8ba5", "stroke-width": "1.5", "marker-end": "url(#arrow)"}
        if edge["kind"] == "order":
            attrs["stroke-dasharray"] = "5 4"
        ET.SubElement(svg, "path", attrs)
    colors = {"completed": "#19866b", "failed": "#bd3e4f", "cancelled": "#bd3e4f",
              "fallback": "#a86a16", "skipped": "#7a8698", "inferred": "#5869a8"}
    for index, node in enumerate(nodes):
        x, top = positions[node["id"]]
        group = ET.SubElement(svg, "g", {"id": f"n{index}"})
        ET.SubElement(group, "title").text = _display(node, graph["kind"])
        ET.SubElement(group, "rect", {"x": str(x), "y": str(top), "width": "236", "height": "90", "rx": "10", "fill": "#ffffff", "stroke": "#d8e0ec"})
        ET.SubElement(group, "rect", {"x": str(x), "y": str(top), "width": "5", "height": "90", "rx": "2", "fill": colors.get(node["status"], "#52728f")})
        for line_no, line in enumerate(_wrap(node["label"], limit=2)):
            ET.SubElement(group, "text", {"x": str(x + 14), "y": str(top + 24 + line_no * 17), "font-family": "sans-serif", "font-size": "12", "fill": "#13213b"}).text = line
        detail = node["status"]
        if node["duration_ms"] is not None:
            detail += f" · {'~' if graph['kind'] != 'measured' else ''}{node['duration_ms']:.1f} ms"
        ET.SubElement(group, "text", {"x": str(x + 14), "y": str(top + 73), "font-family": "sans-serif", "font-size": "11", "fill": "#52637d"}).text = detail
    if not nodes:
        ET.SubElement(svg, "text", {"x": "24", "y": "110", "font-family": "sans-serif", "font-size": "14", "fill": "#52637d"}).text = "No supported stages in this input."
    footer = "One trace is not p95." if graph["kind"] == "measured" else "Declared/inferred structure; validate against execution traces."
    ET.SubElement(svg, "text", {"x": "24", "y": str(height - 20), "font-family": "sans-serif", "font-size": "11", "fill": "#52637d"}).text = footer
    return ET.tostring(svg, encoding="unicode") + "\n"


def compile_graph(graph: Mapping[str, Any], format: str = "mermaid") -> str:
    """Render one validated DAG locally; formats are mermaid, plantuml, svg."""
    normalized = normalize_graph(graph)
    ids = {node["id"]: f"n{index}" for index, node in enumerate(normalized["nodes"])}
    if format == "svg":
        return _svg(normalized)
    if format == "mermaid":
        lines = ["flowchart TD"]
        lines.extend(f'  {ids[node["id"]]}["{_mermaid_text(_display(node, normalized["kind"]))}"]' for node in normalized["nodes"])
        for edge in normalized["edges"]:
            arrow = "-.->" if edge["kind"] == "order" else "-->"
            lines.append(f'  {ids[edge["source"]]} {arrow} {ids[edge["target"]]}')
        return "\n".join(lines) + "\n"
    if format == "plantuml":
        lines = ["@startuml", "top to bottom direction", "skinparam shadowing false",
                 f'title {_plantuml_text(normalized["title"])}']
        lines.extend(f'rectangle "{_plantuml_text(_display(node, normalized["kind"]))}" as {ids[node["id"]]}' for node in normalized["nodes"])
        for edge in normalized["edges"]:
            arrow = "..>" if edge["kind"] == "order" else "-->"
            lines.append(f'{ids[edge["source"]]} {arrow} {ids[edge["target"]]}')
        lines.append("@enduml")
        return "\n".join(lines) + "\n"
    raise ValueError("format must be mermaid, plantuml or svg")
