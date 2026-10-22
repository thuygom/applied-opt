# Flow compiler

The diagram compiler uses a common graph for source structure, declared plans, and measured traces. It exports Mermaid, PlantUML, deterministic standalone SVG, or graph JSON through the CLI. Compilation is local: no diagram is sent to an external rendering service.

The common graph JSON is canonical. Mermaid and PlantUML are output languages here, not accepted general-purpose source languages; importing arbitrary `.mmd` or `.puml` files for round-trip editing is outside this compiler's scope.

```bash
applied-opt graph examples/review_target.py --from source --format svg --output flow.svg
applied-opt graph report.json --from report --variant candidate --format mermaid --output flow.mmd
applied-opt graph work.json --from plan --format plantuml --output flow.puml
applied-opt graph graph.json --from graph --format svg --output flow.svg
```

| Kind | Meaning | Edges and timing |
| --- | --- | --- |
| `inferred` | Direct awaited assignments found in Python source | Dashed edges show lexical order; solid edges show observed local-name dependencies. No runtime duration. |
| `declared` | Engineer-supplied work graph | Declared dependencies and optional supplied duration estimates. |
| `measured` | One selected baseline/candidate trace | Dependencies recorded in the trace and observed stage durations. One trace is not p95. |

Source extraction does not reconstruct branches, loops, TaskGroup bodies, arbitrary tuple assignments, or cross-function execution. A disconnected graph can reflect missing evidence; the compiler does not invent connections from timestamps.

## Python API

```python
from applied_opt.diagrams import graph_from_report, compile_graph

graph = graph_from_report(report, variant="candidate")
svg = compile_graph(graph, format="svg")
```

`graph_from_source(source, filename)`, `graph_from_plan(steps)`, and `normalize_graph(graph)` provide the other entry points. Plans accept `Work` objects or equivalent mappings.

Common graph format:

```json
{
  "schema_version": "1.0",
  "kind": "declared",
  "title": "Note pipeline",
  "nodes": [
    {"id": "context", "label": "Fetch context", "duration_ms": 20, "status": "declared"},
    {"id": "note", "label": "Generate note", "duration_ms": 80, "status": "declared"}
  ],
  "edges": [{"source": "context", "target": "note", "kind": "dependency"}]
}
```

The compiler rejects cycles, unknown references, duplicate IDs, and invalid durations. It caps graphs at 200 nodes and 1,000 edges, bounds labels, remaps export identifiers, and escapes directive/markup characters. SVG includes an accessible title/description and uses at most four node columns. Mermaid and PlantUML layout depends on the consuming renderer.

The [MCP `compile_flow` tool](mcp.md) exposes the same source/graph/report compilation boundary and returns a graph revision to associate a preview with its input snapshot.
