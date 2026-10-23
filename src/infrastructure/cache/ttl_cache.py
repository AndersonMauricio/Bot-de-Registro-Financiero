"""Minimal thread-safe in-memory cache with per-entry TTL."""

import threading
import time
from typing import Callable, Generic, Hashable, Optional, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """In-process cache whose entries expire `ttl_seconds` after being set.

    Used from both the event loop and worker threads (Sheets access runs in threads), hence the lock.
    The clock is injectable so expiry can be tested without sleeping.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[K, tuple[V, float]] = {}  # key -> (value, expires_at)

    def get(self, key: K, default: Optional[V] = None, max_stale: float = 0.0) -> Optional[V]:
        """Return the cached value, or `default` if missing or expired.

        `max_stale` lets the caller accept an expired entry for that many extra seconds
        (used to keep serving the last known data while the source is failing).
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return default
            value, expires_at = entry
            if self._clock() < expires_at + max_stale:
                return value
            return default

    def set(self, key: K, value: V) -> None:
        with self._lock:
            self._store(key, value)

    def set_if_absent(self, key: K, value: V) -> bool:
        """Atomically store `value` unless a live entry exists. Returns True when it was stored."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._clock() < entry[1]:
                return False
            self._store(key, value)
            return True

    def delete(self, key: K) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def _store(self, key: K, value: V) -> None:
        self._entries.pop(key, None)  # re-insert so insertion order tracks recency of writes
        self._entries[key] = (value, self._clock() + self.ttl_seconds)
        if len(self._entries) > self.max_entries:
            self._evict()

    def _evict(self) -> None:
        """Drop expired entries first; if still over capacity, drop the oldest writes."""
        now = self._clock()
        for k in [k for k, (_, exp) in self._entries.items() if exp <= now]:
            del self._entries[k]
        while len(self._entries) > self.max_entries:
            del self._entries[next(iter(self._entries))]
