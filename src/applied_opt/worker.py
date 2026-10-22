"""Serve one evaluation variant over a JSON line protocol.

Two revisions of one application usually share module names, so they cannot
be imported side by side. Run each in its own process (often a container built
from the deployed image, with the candidate source mounted) and serve a handler:

    python worker.py package.module:handle

The module uses only the standard library and no relative imports, so it can
be mounted or copied into an environment that does not have Applied Opt
installed. Application logs may share stdout: only marker-prefixed lines are
protocol messages. Replies carry an exception's type, never its message.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import time
from typing import Any, Callable, TextIO

MARKER = "@@applied-opt@@ "
REPLY_FIELDS = ("output", "spans", "calls", "cost_usd")


def _reply(result: Any) -> dict[str, Any]:
    if isinstance(result, dict) and "output" in result and set(result) <= set(REPLY_FIELDS):
        return {key: result.get(key) for key in REPLY_FIELDS}
    return {"output": result}


def serve(handler: Callable[[Any], Any], *, ready: dict[str, Any] | None = None,
          stdin: TextIO | None = None, stdout: TextIO | None = None, marker: str = MARKER) -> None:
    """Answer ``{"id", "payload"}`` lines until stdin closes.

    ``handler(payload)`` may be sync or async. Returning a dict with only
    ``output``/``spans``/``calls``/``cost_usd`` fills those Observation fields;
    any other value is the output. One event loop serves every request, so
    async clients created on the first call stay usable on later calls.
    """
    source, sink = stdin or sys.stdin, stdout or sys.stdout

    def send(message: dict[str, Any]) -> None:
        sink.write(marker + json.dumps(message, ensure_ascii=False, default=str) + "\n")
        sink.flush()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        send({"ready": True, **(ready or {})})
        while True:
            line = await loop.run_in_executor(None, source.readline)
            if not line:
                return
            if not line.strip():
                continue
            request = json.loads(line)
            started = time.perf_counter()
            try:
                result = handler(request["payload"])
                if inspect.isawaitable(result):
                    result = await result
                message = _reply(result)
            except Exception as exc:  # The evaluator records the type as a failed run.
                message = {"error_type": type(exc).__name__}
            message.update(id=request.get("id"), worker_ms=round((time.perf_counter() - started) * 1000, 3))
            send(message)

    asyncio.run(run())


def _load(target: str) -> Callable[[Any], Any]:
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise SystemExit("usage: worker.py package.module:function")
    return getattr(importlib.import_module(module_name), attribute)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: worker.py package.module:function")
    sys.path.insert(0, "")
    serve(_load(sys.argv[1]), ready={"handler": sys.argv[1]})
