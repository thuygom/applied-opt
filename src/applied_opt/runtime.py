"""Bounded asynchronous DAG execution with explicit optimization contracts."""

from __future__ import annotations

import asyncio
import inspect
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .cache import CacheKeyError, CachePolicy, CacheScope, MemoryCache, exact_key
from .routing import CompiledRoute
from .tracing import Span, critical_path_ms


async def gather_strict(*awaitables: Awaitable[Any]) -> list[Any]:
    """Gather declared-safe work and drain all siblings on failure/cancellation.

    The first raised exception retains its original type. Unlike bare gather,
    sibling tasks cannot keep running after this function exits. This helper
    does not infer independence, impose resource limits, or undo side effects.
    """
    if any(not inspect.isawaitable(item) for item in awaitables):
        # Close native coroutines that otherwise would be left unawaited.
        for item in awaitables:
            if inspect.iscoroutine(item):
                item.close()
        raise TypeError("gather_strict accepts only awaitables")
    tasks: list[asyncio.Future[Any]] = []
    try:
        for item in awaitables:
            tasks.append(asyncio.ensure_future(item))
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for item in awaitables[len(tasks):]:
            if inspect.iscoroutine(item):
                item.close()
        raise


@dataclass(frozen=True)
class RunContext:
    payload: Mapping[str, Any]
    tenant_id: str = ""
    subject_id: str = ""
    authorization_revision: str = ""
    data_revision: str = ""
    model_revision: str = ""
    prompt_revision: str = ""
    reference_time: str = ""
    route: CompiledRoute | None = None

    def cache_scope(self) -> CacheScope:
        return CacheScope(
            self.tenant_id, self.subject_id, self.authorization_revision,
            self.data_revision, self.model_revision, self.prompt_revision, self.reference_time,
        )


@dataclass(frozen=True)
class RetryPolicy:
    """One attempt by default; a single retry owner belongs at this layer.

    Provider SDK retries must be disabled by the application's adapter when this
    policy owns retries. The runtime cannot inspect an SDK's hidden behavior.
    """

    attempts: int = 1
    backoff_s: float = 0.0
    retry_on: tuple[type[Exception], ...] = (TimeoutError,)

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 1:
            raise ValueError("attempts must be a positive integer")
        if not math.isfinite(self.backoff_s) or self.backoff_s < 0:
            raise ValueError("backoff_s must be non-negative and finite")
        if not isinstance(self.retry_on, tuple) or any(
            not isinstance(exc, type) or not issubclass(exc, Exception) for exc in self.retry_on
        ):
            raise ValueError("retry_on must be a tuple of exception classes")


StepFunction = Callable[[RunContext, Mapping[str, Any]], Awaitable[Any]]


@dataclass(frozen=True)
class Step:
    name: str
    run: StepFunction
    depends_on: tuple[str, ...] = ()
    capability: str | None = None
    version: str = "1"
    concurrency_safe: bool = False
    side_effects: bool = False
    idempotent: bool = False
    timeout_s: float = 30.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    cache: CachePolicy | None = None
    resource: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or not callable(self.run):
            raise ValueError("A named callable step is required")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("Step version must be non-empty")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("Step timeout must be positive and finite")
        if self.retry.attempts > 1 and not self.idempotent:
            raise ValueError("Retries require an explicitly idempotent step")
        if self.cache is not None and self.side_effects:
            raise ValueError("Side-effectful steps cannot be cached")
        if not isinstance(self.depends_on, tuple) or len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("depends_on must contain unique dependency names")
        if self.resource is not None and (not isinstance(self.resource, str) or not self.resource):
            raise ValueError("resource must be a non-empty string when supplied")

    @property
    def exclusive(self) -> bool:
        return self.side_effects or not self.concurrency_safe


class RoutingDependencyError(ValueError):
    """An enabled step requires a capability that the route omitted."""


class StepExecutionError(RuntimeError):
    def __init__(self, step_name: str, error_type: str) -> None:
        super().__init__(f"Step {step_name!r} failed ({error_type})")
        self.step_name = step_name
        self.error_type = error_type


@dataclass(frozen=True)
class RunResult:
    outputs: Mapping[str, Any]
    spans: tuple[Span, ...]
    duration_ms: float
    skipped: tuple[str, ...]

    def trace_dict(self) -> dict[str, Any]:
        """Export telemetry without user input or generated content."""
        return {
            "duration_ms": self.duration_ms,
            "critical_path_ms": critical_path_ms(self.spans),
            "skipped": list(self.skipped),
            "spans": [span.to_dict() for span in self.spans],
        }


class Pipeline:
    """Execute a declared DAG; unknown concurrency is serialized by default.

    ``concurrency_safe=True`` is an application contract covering resources,
    dependency-output mutation and external side effects, not an inferred fact.
    Each step must be genuinely asynchronous; blocking CPU or I/O should be
    offloaded explicitly. Inputs/dependency values should be treated immutable.
    """

    def __init__(
        self,
        steps: Iterable[Step],
        *,
        max_concurrency: int = 4,
        cache: MemoryCache | None = None,
        resource_limits: Mapping[str, int] | None = None,
        cache_namespace: str | None = None,
    ) -> None:
        self.steps = tuple(steps)
        if not self.steps or len({step.name for step in self.steps}) != len(self.steps):
            raise ValueError("A non-empty pipeline with unique step names is required")
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self.max_concurrency = max_concurrency
        self.cache = cache if cache is not None else MemoryCache()
        if cache_namespace is not None and (not isinstance(cache_namespace, str) or not cache_namespace):
            raise ValueError("cache_namespace must be a non-empty string when supplied")
        # Separate pipelines cannot accidentally share results for a generic
        # step name. Explicit namespaces opt into cross-instance reuse.
        self.cache_namespace = cache_namespace or uuid.uuid4().hex
        self.resource_limits = dict(resource_limits or {})
        if any(not isinstance(name, str) or not name or isinstance(limit, bool)
               or not isinstance(limit, int) or limit < 1
               for name, limit in self.resource_limits.items()):
            raise ValueError("Resource limits require non-empty names and positive integer limits")
        self._by_name = {step.name: step for step in self.steps}
        for step in self.steps:
            if set(step.depends_on) - set(self._by_name):
                raise ValueError(f"Step {step.name!r} has unknown dependencies")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError("Pipeline contains a cycle")
            if name in visited:
                return
            visiting.add(name)
            for dep in self._by_name[name].depends_on:
                visit(dep)
            visiting.remove(name)
            visited.add(name)

        for name in self._by_name:
            visit(name)

    def _active(self, context: RunContext) -> tuple[tuple[Step, ...], tuple[str, ...]]:
        if context.route is None:
            return self.steps, ()
        if any(step.capability and step.capability not in context.route.available for step in self.steps):
            raise RoutingDependencyError("Pipeline references a capability absent from the route registry")
        active = tuple(step for step in self.steps
                       if step.capability is None or context.route.allows(step.capability))
        names = {step.name for step in active}
        for step in active:
            if set(step.depends_on) - names:
                raise RoutingDependencyError(
                    f"Enabled step {step.name!r} depends on an omitted capability; "
                    "declare capability.requires or keep the dependency enabled"
                )
        return active, tuple(step.name for step in self.steps if step.name not in names)

    async def _run_step(
        self,
        step: Step,
        context: RunContext,
        dependencies: Mapping[str, Any],
        spans: list[Span],
        origin: float,
    ) -> Any:
        started = (time.perf_counter() - origin) * 1000
        status = "failed"
        attempts = 0
        cache_status = "disabled"
        error_type: str | None = None
        key: str | None = None
        scope = context.cache_scope()
        try:
            if step.cache is not None:
                try:
                    key = exact_key(
                        f"{self.cache_namespace}:{step.name}", step.version, scope, context.payload, dependencies,
                        route_revision=context.route.revision if context.route else "full-analysis",
                    )
                except CacheKeyError:
                    cache_status = "bypass_unsafe_key"
                if key is not None:
                    hit, cached = self.cache.get(key)
                    if hit:
                        status, cache_status = "completed", "hit"
                        return cached
                    cache_status = "miss"
            for attempt in range(1, step.retry.attempts + 1):
                attempts = attempt
                try:
                    result = await asyncio.wait_for(step.run(context, dependencies), step.timeout_s)
                    if key is not None and step.cache is not None:
                        try:
                            self.cache.put(key, result, ttl_s=step.cache.ttl_s, scope=scope)
                        except Exception:
                            # An uncopyable result must not break successful computation.
                            cache_status = "bypass_uncopyable_result"
                    status = "completed"
                    return result
                except Exception as exc:
                    if attempt == step.retry.attempts or not isinstance(exc, step.retry.retry_on):
                        raise StepExecutionError(step.name, type(exc).__name__) from None
                    if step.retry.backoff_s:
                        await asyncio.sleep(step.retry.backoff_s)
        except asyncio.CancelledError:
            status, error_type = "cancelled", "CancelledError"
            raise
        except Exception as exc:
            error_type = exc.error_type if isinstance(exc, StepExecutionError) else type(exc).__name__
            raise
        finally:
            spans.append(Span(
                step.name, step.depends_on, started, (time.perf_counter() - origin) * 1000,
                status, attempts, cache_status, step.capability, error_type, step.resource,
            ))

    async def run(self, context: RunContext, *, timeout_s: float = 120.0) -> RunResult:
        """Run with a whole-request deadline, including waits, retries and backoff.

        Cancellation is cooperative. Code that blocks the event loop or suppresses
        cancellation cannot be forcibly stopped by an asyncio runtime.
        """
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Request timeout must be positive and finite")
        active, skipped = self._active(context)
        origin = time.perf_counter()
        outputs: dict[str, Any] = {}
        spans = [Span(name, self._by_name[name].depends_on, 0, 0, "skipped",
                      capability=self._by_name[name].capability) for name in skipped]
        pending = {step.name for step in active}
        running: dict[asyncio.Task[Any], Step] = {}
        try:
            async with asyncio.timeout(timeout_s):
                while pending or running:
                    ready = [step for step in active if step.name in pending
                             and all(dep in outputs for dep in step.depends_on)]
                    for step in ready:
                        if len(running) >= self.max_concurrency or any(s.exclusive for s in running.values()):
                            break
                        if step.exclusive and running:
                            break
                        if step.resource in self.resource_limits and sum(
                            other.resource == step.resource for other in running.values()
                        ) >= self.resource_limits[step.resource]:
                            continue
                        dependencies = MappingProxyType({name: outputs[name] for name in step.depends_on})
                        task = asyncio.create_task(self._run_step(step, context, dependencies, spans, origin))
                        running[task] = step
                        pending.remove(step.name)
                        if step.exclusive:
                            break
                    if not running:
                        if pending:
                            raise RuntimeError("Pipeline made no progress")
                        break
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    # Preserve the declaration/scheduling order when several finish together.
                    for task in [task for task in running if task in done]:
                        step = running.pop(task)
                        outputs[step.name] = task.result()
        except BaseException:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        return RunResult(
            MappingProxyType(outputs), tuple(sorted(spans, key=lambda span: (span.start_ms, span.name))),
            (time.perf_counter() - origin) * 1000, skipped,
        )
