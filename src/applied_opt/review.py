"""Local, non-executing reviews of Python AI application code.

These rules identify evidence for a human review. An AST cannot establish network
latency, model quality, shared-resource capacity, or the absence of side effects.
Reports intentionally omit literal values and never upload or import reviewed code.
"""

from __future__ import annotations

import ast
import fnmatch
import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


DEFAULT_EXCLUDES = frozenset({
    "node_modules", "vendor", "dist", "build", "__pycache__", "venv", "env",
    "site-packages", "generated", "migrations", "htmlcov",
})
MAX_SOURCE_BYTES = 1_000_000
LIMITATIONS = [
    "Static evidence is a review candidate, not a measured performance result.",
    "Call independence does not prove side-effect safety or provider capacity.",
    "Measure sequential and bounded-parallel p50/p95, errors, cost, and quality at representative load.",
    "No source is executed, imported, or sent to a model by this scanner.",
]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return "<dynamic>"


def _owned_walk(node: ast.AST):
    """Walk a lexical body without attributing nested functions to its caller."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from _owned_walk(child)


def _loads(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _await_assignment(node: ast.AST):
    if (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Await) and isinstance(node.value.value, ast.Call)):
        return node.targets[0].id, node.value.value
    return None


def _blocks(body: list[ast.stmt]):
    yield body
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for name in ("body", "orelse", "finalbody"):
            child = getattr(stmt, name, None)
            if isinstance(child, list) and child:
                yield from _blocks(child)
        if isinstance(stmt, (ast.Try, ast.TryStar)):
            for handler in stmt.handlers:
                yield from _blocks(handler.body)
        if isinstance(stmt, ast.Match):
            for case in stmt.cases:
                yield from _blocks(case.body)


def _finding(rule_id: str, severity: str, title: str, filename: str, node: ast.AST,
             evidence: str, why: str, recommendation: str, confidence: str = "medium",
             *, end_line: int | None = None, prerequisites: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "rule_id": rule_id, "severity": severity, "title": title, "path": filename,
        "line": getattr(node, "lineno", 1),
        "end_line": end_line or getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        "evidence": evidence, "why": why, "recommendation": recommendation,
        "confidence": confidence,
        "autofix": {"available": False, "requires": list(prerequisites)},
    }


def _summarize(findings: list[dict[str, Any]], files: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["severity"] for item in findings)
    return {
        "files_scanned": sum(item.get("status") == "reviewed" for item in files),
        "files_with_errors": sum(item.get("status") == "error" for item in files),
        "findings": len(findings),
        "by_severity": {key: counts.get(key, 0) for key in ("error", "warning", "info")},
        "optimization_candidates": sum(item["rule_id"] == "AO001" for item in findings),
        "measured_speedup": None,
    }


def _error_report(filename: str, title: str, *, line: int = 1) -> dict[str, Any]:
    marker = ast.Pass(lineno=line, end_lineno=line)
    findings = [_finding("AO000", "error", title, filename, marker,
                         "The file could not be reviewed.",
                         "Incomplete analysis cannot establish safety.",
                         "Resolve the reported file or syntax issue and review again.", "high")]
    files = [{"path": filename, "status": "error"}]
    return {"format_version": "1.0", "kind": "static_review", "findings": findings,
            "files": files, "summary": _summarize(findings, files), "limitations": LIMITATIONS[:]}


def review_source(source: str, filename: str = "input.py") -> dict[str, Any]:
    """Return line-addressable review findings; never evaluate supplied source.

    Literal values, snippets, and parse-error source lines are omitted from the
    resulting report. Findings use syntactic evidence with explicit uncertainty.
    """
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return _error_report(filename, "Source exceeds the 1 MB review limit")
    try:
        tree = ast.parse(source, filename=filename)
        # Compilation validates contexts such as a return outside a function; it
        # does not execute the returned code object.
        compile(tree, filename, "exec")
    except (SyntaxError, ValueError, RecursionError) as exc:
        return _error_report(filename, "Python syntax is invalid or too deeply nested",
                             line=getattr(exc, "lineno", None) or 1)

    findings: list[dict[str, Any]] = []
    import_names: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                import_names[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    # A module rebind can invalidate an imported alias. Over-suppressing a
    # candidate is preferable to claiming a custom object has library semantics.
    module_rebound = {n.id for n in _owned_walk(tree)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    module_rebound.update(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    for name in module_rebound:
        import_names.pop(name, None)

    def resolved_name(call: ast.Call, aliases: Mapping[str, str]) -> str | None:
        name = _call_name(call)
        root, *rest = name.split(".")
        if root not in aliases:
            return None
        return ".".join([aliases[root], *rest])

    sdk_retry_configs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                         and any(k.arg == "max_retries" and isinstance(k.value, ast.Constant)
                                 and isinstance(k.value.value, (int, float))
                                 and not isinstance(k.value.value, bool) and k.value.value > 0
                                 for k in n.keywords)]
    optional_calls = {"final_review", "review_output", "analyze_schedule", "extract_schedule",
                      "extract_schedules", "extract_tasks", "classify_auxiliary", "split_notes",
                      "detect_typos", "check_spelling"}
    blocking_calls = {"time.sleep", "subprocess.run", "subprocess.call", "subprocess.check_call",
                      "subprocess.check_output", "requests.get", "requests.post", "requests.put",
                      "requests.patch", "requests.delete", "requests.request", "requests.head"}
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    for function in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        owned = list(_owned_walk(function))
        shadowed = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args,
                                       *function.args.kwonlyargs)}
        shadowed.update(arg.arg for arg in (function.args.vararg, function.args.kwarg) if arg)
        shadowed.update(n.id for n in owned if isinstance(n, ast.Name)
                        and isinstance(n.ctx, (ast.Store, ast.Del)))
        shadowed.update(n.name for n in owned if isinstance(n, ast.ExceptHandler) and n.name)
        shadowed.update(n.name for n in ast.walk(function) if n is not function
                        and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        ancestor = parents.get(function)
        while ancestor is not None:
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                shadowed.update(arg.arg for arg in (*ancestor.args.posonlyargs, *ancestor.args.args,
                                                   *ancestor.args.kwonlyargs))
                shadowed.update(n.id for n in _owned_walk(ancestor) if isinstance(n, ast.Name)
                                and isinstance(n.ctx, (ast.Store, ast.Del)))
            ancestor = parents.get(ancestor)
        aliases = {name: value for name, value in import_names.items() if name not in shadowed}
        for node in owned:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if local not in shadowed:
                        aliases[local] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if local not in shadowed:
                        aliases[local] = f"{node.module}.{alias.name}"
        for body in _blocks(function.body):
            for first, second in zip(body, body[1:]):
                a, b = _await_assignment(first), _await_assignment(second)
                if not a or not b:
                    continue
                a_target, a_call = a
                b_target, b_call = b
                dependent = (a_target in _loads(b_call) or b_target in _loads(a_call)
                             or a_target == b_target)
                if dependent:
                    findings.append(_finding(
                        "AO002", "info", "Keep this await chain sequential", filename, first,
                        f"Adjacent awaited assignments in {function.name} share a read/write dependency.",
                        "Starting both calls together can use a stale or unavailable value or change assignment order.",
                        "Preserve this edge. Optimize inside either stage or parallelize independent branches only.",
                        "high", end_line=second.end_lineno))
                else:
                    findings.append(_finding(
                        "AO001", "info", "Evaluate bounded parallel execution", filename, first,
                        f"{_call_name(a_call)} and {_call_name(b_call)} are consecutive awaits with no direct local-name dependency.",
                        "Their latency may overlap, but shared mutable state, a shared GPU, connection limits, or rate limits can erase the benefit.",
                        "Verify read-only and cancellation-safe call contracts, then compare concurrency 1 versus 2 under realistic load; require unchanged outputs and acceptable p95, errors, and cost.",
                        "medium", end_line=second.end_lineno,
                        prerequisites=("explicit safe-call allowlist", "immutable arguments",
                                       "resource and cancellation review", "quality and latency comparison")))

        for call in (n for n in owned if isinstance(n, ast.Call)):
            name = resolved_name(call, aliases)
            if name in blocking_calls:
                findings.append(_finding(
                    "AO003", "warning", "Blocking operation inside an async function", filename, call,
                    f"{name} is called from async function {function.name}.",
                    "A blocking operation can stall unrelated requests on the same event loop.",
                    "Use the library's async API or a bounded worker pool after checking cancellation and thread safety. Benchmark event-loop lag.",
                    "high"))
            if name == "asyncio.gather":
                if any(isinstance(arg, ast.Starred) for arg in call.args):
                    findings.append(_finding(
                        "AO004", "warning", "Expanded async fan-out has no visible bound", filename, call,
                        "gather receives an expanded iterable of awaitables.",
                        "The iterable may create more in-flight work than the provider or shared hardware can sustain.",
                        "Bound both task creation and in-flight requests with a worker pool or batches; sweep concurrency while measuring queue time, p95, 429 errors, and total cost.",
                        "medium"))
                returns_exceptions = any(k.arg == "return_exceptions" and
                                         isinstance(k.value, ast.Constant) and k.value.value is True
                                         for k in call.keywords)
                if not returns_exceptions:
                    findings.append(_finding(
                        "AO008", "warning", "Review sibling lifecycle on gather failure", filename, call,
                        "gather uses its default exception propagation behavior.",
                        "An exception propagated from one awaitable does not by itself cancel the other awaitables, which may continue consuming resources.",
                        "Use structured cancellation or explicitly cancel and drain sibling tasks. Test failure behavior; TaskGroup changes the exception surface to ExceptionGroup.",
                        "high"))

        retry_loops = [n for n in owned if isinstance(n, (ast.For, ast.While))
                       and any(isinstance(child, ast.ExceptHandler) for child in _owned_walk(n))
                       and any(isinstance(child, ast.Await) for child in _owned_walk(n))]
        retry_decorators = [d for d in function.decorator_list if "retry" in _call_name(d).lower()]
        if retry_loops and (sdk_retry_configs or retry_decorators or len(retry_loops) > 1):
            findings.append(_finding(
                "AO005", "warning", "Multiple retry layers need a shared budget", filename, retry_loops[0],
                "An awaited exception-handling loop is present together with a retry decorator, another retry loop, or nonzero SDK retry configuration in this file.",
                "These are syntactic indicators; if they apply to the same request, attempts and tail latency can multiply.",
                "Trace which layers actually wrap the call. Assign one retry owner, an overall deadline, and idempotency rules; disable redundant SDK retries explicitly.",
                "medium"))

        seen_calls: dict[str, ast.AST] = {}
        for stmt in function.body:
            assignment = _await_assignment(stmt)
            if not assignment:
                continue
            _, call = assignment
            leaf = _call_name(call).split(".")[-1]
            if leaf in optional_calls:
                findings.append(_finding(
                    "AO006", "info", "Check whether this analysis capability is required", filename, stmt,
                    f"{leaf} is awaited directly in {function.name}, without an enclosing branch.",
                    "This may be required behavior; the AST cannot determine whether the input needs this capability.",
                    "If optional, make one validated route control its prompt, output schema, and downstream review. Preserve explicit requested capabilities and use full analysis on uncertainty; never skip only because input is short.",
                    "low"))
            signature = ast.dump(call, include_attributes=False)
            if signature in seen_calls:
                findings.append(_finding(
                    "AO009", "info", "Repeated awaited call merits a reuse review", filename, stmt,
                    f"An equivalent top-level call to {_call_name(call)} appeared earlier in {function.name}.",
                    "The call may be intentionally repeated for fresh data or independent model sampling.",
                    "Confirm freshness and sampling semantics before reusing a value. Prefer request-local reuse; cross-request caching needs explicit authorization and version scope.",
                    "medium"))
            seen_calls[signature] = stmt

    for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        decorators = [_call_name(d).split(".")[-1] for d in function.decorator_list]
        cached = any(name in {"cache", "lru_cache", "cached", "cachedmethod"} for name in decorators)
        owned = list(_owned_walk(function))
        cache_calls = [n for n in owned if isinstance(n, ast.Call)
                       and _call_name(n).split(".")[-1] in {"get", "set", "get_or_set"}
                       and "cache" in _call_name(n).lower()]
        if isinstance(function, ast.AsyncFunctionDef) and any(name in {"cache", "lru_cache"} for name in decorators):
            findings.append(_finding(
                "AO010", "warning", "Synchronous memoization on an async function", filename, function,
                "An async function has a cache or lru_cache decorator.",
                "If this is functools memoization, it caches coroutine objects rather than completed values, which cannot safely be awaited repeatedly.",
                "Verify the decorator implementation. Use an async-aware completed-value cache with explicit TTL, isolation, failure, and cancellation behavior.",
                "medium"))
        looks_contextual = any(word in function.name.lower() for word in ("analy", "infer", "context", "memory", "note", "predict"))
        has_remote_call = any(isinstance(n, ast.Call) and any(word in _call_name(n).lower()
                                                            for word in ("client", "model", "completion", "response", "fetch"))
                              for n in owned)
        if (cached or cache_calls) and (looks_contextual or has_remote_call):
            args = [a.arg.lower() for a in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)]
            has_principal = any(any(word in name for word in ("tenant", "subject", "principal", "scope")) for name in args)
            has_revision = any(any(word in name for word in ("revision", "version", "scope")) for name in args)
            if not (has_principal and has_revision):
                findings.append(_finding(
                    "AO007", "warning", "Cache isolation and invalidation contract is not visible", filename, function,
                    "A contextual or model-related function uses caching without both principal/scope and revision/version parameters visible in its signature.",
                    "Scope may be supplied elsewhere, but an input-only key can reuse stale data or another principal's result.",
                    "Inspect the complete key: tenant, principal, authorization/data revisions, model and prompt versions, relevant reference time, and exact input. Cache only explicitly deterministic read-only stages; justify any public unscoped cache.",
                    "low"))

    findings.sort(key=lambda item: (item["line"], item["rule_id"]))
    files = [{"path": filename, "status": "reviewed", "lines": len(source.splitlines())}]
    return {"format_version": "1.0", "kind": "static_review", "findings": findings,
            "files": files, "summary": _summarize(findings, files), "limitations": LIMITATIONS[:]}


def review_path(path: str | Path, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Review Python files below a local path without following symlinks.

    Optional ``config`` supports ``exclude`` (glob patterns) and
    ``max_file_bytes`` (at most the built-in 1 MB hard ceiling). Paths in reports
    are relative to the reviewed directory. Hidden, generated, vendor and venv
    directories are skipped even when not specified in configuration.
    """
    target = Path(path)
    config = dict(config or {})
    excludes = tuple(config.get("exclude", ()))
    if isinstance(config.get("exclude"), str):
        raise ValueError("exclude must be a sequence of glob patterns, not one string")
    if any(not isinstance(pattern, str) for pattern in excludes):
        raise ValueError("exclude entries must be glob strings")
    max_size = config.get("max_file_bytes", MAX_SOURCE_BYTES)
    if type(max_size) is not int or not 0 < max_size <= MAX_SOURCE_BYTES:
        raise ValueError("max_file_bytes must be between 1 and 1000000")
    if any(candidate.is_symlink() for candidate in (target, *target.parents)):
        return _error_report(target.name, "Refusing to follow a symlink")
    if not target.exists():
        return _error_report(target.name, "Review path does not exist")
    if any((part.startswith(".") and part not in {".", ".."}) or part in DEFAULT_EXCLUDES
           for part in target.parts):
        return _error_report(target.name, "Hidden and generated review paths are excluded")

    findings: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    skipped = 0
    root = target if target.is_dir() else target.parent

    def excluded(file: Path) -> bool:
        relative = file.relative_to(root)
        return (any(part.startswith(".") or part in DEFAULT_EXCLUDES for part in relative.parts)
                or any(fnmatch.fnmatch(relative.as_posix(), pattern) for pattern in excludes))

    paths: list[Path] = []
    if target.is_file():
        paths = [target] if target.suffix == ".py" else []
    else:
        def walk_error(error: OSError) -> None:
            report = _error_report(Path(error.filename or "directory").name,
                                   "A review directory could not be read")
            findings.extend(report["findings"])
            files.extend(report["files"])

        for directory, dirs, filenames in os.walk(target, followlinks=False, onerror=walk_error):
            base = Path(directory)
            old_count = len(dirs)
            dirs[:] = sorted(d for d in dirs if not excluded(base / d) and not (base / d).is_symlink())
            skipped += old_count - len(dirs)
            for name in sorted(filenames):
                file = base / name
                if file.suffix != ".py":
                    continue
                if file.is_symlink() or excluded(file):
                    skipped += 1
                    continue
                paths.append(file)
    for file in paths:
        filename = file.relative_to(root).as_posix()
        try:
            if file.stat().st_size > max_size:
                report = _error_report(filename, "Source exceeds the configured review limit")
            else:
                report = review_source(file.read_text(encoding="utf-8"), filename)
        except (OSError, UnicodeError):
            report = _error_report(filename, "Source could not be read as UTF-8")
        findings.extend(report["findings"])
        files.extend(report["files"])
    return {"format_version": "1.0", "kind": "static_review", "findings": findings,
            "files": files, "skipped_entries": skipped,
            "summary": _summarize(findings, files), "limitations": LIMITATIONS[:]}


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a portable review without source literal contents."""
    def escape(value: Any) -> str:
        return str(value).replace("`", "'").replace("\n", " ")
    summary = report.get("summary", {})
    lines = ["# Applied Opt code review", "", "Static findings require workload validation.", "",
             f"Files reviewed: {summary.get('files_scanned', 0)} · Findings: {summary.get('findings', 0)}", ""]
    for finding in report.get("findings", []):
        lines.extend([
            f"## {escape(finding['rule_id'])} · {escape(finding['title'])}", "",
            f"**{escape(finding['severity'])}** · `{escape(finding['path'])}:{finding['line']}` · confidence: {escape(finding['confidence'])}", "",
            f"**Evidence:** {escape(finding['evidence'])}", "",
            f"**Why:** {escape(finding['why'])}", "",
            f"**Recommendation:** {escape(finding['recommendation'])}", "",
        ])
    if not report.get("findings"):
        lines.extend(["No configured rule matched. This does not establish safety or optimal performance.", ""])
    return "\n".join(lines)
