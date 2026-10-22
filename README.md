# Applied Opt

**Review AI application code, propose bounded optimizations, and verify the result.**

Applied Opt connects line-level code review with execution measurements. It focuses on sequential/parallel trade-offs, unnecessary analysis stages, retry amplification, and cache correctness. Python 3.11+, Apache-2.0, no runtime dependencies.

[한국어](README.ko.md) · [Architecture](docs/architecture.md) · [Trade-offs](docs/tradeoffs.md) · [Integration](docs/integration.md) · [Flow compiler](docs/diagrams.md) · [MCP](docs/mcp.md)

## What ships in v0.1

| Component | What it does |
| --- | --- |
| Local reviewer | Deterministic Python AST rules with file/line evidence, uncertainty, and recommendations; JSON, Markdown, and SARIF output. |
| Patch proposer | Produces a diff for two adjacent approved async calls. Explicit safe-call contracts are required. |
| Planner | Compares sequential and bounded-parallel scheduling under supplied dependencies, resource limits, and contention assumptions. |
| Flow compiler | Turns inferred source structure, declared plans, or measured traces into Mermaid, PlantUML, and standalone SVG. |
| Runtime | Executes declared DAGs, coordinates routing, bounds retries, and supports opt-in exact caching. |
| Evaluation | Alternates baseline/candidate runs and checks application assertions plus per-case p95 regression. |
| Dashboard | Displays review findings, measurements, quality gates, traces, and planning estimates. |
| MCP server | Exposes four read-only local tools over stdio: review, planning, diagram compilation, and run comparison. |
| Agent skill | Guides semantic review, application-specific invariants, and evidence-backed changes. |

The AST reviewer does not call an AI model or execute the reviewed repository. The agent skill supplies a workflow for an agent that can understand application context; its capabilities and data handling depend on that agent's host. This is an alpha release with deliberately narrow automatic transformations.

## Start locally

Extract the source archive or clone the project, then run inside its directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
applied-opt review examples/review_target.py --format markdown
applied-opt demo --output report.json --repetitions 7
applied-opt serve --report report.json --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787`. Installation uses this source checkout; a public PyPI release is not assumed. On Windows, activate `.venv\Scripts\activate` instead.

## Review → propose → verify

```bash
applied-opt review ./your-project --format sarif --output review.sarif
applied-opt review . --base main --fail-on high
applied-opt optimize examples/review_target.py \
  --safe-call fetch_context --safe-call detect_typos --output candidate.diff
applied-opt compare baseline.json candidate.json --output comparison.json
```

`--base` requires a Git checkout and filters findings to added/changed line hunks. It is not a complete cross-file semantic review. Review and optimization also accept `--policy examples/review_policy.toml`.

`optimize` writes a proposed diff by default. Add `--apply` only after reviewing the safe-call contract and the diff. Allowlisted calls must be read-only, cancellation-safe, free from shared-state mutation, and supported by available resources. The proposer refuses direct dependencies, mutable/dynamic arguments, method receivers, and ambiguous source layouts. Type annotations are declarations, not runtime proof.

`compare` exits with status 1 when its gate fails. A faster candidate cannot pass by dropping a required schedule or task. See [the input format](docs/integration.md#compare-saved-runs).

## Flow compiler and MCP

```bash
applied-opt graph examples/review_target.py --from source --format svg --output flow.svg
applied-opt graph report.json --from report --variant candidate --format mermaid --output flow.mmd
applied-opt graph work.json --from plan --format plantuml --output flow.puml
```

Common graph JSON is the canonical representation. Source, plans, and traces are converted into that representation; Mermaid, PlantUML, and SVG are export formats. This is not a general parser or round-trip editor for those diagram languages. See [graph semantics and limits](docs/diagrams.md).

Configure an MCP client's stdio transport to run `applied-opt mcp` or `python -m applied_opt.mcp_server`. It exposes `review_code`, `plan_execution`, `compile_flow`, and `compare_runs`, without executing supplied source or modifying repositories. [MCP connection instructions](docs/mcp.md) describe the supported protocol subset, input limits, and snapshot revisions.

## Reporting and deployment

The local Python server can review pasted Python through a non-executing AST endpoint and read one configured report file. It does not clone arbitrary repositories or run uploaded code. A hosted static dashboard displays bundled/imported reports; it does not provide the local Python analysis endpoint.

Docker packaging is included. Set `APPLIED_OPT_TOKEN` to a strong value of at least 16 characters, then run `docker compose up --build`. Use the dashboard's token field when authentication is enabled. Non-loopback binding requires a token. The supplied setup supports local/same-port Docker access; current Host/Origin validation expects the server port. A reverse proxy with a different public port requires reviewed integration changes. Turnkey Internet-facing deployment is not included.

The bundled demo measures real local wall time around **synthetic async delays and deterministic fixtures**. It is neither an LLM accuracy benchmark nor evidence of a production 15-second SLA. Models, prompts, cache states, reference dates, and load must be controlled in your own comparison.

## Agent skill

Source is included in [`skills/review-ai-pipeline`](skills/review-ai-pipeline). Load its `SKILL.md` through your agent's supported skill mechanism; for hosts supporting repository-local skills, copy this directory into that host's repository skill directory. This package does not automatically register or activate the skill.

The workflow is inspired by [CodeRabbit code reviews](https://docs.coderabbit.ai/guides/code-review-overview) and [walkthroughs](https://docs.coderabbit.ai/pr-reviews/walkthroughs). Applied Opt is independent, unaffiliated, and does not include CodeRabbit's models or implementation.

## Development

```bash
python -m unittest discover -s tests -v
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Examples contain synthetic data; private repositories, operational logs, and recordings are excluded.
