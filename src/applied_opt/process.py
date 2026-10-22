"""Evaluator side of the worker protocol: one variant per process.

Use a ProcessRunner for each variant when baseline and candidate cannot share
an interpreter, for example two revisions of the same package. Latency measured
by ``evaluate`` then includes a small pipe round trip per run; the worker's own
handler time is kept as ``worker_ms`` in the observation spans.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evaluation import Observation
from .worker import MARKER


class WorkerError(RuntimeError):
    """A worker reply without output; ``error_type`` is the handler's exception type."""

    def __init__(self, error_type: str):
        super().__init__(error_type)
        self.error_type = error_type


class ProcessRunner:
    """Start ``command`` (which must run :func:`applied_opt.worker.serve`) and call it per case.

    ``stdout_log``/``stderr_log`` receive the worker's non-protocol output.
    Requests are sent one at a time; ``evaluate`` never overlaps variants.
    """

    def __init__(self, command: Sequence[str], *, cwd: str | Path | None = None, env: Mapping[str, str] | None = None,
                 stdout_log: str | Path | None = None, stderr_log: str | Path | None = None,
                 ready_timeout_s: float = 300.0, marker: str = MARKER, line_limit: int = 64 << 20):
        if not command:
            raise ValueError("command is required")
        self.command = list(command)
        self.cwd, self.env = cwd, dict(env) if env is not None else None
        self.stdout_log, self.stderr_log = stdout_log, stderr_log
        self.ready_timeout_s, self.marker, self.line_limit = ready_timeout_s, marker, line_limit
        self.ready: dict[str, Any] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def start(self) -> dict[str, Any]:
        stderr = open(self.stderr_log, "a", encoding="utf-8") if self.stderr_log else asyncio.subprocess.DEVNULL
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=stderr, limit=self.line_limit, cwd=self.cwd, env=self.env)
        finally:
            if self.stderr_log:
                stderr.close()
        self.ready = await asyncio.wait_for(self._read(), self.ready_timeout_s)
        if self.ready.get("ready") is not True:
            raise RuntimeError("worker did not announce readiness")
        return self.ready

    async def _read(self) -> dict[str, Any]:
        assert self._process is not None and self._process.stdout is not None
        log = open(self.stdout_log, "a", encoding="utf-8") if self.stdout_log else None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    raise RuntimeError("worker exited")
                text = line.decode("utf-8", "replace")
                if text.startswith(self.marker):
                    return json.loads(text[len(self.marker):])
                if log:
                    log.write(text)
        finally:
            if log:
                log.close()

    async def __call__(self, payload: Mapping[str, Any]) -> Observation:
        if self._process is None:
            await self.start()
        async with self._lock:
            self._sequence += 1
            assert self._process is not None and self._process.stdin is not None
            self._process.stdin.write((json.dumps({"id": self._sequence, "payload": payload},
                                                  ensure_ascii=False) + "\n").encode())
            await self._process.stdin.drain()
            reply = await self._read()
        if reply.get("id") != self._sequence:
            raise RuntimeError("worker reply out of order")
        if reply.get("error_type"):
            raise WorkerError(str(reply["error_type"]))
        spans = list(reply.get("spans") or [])
        return Observation(reply.get("output"), spans=spans, calls=reply.get("calls"), cost_usd=reply.get("cost_usd"))

    async def close(self, timeout_s: float = 20.0) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout_s)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def __aenter__(self) -> "ProcessRunner":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()
