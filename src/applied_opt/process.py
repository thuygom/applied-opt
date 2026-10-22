"""Evaluator side of the worker protocol: one variant per process.

End-to-end latency includes the pipe round trip. The worker's handler duration
is preserved separately as Observation.worker_ms, not as another DAG stage.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .evaluation import Observation, StopEvaluation
from .worker import MARKER


class WorkerError(RuntimeError):
    """A worker reply without output; ``error_type`` is the handler's exception type."""

    def __init__(self, error_type: str):
        super().__init__(error_type)
        self.error_type = error_type


class ProcessRunner:
    """Run a trusted worker, serializing startup and requests on one pipe.

    ``stop_on_error_types`` is an explicit adapter policy, not provider error
    inference. A StopWorker/StopEvaluation signal also stops the evaluation.
    Transport failures discard the child; the next call starts a fresh worker.
    Killing this child cannot guarantee cancellation of remote provider work.
    """

    def __init__(self, command: Sequence[str], *, cwd: str | Path | None = None, env: Mapping[str, str] | None = None,
                 stdout_log: str | Path | None = None, stderr_log: str | Path | None = None,
                 ready_timeout_s: float = 300.0, marker: str = MARKER, line_limit: int = 64 << 20,
                 stop_on_error_types: Sequence[str] = ()):
        if not command:
            raise ValueError("command is required")
        if not math.isfinite(ready_timeout_s) or ready_timeout_s <= 0:
            raise ValueError("ready_timeout_s must be positive and finite")
        if isinstance(stop_on_error_types, str) or any(not isinstance(v, str) or not v for v in stop_on_error_types):
            raise ValueError("stop_on_error_types must be a sequence of nonempty strings")
        self.command = list(command)
        self.cwd, self.env = cwd, dict(env) if env is not None else None
        self.stdout_log, self.stderr_log = stdout_log, stderr_log
        self.ready_timeout_s, self.marker, self.line_limit = ready_timeout_s, marker, line_limit
        self.stop_on_error_types = frozenset(stop_on_error_types)
        self.ready: dict[str, Any] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._sequence = 0
        self._lock = asyncio.Lock()

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            return await self._start()

    async def _start(self) -> dict[str, Any]:
        if self._process is not None and self._process.returncode is None and self.ready is not None:
            return self.ready
        if self._process is not None:
            await self._discard()
        try:
            stderr = open(self.stderr_log, "a", encoding="utf-8") if self.stderr_log else asyncio.subprocess.DEVNULL
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=stderr, limit=self.line_limit, cwd=self.cwd, env=self.env)
            finally:
                if self.stderr_log:
                    stderr.close()
            ready = await asyncio.wait_for(self._read(), self.ready_timeout_s)
            if ready.get("ready") is not True:
                raise RuntimeError("worker did not announce readiness")
            self.ready = ready
            return ready
        except BaseException:
            await self._discard()
            raise

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
                    reply = json.loads(text[len(self.marker):])
                    if not isinstance(reply, dict):
                        raise RuntimeError("worker reply must be a JSON object")
                    return reply
                if log:
                    log.write(text)
        finally:
            if log:
                log.close()

    async def _discard(self) -> None:
        """Kill/reap a desynchronized direct child before allowing another call."""
        process, self._process = self._process, None
        self.ready = None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        # Drain pipes as well as waiting: wait() alone can block on buffered output.
        await process.communicate()

    async def __call__(self, payload: Mapping[str, Any]) -> Observation:
        async with self._lock:
            await self._start()
            self._sequence += 1
            request_id = self._sequence
            assert self._process is not None and self._process.stdin is not None
            try:
                self._process.stdin.write((json.dumps({"id": request_id, "payload": payload},
                                                      ensure_ascii=False) + "\n").encode())
                await self._process.stdin.drain()
                reply = await self._read()
                if type(reply.get("id")) is not int or reply["id"] != request_id:
                    raise RuntimeError("worker reply out of order")
                if reply.get("error_type") is not None and not isinstance(reply["error_type"], str):
                    raise RuntimeError("invalid worker error_type")
                worker_ms = reply.get("worker_ms")
                if worker_ms is not None and (type(worker_ms) not in (int, float)
                                               or not math.isfinite(worker_ms) or worker_ms < 0):
                    raise RuntimeError("invalid worker_ms")
            except BaseException:
                # Parent timeouts cancel this await; never reuse its pending reply.
                await self._discard()
                raise
            error_type = reply.get("error_type")
            if reply.get("stop_evaluation") is True or error_type in self.stop_on_error_types:
                raise StopEvaluation(str(error_type or "StopEvaluation"))
            if error_type:
                raise WorkerError(str(error_type))
            spans = list(reply.get("spans") or [])
            return Observation(reply.get("output"), spans=spans, calls=reply.get("calls"),
                               cost_usd=reply.get("cost_usd"), worker_ms=worker_ms)

    async def close(self, timeout_s: float = 20.0) -> None:
        async with self._lock:
            process = self._process
            if process is None:
                return
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout_s)
            except asyncio.TimeoutError:
                pass
            finally:
                await self._discard()

    async def __aenter__(self) -> "ProcessRunner":
        await self.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()
