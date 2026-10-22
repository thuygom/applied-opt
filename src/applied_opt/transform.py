"""Conservative, opt-in source patches for pairs of independent awaits.

An allowlisted call is an explicit engineering contract: read-only, safe to
cancel, free of shared-state mutation, and supported by the resource budget.
This module cannot infer that contract. It never edits a file or executes code.
"""

from __future__ import annotations

import ast
import difflib
from typing import Any, Iterable

from .review import MAX_SOURCE_BYTES, _await_assignment, _call_name, _loads


_PRIMITIVES = {"str", "bytes", "int", "float", "bool"}
_TRADEOFFS = [
    "The patch allows at most two calls in a batch; provider-wide concurrency still needs a shared limit.",
    "Parallelism can reduce single-request latency while increasing contention, rate-limit errors, or wasted work.",
    "The first completed failure may differ from sequential failure order; regression-test exception and cancellation behavior.",
    "Immutable type annotations are caller contracts, not runtime validation.",
    "Benchmark p50/p95, queue time, errors, token/cost totals, and semantic invariants before adoption.",
]


def _immutable_annotation(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _PRIMITIVES
    if isinstance(node, ast.Constant):
        if node.value is None:
            return True
        if isinstance(node.value, str):
            try:
                # Parse the annotation string, never evaluate it.
                return _immutable_annotation(ast.parse(node.value, mode="eval").body)
            except (SyntaxError, ValueError, RecursionError):
                return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _immutable_annotation(node.left) and _immutable_annotation(node.right)
    return False


def _immutable_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and (node.value is None or type(node.value) in {str, bytes, int, float, bool})


def _simple_argument(node: ast.AST, immutable: set[str]) -> bool:
    return _immutable_literal(node) or isinstance(node, ast.Name) and node.id in immutable


def _line_parts(source: str, lines: list[str], node: ast.AST):
    if node.lineno != node.end_lineno:
        return None
    raw = lines[node.lineno - 1]
    raw_bytes = raw.encode("utf-8")
    before = raw_bytes[:node.col_offset].decode("utf-8")
    after = raw_bytes[node.end_col_offset:].decode("utf-8").rstrip("\r\n")
    if before.strip() or "\t" in before:
        return None
    if after.strip() and not after.lstrip().startswith("#"):
        return None
    return before, after


def _advance_immutables(stmt: ast.stmt, immutable: set[str]) -> None:
    # Conservative invalidation includes assignments in conditional branches.
    # Assignments inside a nested scope may over-invalidate but never authorize
    # an unsafe conversion.
    written = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)
               and isinstance(n.ctx, (ast.Store, ast.Del))}
    immutable.difference_update(written)
    if isinstance(stmt, ast.Assign) and _immutable_literal(stmt.value):
        immutable.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None and _immutable_literal(stmt.value):
        immutable.add(stmt.target.id)


def propose_parallel_patch(source: str, safe_calls: Iterable[str] = (),
                           filename: str = "input.py") -> dict[str, Any]:
    """Propose source-preserving, two-call parallel batches and a unified diff.

    Only adjacent single-line assignments directly inside a module-level async
    function are eligible. Calls must be plain allowlisted names with literal or
    declared-immutable arguments. Dependency, alias, multiline, semicolon,
    dynamic-argument, global/nonlocal, and nested-control-flow cases are refused.

    ``safe_calls`` is a reviewed contract, not a prediction made by this tool.
    An empty allowlist produces no patch. The original file is never written.
    """
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    if isinstance(safe_calls, str):
        raise TypeError("safe_calls must be a sequence of function names")
    approved = frozenset(safe_calls)
    if any(not isinstance(name, str) or not name.isidentifier() for name in approved):
        raise ValueError("safe_calls only supports plain Python function names")
    result: dict[str, Any] = {
        "format_version": "1.0", "kind": "parallel_patch", "path": filename,
        "source": source, "diff": "", "changed": False, "applied": 0,
        "candidates": [], "refusals": [], "max_parallel_per_batch": 2,
        "tradeoffs": _TRADEOFFS[:],
    }
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        result["refusals"].append({"line": 1, "reason": "Source exceeds the 1 MB limit."})
        return result
    try:
        tree = ast.parse(source, filename=filename)
        compile(tree, filename, "exec")
    except (SyntaxError, ValueError, RecursionError) as exc:
        result["refusals"].append({"line": getattr(exc, "lineno", None) or 1,
                                   "reason": "Source has invalid syntax or excessive nesting."})
        return result

    lines = source.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in source else "\n"
    existing_names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    existing_names.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
    existing_names.update(n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    existing_names.update((n.asname or n.name.split(".")[0]) for n in ast.walk(tree) if isinstance(n, ast.alias))
    alias = "_applied_opt_gather"
    index = 1
    while alias in existing_names:
        alias = f"_applied_opt_gather_{index}"
        index += 1

    replacements: list[tuple[int, int, str]] = []
    for function in (n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)):
        all_args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        immutable = {arg.arg for arg in all_args if _immutable_annotation(arg.annotation)}
        locals_ = {arg.arg for arg in all_args}
        if function.args.vararg:
            locals_.add(function.args.vararg.arg)
        if function.args.kwarg:
            locals_.add(function.args.kwarg.arg)
        locals_.update(n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        locals_.update(n.name for n in ast.walk(function) if n is not function and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        forbidden_scope = any(isinstance(n, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom)) for n in ast.walk(function))
        position = 0
        while position < len(function.body):
            first = function.body[position]
            second = function.body[position + 1] if position + 1 < len(function.body) else None
            a, b = _await_assignment(first), _await_assignment(second) if second else None
            if not a or not b:
                _advance_immutables(first, immutable)
                position += 1
                continue
            a_target, a_call = a
            b_target, b_call = b
            reasons: list[str] = []
            if forbidden_scope:
                reasons.append("Global/nonlocal bindings or generator semantics require manual review.")
            if a_target == b_target or a_target in _loads(b_call) or b_target in _loads(a_call):
                reasons.append("The assignments have a read/write or write/write dependency.")
            for call in (a_call, b_call):
                if not isinstance(call.func, ast.Name):
                    reasons.append("Only plain named calls are supported; method receivers may share mutable state.")
                elif call.func.id not in approved:
                    reasons.append(f"{call.func.id} has no explicit safe-call contract.")
                elif call.func.id in locals_:
                    reasons.append(f"{call.func.id} is shadowed by a local binding.")
                if any(k.arg is None for k in call.keywords):
                    reasons.append("Expanded keyword arguments require manual review.")
                arguments = [*call.args, *(k.value for k in call.keywords)]
                if any(not _simple_argument(arg, immutable) for arg in arguments):
                    reasons.append("Arguments must be literals or declared immutable names; dynamic expressions and mutable aliases are refused.")
            first_parts, second_parts = _line_parts(source, lines, first), _line_parts(source, lines, second)
            if first_parts is None or second_parts is None:
                reasons.append("Only standalone single-line assignments with space indentation can be preserved safely.")
            elif first_parts[0] != second_parts[0] or second.lineno != first.end_lineno + 1:
                reasons.append("Comments, blank lines, or differing indentation separate this pair.")
            candidate = {"line": first.lineno, "end_line": second.end_lineno,
                         "calls": [_call_name(a_call), _call_name(b_call)],
                         "eligible": not reasons, "reasons": list(dict.fromkeys(reasons))}
            result["candidates"].append(candidate)
            if reasons:
                result["refusals"].append({"line": first.lineno, "reason": " ".join(candidate["reasons"])})
                _advance_immutables(first, immutable)
                position += 1
                continue
            indent = first_parts[0]
            a_source = ast.get_source_segment(source, a_call)
            b_source = ast.get_source_segment(source, b_call)
            # The original await expressions stay byte-for-byte identical. Only
            # execution order and tuple assignment change under the contract.
            replacement = (
                f"{indent}{a_target}, {b_target} = await {alias}({newline}"
                f"{indent}    {a_source},{first_parts[1]}{newline}"
                f"{indent}    {b_source},{second_parts[1]}{newline}"
                f"{indent}){newline}"
            )
            replacements.append((first.lineno - 1, second.end_lineno, replacement))
            _advance_immutables(first, immutable)
            _advance_immutables(second, immutable)
            position += 2

    if not replacements:
        return result
    import_position = 0
    while import_position < len(lines) and (not lines[import_position].strip() or lines[import_position].lstrip().startswith("#")):
        import_position += 1
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str):
        import_position = tree.body[0].end_lineno
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            import_position = max(import_position, node.end_lineno)
    import_text = f"from applied_opt.runtime import gather_strict as {alias}{newline}"
    edits = [*replacements, (import_position, import_position, import_text)]
    for start, end, content in sorted(edits, key=lambda item: item[0], reverse=True):
        lines[start:end] = [content]
    patched = "".join(lines)
    try:
        compile(patched, filename, "exec")
    except (SyntaxError, ValueError, RecursionError):
        result["refusals"].append({"line": 1, "reason": "Generated patch failed compilation; no change returned."})
        return result
    result.update({
        "source": patched, "changed": True, "applied": len(replacements),
        "diff": "".join(difflib.unified_diff(source.splitlines(keepends=True), patched.splitlines(keepends=True),
                                             fromfile=f"a/{filename}", tofile=f"b/{filename}")),
        "required_dependency": "applied-opt>=0.1.0",
    })
    return result
