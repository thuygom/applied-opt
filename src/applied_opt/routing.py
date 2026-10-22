"""One conservative routing decision shared by prompts, schemas and reviewers.

The router is provider independent. A decider must explicitly consider every
capability; an omitted capability is not evidence that it can be skipped.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Capability:
    """A prompt section and its identically named JSON output property."""

    name: str
    prompt: str
    output_schema: Mapping[str, Any]
    mandatory: bool = False
    reviewers: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    selected: tuple[str, ...]
    confidence: float
    considered: tuple[str, ...]


@dataclass(frozen=True)
class CompiledRoute:
    enabled: tuple[str, ...]
    available: tuple[str, ...]
    prompt: str
    response_schema: Mapping[str, Any]
    reviewers: tuple[str, ...]
    fallback: bool
    reason: str
    decision_ms: float
    revision: str

    def allows(self, capability: str) -> bool:
        return capability in self.enabled


Decider = Callable[[Mapping[str, Any], tuple[str, ...]], Awaitable[Decision | Mapping[str, Any]]]


class Router:
    """Compile a single route, with full-analysis fallback on uncertainty.

    Confidence is a threshold supplied by the application, not a calibrated
    correctness guarantee. Production users should validate the decider against
    representative labeled cases before permitting capability omission.
    """

    def __init__(
        self,
        capabilities: Iterable[Capability],
        *,
        confidence_threshold: float = 0.95,
        timeout_s: float = 0.5,
    ) -> None:
        items = tuple(capabilities)
        if not items or any(not isinstance(c.name, str) or not c.name for c in items):
            raise ValueError("At least one named capability is required")
        if len({c.name for c in items}) != len(items):
            raise ValueError("Capability names must be unique")
        if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
            raise ValueError("confidence_threshold must be between zero and one")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        self.capabilities = items
        self.names = tuple(c.name for c in items)
        self._by_name = {c.name: c for c in items}
        for capability in items:
            if set(capability.requires) - set(self.names):
                raise ValueError(f"Unknown requirement for capability {capability.name!r}")
            if not isinstance(capability.output_schema, Mapping):
                raise ValueError("output_schema must be a JSON schema mapping")
            json.dumps(dict(capability.output_schema), allow_nan=False)
        self.confidence_threshold = confidence_threshold
        self.timeout_s = timeout_s

    def _closure(self, selected: set[str]) -> set[str]:
        result = set(selected)
        pending = list(selected)
        while pending:
            for dependency in self._by_name[pending.pop()].requires:
                if dependency not in result:
                    result.add(dependency)
                    pending.append(dependency)
        return result

    def _compile(self, selected: set[str], fallback: bool, reason: str, started: float) -> CompiledRoute:
        selected = self._closure(selected | {c.name for c in self.capabilities if c.mandatory})
        enabled = tuple(name for name in self.names if name in selected)
        prompt = "\n\n".join(f"[{c.name}]\n{c.prompt}" for c in self.capabilities if c.name in selected)
        schema = {
            "type": "object",
            "properties": {
                c.name: copy.deepcopy(dict(c.output_schema))
                for c in self.capabilities
                if c.name in selected
            },
            "required": list(enabled),
            "additionalProperties": False,
        }
        reviewers = tuple(dict.fromkeys(
            reviewer for c in self.capabilities if c.name in selected for reviewer in c.reviewers
        ))
        revision = hashlib.sha256(json.dumps(
            {"enabled": enabled, "prompt": prompt, "schema": schema, "reviewers": reviewers},
            sort_keys=True, ensure_ascii=False, allow_nan=False,
        ).encode()).hexdigest()
        return CompiledRoute(
            enabled, self.names, prompt, schema, reviewers, fallback, reason,
            (time.perf_counter() - started) * 1000, revision,
        )

    async def route(
        self,
        payload: Mapping[str, Any],
        decider: Decider | None = None,
        *,
        forced: Iterable[str] = (),
        evidence: Iterable[str] = (),
    ) -> CompiledRoute:
        """Decide once; forced and evidence-backed capabilities cannot be omitted.

        ``evidence`` contains capability names established by application-specific
        checks, not raw user text. No input-length rule is used here.
        """
        started = time.perf_counter()
        if isinstance(forced, str) or isinstance(evidence, str):
            raise ValueError("forced and evidence must be iterables of capability names")
        preserved = set(forced) | set(evidence)
        if preserved - set(self.names):
            raise ValueError("Unknown forced or evidence-backed capability")
        if decider is None:
            return self._compile(set(self.names), True, "missing_decider", started)
        try:
            raw = await asyncio.wait_for(decider(payload, self.names), self.timeout_s)
        except asyncio.TimeoutError:
            return self._compile(set(self.names), True, "timeout", started)
        except Exception:
            # The exception may contain input or secrets; do not retain its text.
            return self._compile(set(self.names), True, "decision_error", started)
        try:
            if isinstance(raw, Decision):
                decision = raw
            elif isinstance(raw, Mapping) and set(raw) == {"selected", "confidence", "considered"}:
                if not isinstance(raw["selected"], (tuple, list)) or not isinstance(raw["considered"], (tuple, list)):
                    raise ValueError("Lists required")
                decision = Decision(tuple(raw["selected"]), raw["confidence"], tuple(raw["considered"]))
            else:
                raise ValueError("Invalid decision shape")
            if not isinstance(decision.selected, (list, tuple)) or not isinstance(decision.considered, (list, tuple)):
                raise ValueError("Lists required")
            if any(not isinstance(n, str) for n in (*decision.selected, *decision.considered)):
                raise ValueError("Names must be strings")
            if len(set(decision.selected)) != len(decision.selected):
                raise ValueError("Duplicate selection")
            if set(decision.selected) - set(self.names):
                raise ValueError("Unknown capability")
            if set(decision.considered) != set(self.names) or len(decision.considered) != len(self.names):
                raise ValueError("Every capability must be considered explicitly")
            if isinstance(decision.confidence, bool) or not isinstance(decision.confidence, (float, int)):
                raise ValueError("Numeric confidence required")
            if not math.isfinite(decision.confidence) or not 0 <= decision.confidence <= 1:
                raise ValueError("Invalid confidence")
        except (ValueError, TypeError, KeyError):
            return self._compile(set(self.names), True, "invalid_decision", started)
        if decision.confidence < self.confidence_threshold:
            return self._compile(set(self.names), True, "low_confidence", started)
        return self._compile(set(decision.selected) | preserved, False, "selected", started)
