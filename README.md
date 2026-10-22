# Applied Opt

**Review AI application code, propose bounded optimizations, and verify the result.**

Applied Opt connects line-level code review with execution measurements. It focuses on sequential/parallel trade-offs, unnecessary analysis stages, retry amplification, and cache correctness. Python 3.11+, Apache-2.0, no runtime dependencies for the core. The optional hosted server uses Gunicorn on Python 3.12+.

[한국어](README.ko.md) · [Host the web app](docs/hosting.md) · [Architecture](docs/architecture.md) · [Trade-offs](docs/tradeoffs.md) · [Integration](docs/integration.md) · [Flow compiler](docs/diagrams.md) · [MCP](docs/mcp.md)

## What ships in v0.1

| Component | What it does |
| --- | --- |
| Local reviewer | Deterministic Python AST rules with file/line evidence, uncertainty, and recommendations; JSON, Markdown, and SARIF output. |
| Patch proposer | Produces a diff for two adjacent approved async calls. Explicit safe-call contracts are required. |
| Planner | Compares sequential and bounded-parallel scheduling under supplied dependencies, resource limits, and contention assumptions; replays measured traces to show which stage reduction would be visible end to end. |
| Flow compiler | Turns inferred source structure, declared plans, or measured traces into Mermaid, PlantUML, and standalone SVG. |
| Runtime | Executes declared DAGs, coordinates routing, bounds retries, and supports opt-in exact caching. |
| Evaluation | Alternates baseline/candidate runs and checks application assertions plus per-case p95 regression; runs revisions of one package in separate processes, with a paid-call budget and stop-on-quota. |
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

**The web server and hosting configuration are in this repository.** One application serves the dashboard, authenticated Python code review, graph compilation, and the configured report. `applied-opt host` runs the Gunicorn-backed server; it accepts a declared public HTTPS origin even when a proxy uses a different internal port.

From the source checkout, with Docker Compose and Python 3 installed:

```bash
python3 deploy/bootstrap.py --up
```

Open **http://localhost:8787** and enter the generated `APPLIED_OPT_TOKEN` from `.env` in the dashboard. Existing environment files are never overwritten. For a VPS with a DNS hostname, `python3 deploy/bootstrap.py --domain opt.example.com --up` prepares a separate production stack with Caddy HTTPS and persistent certificate storage.

[Deploy to Render](https://render.com/deploy?repo=https://github.com/thuygom/applied-opt) uses the included [`render.yaml`](render.yaml) Docker Blueprint. The image serves both UI and API; the Blueprint asks Render to generate a deployment token, and the server uses the platform's public URL and port. [Hosting instructions](docs/hosting.md) cover local Docker, VPS setup, domain configuration, reports, health checks, and verification. Provider accounts and DNS are configured on your own deployment target; including these assets does not claim a provider deployment was performed.

The bundled demo measures real local wall time around **synthetic async delays and deterministic fixtures**. It is neither an LLM accuracy benchmark nor evidence of a production 15-second SLA. Models, prompts, cache states, reference dates, and load must be controlled in your own comparison.

## Agent skill

Source is included in [`skills/review-ai-pipeline`](skills/review-ai-pipeline). Load its `SKILL.md` through your agent's supported skill mechanism; for hosts supporting repository-local skills, copy this directory into that host's repository skill directory. This package does not automatically register or activate the skill.

The workflow is inspired by [CodeRabbit code reviews](https://docs.coderabbit.ai/guides/code-review-overview) and [walkthroughs](https://docs.coderabbit.ai/pr-reviews/walkthroughs). Applied Opt is independent, unaffiliated, and does not include CodeRabbit's models or implementation.

## Development

```bash
python -m unittest discover -s tests -v
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Examples contain synthetic data; private repositories, operational logs, and recordings are excluded.
