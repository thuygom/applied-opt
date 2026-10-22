"""Metadata-only local spans. Payloads, outputs and exception text are omitted."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Span:
    name: str
    depends_on: tuple[str, ...]
    start_ms: float
    end_ms: float
    status: str
    attempts: int = 0
    cache_status: str = "disabled"
    capability: str | None = None
    error_type: str | None = None
    resource: str | None = None

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "duration_ms": self.duration_ms}


def critical_path_ms(spans: Iterable[Span]) -> float:
    """Longest declared dependency path by execution duration, excluding queue time.

    This is a diagnostic lower bound, not a predicted request latency. Resource
    serialization, routing, network variation and scheduling can add wall time.
    """
    nodes = {span.name: span for span in spans if span.status != "skipped"}
    memo: dict[str, float] = {}
    visiting: set[str] = set()

    def visit(name: str) -> float:
        if name in memo:
            return memo[name]
        if name in visiting:
            raise ValueError("Trace dependencies contain a cycle")
        visiting.add(name)
        span = nodes[name]
        value = span.duration_ms + max((visit(dep) for dep in span.depends_on if dep in nodes), default=0.0)
        visiting.remove(name)
        memo[name] = value
        return value

    return max((visit(name) for name in nodes), default=0.0)
