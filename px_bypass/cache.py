"""
Tiny in-memory TTL cache shared by the v2 routes.

Two stores:
  - listings:  zpid -> PropertyListing  (saves a full Zillow round-trip)
  - resolved:  normalized_address -> zillow_url  (saves the autocomplete hop)

Per-process, no eviction policy beyond TTL — a single
`prune_expired()` call sweeps stale entries lazily on each access.
The size cap keeps memory bounded under pathological input.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    def __init__(self, max_size: int = 50_000) -> None:
        self._data: dict[str, _Entry[T]] = {}
        self._max_size = max_size
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[T]:
        now = time.monotonic()
        with self._lock:
            e = self._data.get(key)
            if e is None or e.expires_at < now:
                if e is not None:
                    del self._data[key]
                self.misses += 1
                return None
            self.hits += 1
            return e.value

    def set(self, key: str, value: T, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            if len(self._data) >= self._max_size:
                # cheap eviction: drop the 10% closest to expiring
                cutoff = sorted(e.expires_at for e in self._data.values())[self._max_size // 10]
                self._data = {
                    k: e for k, e in self._data.items() if e.expires_at > cutoff
                }
            self._data[key] = _Entry(value=value, expires_at=time.monotonic() + ttl_s)

    @property
    def size(self) -> int:
        return len(self._data)


def normalize_address(addr: str) -> str:
    return " ".join(addr.lower().split())
