"""Local code review, optimization proposals, measurements and reports."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _read_json(path: str) -> Any:
    file = Path(path)
    if file.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("JSON input exceeds 8 MiB")
    return json.loads(file.read_text(encoding="utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")))


def _write(text: str, output: str | None) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _policy(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    return tomllib.loads(Path(path).read_text(encoding="utf-8"))


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "diff.external=", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=20)
    if completed.returncode:
        raise ValueError("Git base review failed; use an existing commit reference in a Git checkout")
    return completed.stdout


def _filter_changed(report: dict[str, Any], target: Path, base: str) -> dict[str, Any]:
    if not base or base.startswith("-"):
        raise ValueError("base must be a commit reference, not an option")
    scan_root = target.absolute() if target.is_dir() else target.absolute().parent
    repo_root = Path(_git(scan_root, "rev-parse", "--show-toplevel").strip())
    sha = _git(scan_root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").strip()
    kept = []
    line_ranges: dict[str, list[tuple[int, int]]] = {}
    for file in report["files"]:
        local = scan_root / file["path"]
        relative = local.relative_to(repo_root).as_posix()
        diff = _git(repo_root, "diff", "--no-ext-diff", "--no-textconv", "--unified=0", sha, "--", relative)
        ranges = []
        for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
            start, count = int(match[1]), int(match[2] or "1")
            if count:
                ranges.append((start, start + count - 1))
        # New untracked files are part of a working tree review too.
        tracked = _git(repo_root, "ls-files", "--", relative).strip()
        if not tracked:
            ranges = [(1, sys.maxsize)]
        line_ranges[file["path"]] = ranges
    for finding in report["findings"]:
        if any(finding["line"] <= end and finding["end_line"] >= start for start, end in line_ranges.get(finding["path"], [])):
            kept.append(finding)
    report["findings"] = kept
    report["summary"]["findings"] = len(kept)
    report["summary"]["by_severity"] = dict(Counter(f["severity"] for f in kept))
    report["summary"]["optimization_candidates"] = sum(f["rule_id"] == "AO001" for f in kept)
    report["scope"] = {"base": sha, "mode": "changed lines and untracked files", "note": "Cross-file semantic impact requires the agent skill; this filters syntactic findings."}
    return report


def sarif_report(review: dict[str, Any]) -> dict[str, Any]:
    findings = review.get("findings", [])
    rules = {f["rule_id"]: {"id": f["rule_id"], "shortDescription": {"text": f["title"]}} for f in findings}
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Applied Opt", "version": __version__, "rules": list(rules.values())}}, "results": [{"ruleId": f["rule_id"], "level": {"error": "error", "warning": "warning", "info": "note"}.get(f["severity"], "note"), "message": {"text": f["title"] + "\n" + f["evidence"] + "\n" + f["recommendation"]}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": quote(f["path"].replace("\\", "/"), safe="/.-_")}, "region": {"startLine": max(1, f["line"]), "endLine": max(f["line"], f["end_line"])}}}]} for f in findings]}]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="applied-opt", description="Review AI pipeline latency and verify optimization trade-offs.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="Review Python files without importing or executing them")
    review.add_argument("path")
    review.add_argument("--format", choices=("json", "markdown", "sarif"), default="markdown")
    review.add_argument("--output")
    review.add_argument("--policy", help="TOML with [review] configuration")
    review.add_argument("--base", help="Filter findings to lines changed from a Git ref")
    review.add_argument("--fail-on", choices=("high", "medium", "low", "none"), default="high")
    optimize = sub.add_parser("optimize", help="Propose bounded parallel patches under an explicit call contract")
    optimize.add_argument("file")
    optimize.add_argument("--safe-call", action="append", default=[], help="Read-only, immutable-input, cancel-safe call approved for overlap")
    optimize.add_argument("--policy", help="TOML with [parallel].safe_calls")
    optimize.add_argument("--output", help="Write unified diff to this file")
    optimize.add_argument("--apply", action="store_true", help="Apply reviewed candidate locally; creates .applied-opt.bak and refuses overwrite")
    demo = sub.add_parser("demo", help="Measure simulated service work and named quality assertions")
    demo.add_argument("--output", required=True)
    demo.add_argument("--repetitions", type=int, default=7)
    demo.add_argument("--warmups", type=int, default=1)
    compare = sub.add_parser("compare", help="Fail if matched run cohorts lose quality or regress p95")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.add_argument("--output")
    compare.add_argument("--tolerance", type=float, default=.05)
    plan = sub.add_parser("plan", help="Estimate sequential/bounded-parallel scheduling trade-offs")
    plan.add_argument("file")
    plan.add_argument("--output")
    graph = sub.add_parser("graph", help="Compile source, reports or declared plans into flow diagrams")
    graph.add_argument("file")
    graph.add_argument("--from", dest="source_kind", choices=("source", "report", "plan", "graph"), default="report")
    graph.add_argument("--format", choices=("mermaid", "plantuml", "svg", "json"), default="mermaid")
    graph.add_argument("--variant", choices=("baseline", "candidate"), default="candidate")
    graph.add_argument("--output")
    serve = sub.add_parser("serve", help="Run the local report dashboard and review API")
    serve.add_argument("--report")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--token-env", default="APPLIED_OPT_TOKEN", help="Environment variable name, never a literal token")
    sub.add_parser("mcp", help="Serve read-only review/planning/diagram tools over MCP stdio")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "review":
            from .review import render_markdown, review_path
            report = review_path(args.path, _policy(args.policy).get("review", {}))
            if args.base:
                report = _filter_changed(report, Path(args.path), args.base)
            envelope = {"schema_version": "1.0", "project": Path(args.path).absolute().name, "generated_at": datetime.now(timezone.utc).isoformat(), "review": report}
            rendered = _json(envelope) if args.format == "json" else _json(sarif_report(report)) if args.format == "sarif" else render_markdown(report)
            _write(rendered, args.output)
            rank = {"error": 3, "warning": 2, "info": 1}
            threshold = {"high": 3, "medium": 2, "low": 1, "none": 99}[args.fail_on]
            return int(any(rank.get(f["severity"], 0) >= threshold for f in report["findings"]))
        if args.command == "optimize":
            from .transform import propose_parallel_patch
            path = Path(args.file)
            if args.output and Path(args.output).absolute() == path.absolute():
                raise ValueError("Diff output must differ from the source path")
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("Refusing symlink optimization path")
            if path.stat().st_size > 1_000_000:
                raise ValueError("Source exceeds 1 MB limit")
            source = path.read_bytes().decode("utf-8")
            policy_names = _policy(args.policy).get("parallel", {}).get("safe_calls", [])
            if not isinstance(policy_names, list) or any(not isinstance(x, str) for x in policy_names):
                raise ValueError("[parallel].safe_calls must be an array of function names")
            names = list(args.safe_call) + policy_names
            if not names:
                raise ValueError("Explicit --safe-call or [parallel].safe_calls is required")
            result = propose_parallel_patch(source, names, path.name)
            if args.apply and result["changed"]:
                backup = path.with_name(path.name + ".applied-opt.bak")
                if backup.exists():
                    raise ValueError("Backup exists; inspect it before applying another patch")
                # Refuse stale source changes between proposal and local application.
                if path.read_bytes().decode("utf-8") != source:
                    raise ValueError("Source changed during optimization; review the current file")
                with backup.open("xb") as stream:
                    stream.write(source.encode("utf-8"))
                mode = path.stat().st_mode
                fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".applied-opt-")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(result["source"].encode("utf-8"))
                    os.chmod(temporary, mode)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            _write(result["diff"], args.output)
            print(f"Proposed {result['applied']} bounded parallel batch(es). {'Applied locally.' if args.apply and result['changed'] else 'Source unchanged.'}", file=sys.stderr)
            for refused in result["refusals"]:
                print(f"Line {refused['line']}: {refused['reason']}", file=sys.stderr)
            return 0
        if args.command == "demo":
            from .demo import create_demo_report
            from .review import review_source
            if not 1 <= args.repetitions <= 1000 or not 0 <= args.warmups <= 100:
                raise ValueError("repetitions must be 1..1000 and warmups 0..100")
            report = asyncio.run(create_demo_report(repetitions=args.repetitions, warmups=args.warmups))
            demo_source = "async def create_note(text: str):\n    context = await fetch_context(text)\n    spelling = await detect_typos(text)\n    result = await generate_note(text, context, spelling)\n    tasks = await extract_tasks(result)\n    return result, tasks\n"
            report["review"] = review_source(demo_source, "synthetic_pipeline.py")
            _write(_json(report), args.output)
            print(f"Synthetic report saved: {args.output}. Gate: {'PASS' if report['gate']['passed'] else 'FAIL'}")
            return 0 if report["gate"]["passed"] else 1
        if args.command == "compare":
            from .evaluation import compare_reports
            result = compare_reports(_read_json(args.baseline), _read_json(args.candidate), tolerance=args.tolerance)
            _write(_json(result), args.output)
            return 0 if result["passed"] else 1
        if args.command == "plan":
            from .planning import Work, compare_strategies
            data = _read_json(args.file)
            steps = data.pop("steps")
            if len(steps) > 200:
                raise ValueError("At most 200 steps are supported")
            work = [Work(**{**row, "depends_on": tuple(row.get("depends_on", ()))}) for row in steps]
            _write(_json(compare_strategies(work, **data)), args.output)
            return 0
        if args.command == "graph":
            from .diagrams import compile_graph, graph_from_plan, graph_from_report, graph_from_source
            if args.source_kind == "source":
                path = Path(args.file)
                if path.stat().st_size > 1_000_000:
                    raise ValueError("Source exceeds 1 MB limit")
                graph_data = graph_from_source(path.read_text(encoding="utf-8"), path.name)
            else:
                data = _read_json(args.file)
                graph_data = graph_from_report(data, variant=args.variant) if args.source_kind == "report" else graph_from_plan(data["steps"]) if args.source_kind == "plan" else data
            _write(_json(graph_data) if args.format == "json" else compile_graph(graph_data, format=args.format), args.output)
            return 0
        if args.command == "serve":
            from .server import serve
            serve(report_path=args.report, host=args.host, port=args.port, token=os.environ.get(args.token_env))
            return 0
        if args.command == "mcp":
            from .mcp_server import main as mcp_main
            return mcp_main()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"applied-opt: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
