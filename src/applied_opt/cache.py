"""Explicit, exact-key, tenant-scoped caching for deterministic read operations."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


class CacheKeyError(ValueError):
    """Inputs cannot be safely represented as a complete exact cache key."""


@dataclass(frozen=True)
class CachePolicy:
    ttl_s: float = 60.0
    deterministic: bool = False
    read_only: bool = False

    def __post_init__(self) -> None:
        if not self.deterministic or not self.read_only:
            raise ValueError("Caching requires explicit deterministic=True and read_only=True")
        if not math.isfinite(self.ttl_s) or self.ttl_s <= 0:
            raise ValueError("Cache TTL must be positive and finite")


@dataclass(frozen=True)
class CacheScope:
    tenant_id: str
    subject_id: str
    authorization_revision: str
    data_revision: str
    model_revision: str
    prompt_revision: str
    reference_time: str

    def validate(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in asdict(self).values()):
            raise CacheKeyError("All cache scope and revision fields must be non-empty strings")


def _json_value(value: Any) -> Any:
    """Reject lossy JSON coercions such as integer dict keys or arbitrary objects."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CacheKeyError("Non-finite floats cannot be cached")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise CacheKeyError("Cache mapping keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_json_value(item) for item in value]
    raise CacheKeyError("Cache inputs must be JSON values; unsupported values bypass caching")


def exact_key(
    namespace: str,
    version: str,
    scope: CacheScope,
    payload: Mapping[str, Any],
    dependencies: Mapping[str, Any],
    *,
    route_revision: str = "full-analysis",
) -> str:
    scope.validate()
    if not isinstance(namespace, str) or not namespace or not isinstance(version, str) or not version:
        raise CacheKeyError("A named, versioned operation is required")
    try:
        encoded = json.dumps({
            "namespace": namespace,
            "version": version,
            "scope": asdict(scope),
            "payload": _json_value(payload),
            "dependencies": _json_value(dependencies),
            "route_revision": route_revision,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise CacheKeyError("Inputs cannot be represented as a safe exact key") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass
class _Entry:
    expires_at: float
    value: Any
    scope: CacheScope


class MemoryCache:
    """Bounded in-process LRU/TTL cache; no persistence or semantic matching.

    Values are copied both on put and get so one request cannot mutate another
    request's result. Duplicate in-flight misses may compute independently.
    """

    def __init__(self, max_entries: int = 256, *, clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                return False, None
            self._entries.move_to_end(key)
            return True, copy.deepcopy(entry.value)

    def put(self, key: str, value: Any, *, ttl_s: float, scope: CacheScope) -> None:
        scope.validate()
        if not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError("Cache TTL must be positive and finite")
        cloned = copy.deepcopy(value)
        with self._lock:
            now = self._clock()
            expired = [k for k, entry in self._entries.items() if entry.expires_at <= now]
            for old in expired:
                del self._entries[old]
            self._entries[key] = _Entry(now + ttl_s, cloned, scope)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def invalidate(self, *, tenant_id: str, subject_id: str | None = None) -> int:
        if not tenant_id:
            raise ValueError("Tenant must be specified for scoped invalidation")
        with self._lock:
            keys = [key for key, entry in self._entries.items()
                    if entry.scope.tenant_id == tenant_id
                    and (subject_id is None or entry.scope.subject_id == subject_id)]
            for key in keys:
                del self._entries[key]
            return len(keys)

    def __len__(self) -> int:
        with self._lock:
            now = self._clock()
            return sum(entry.expires_at > now for entry in self._entries.values())
