"""
engine/cache.py — In-memory TTL cache for API responses.

Prevents repeated API calls for the same data within a configurable window.
Each cache instance has its own TTL (time-to-live) in seconds.

Thread-safety note: Python's GIL makes dict reads/writes atomic enough for
single-process bots. If you move to multi-process, swap to a proper store.

No Telegram code. No API calls.
"""

from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Core data structure ───────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    value:      Any
    expires_at: float   # monotonic timestamp (time.monotonic())


class TTLCache:
    """
    Simple in-memory key-value store with per-entry expiry.

    Example
    -------
    cache = TTLCache(ttl_seconds=300)   # 5-minute TTL
    cache.set("key", some_value)
    result = cache.get("key")           # None if expired or missing
    """

    def __init__(self, ttl_seconds: int, name: str = "") -> None:
        self._ttl  = ttl_seconds
        self._name = name or "cache"
        self._store: dict[str, _CacheEntry] = {}
        self._hits   = 0
        self._misses = 0

    # ── Public interface ──────────────────────────────────────────────────────

    def get(self, key: str) -> Any | None:
        """Return cached value or None if missing / expired.

        Uses pop() instead of del to avoid KeyError under concurrent access —
        if another thread evicts the same key between our .get() and the
        expiry removal, pop() silently returns None rather than raising.
        """
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry.expires_at:
            self._store.pop(key, None)   # safe: no KeyError if already removed
            self._misses += 1
            logger.debug("[%s] Cache expired: %s", self._name, key)
            return None
        self._hits += 1
        logger.debug("[%s] Cache hit: %s", self._name, key)
        return entry.value

    def set(self, key: str, value: Any) -> None:
        """Store a value with the configured TTL."""
        self._store[key] = _CacheEntry(
            value=value,
            expires_at=time.monotonic() + self._ttl,
        )
        logger.debug("[%s] Cache set: %s (TTL %ds)", self._name, key, self._ttl)

    def delete(self, key: str) -> None:
        """Remove a specific key."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Flush all entries."""
        self._store.clear()
        logger.info("[%s] Cache cleared.", self._name)

    def has(self, key: str) -> bool:
        """Return True if key exists and is not expired.

        Unlike get(), this does NOT increment hit/miss counters — it is a
        pure existence check intended for conditional logic, not data retrieval.
        """
        entry = self._store.get(key)
        if entry is None:
            return False
        if time.monotonic() > entry.expires_at:
            self._store.pop(key, None)
            return False
        return True

    def purge_expired(self) -> int:
        """
        Remove all expired entries in one pass.

        The base get() evicts entries lazily (on next access).  Calling
        purge_expired() proactively reclaims memory for long-lived processes
        or after a high-traffic period.  Safe to call at any time.

        Returns the number of entries removed.
        """
        now     = time.monotonic()
        # Snapshot with list() before filtering — iterating the live dict while
        # another coroutine/thread calls set() can raise RuntimeError on mutation.
        expired = [k for k, e in list(self._store.items()) if e.expires_at <= now]
        for k in expired:
            self._store.pop(k, None)
        if expired:
            logger.debug("[%s] Purged %d expired entries.", self._name, len(expired))
        return len(expired)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return hit/miss/active-key stats for debugging."""
        now    = time.monotonic()
        active = sum(1 for e in self._store.values() if e.expires_at > now)
        total  = self._hits + self._misses
        ratio  = self._hits / total if total > 0 else 0.0
        return {
            "name":        self._name,
            "ttl_seconds": self._ttl,
            "total_keys":  len(self._store),
            "active_keys": active,
            "hits":        self._hits,
            "misses":      self._misses,
            "hit_ratio":   round(ratio, 3),
        }

    def __repr__(self) -> str:
        s = self.stats()
        return (
            f"TTLCache({self._name!r}, ttl={self._ttl}s, "
            f"active={s['active_keys']}, hits={s['hits']}, misses={s['misses']})"
        )


# ── Pre-configured cache instances ────────────────────────────────────────────
# Imported by data.py and reused across all API calls.
# TTLs chosen to balance freshness against API-Football free-tier quota (≈100/day).

fixture_cache = TTLCache(ttl_seconds=60,          name="fixtures")    # 60 s — today's schedule
odds_cache    = TTLCache(ttl_seconds=120,          name="odds")        # 2 min — pre-KO drift
stats_cache   = TTLCache(ttl_seconds=15 * 60,     name="team_stats")  # 15 min — seasonal aggs
h2h_cache     = TTLCache(ttl_seconds=60 * 60,     name="h2h")         # 1 hr  — history stable
injury_cache  = TTLCache(ttl_seconds=30 * 60,     name="injuries")    # 30 min — pre-KO updates


# ── Generic per-key-TTL cache ─────────────────────────────────────────────────
# Backs get_cached_data(); separate store from the named singletons above.

_GENERIC_STORE: dict[str, _CacheEntry] = {}
_GENERIC_LOCK  = threading.Lock()


def get_cached_data(key: str, fetch_fn: Callable[[], Any], ttl_seconds: int) -> Any:
    """
    Unified fetch-or-cache helper with caller-supplied TTL.

    Behaviour
    ---------
    • Cache hit  (key present and not expired) → return cached value immediately.
    • Cache miss → call ``fetch_fn()``, store result under ``key`` for
      ``ttl_seconds`` seconds, return the fresh value.

    A lock around the store prevents two concurrent callers from both
    deciding on a miss and making duplicate API calls for the same key.

    Parameters
    ----------
    key          : Unique string that identifies this piece of data.
    fetch_fn     : Zero-argument callable that performs the actual API call.
    ttl_seconds  : How long (in seconds) the result should be considered fresh.

    Example
    -------
    data = get_cached_data(
        key="standings:39:2024",
        fetch_fn=lambda: _get("standings", {"league": 39, "season": 2024}),
        ttl_seconds=900,
    )
    """
    with _GENERIC_LOCK:
        now   = time.monotonic()
        entry = _GENERIC_STORE.get(key)
        if entry is not None and now < entry.expires_at:
            logger.debug("[generic] Cache hit: %s", key)
            return entry.value

        # Miss — fetch while still holding the lock so a second concurrent
        # caller waits here instead of also firing an API request.
        logger.debug("[generic] Cache miss: %s — fetching…", key)
        result = fetch_fn()
        _GENERIC_STORE[key] = _CacheEntry(
            value=result,
            expires_at=time.monotonic() + ttl_seconds,
        )
        return result


def all_stats() -> list[dict]:
    """Return stats for every named cache instance (for diagnostics)."""
    named = [c.stats() for c in (
        fixture_cache, odds_cache, stats_cache,
        h2h_cache, injury_cache,
    )]
    # Summarise the generic store separately
    now    = time.monotonic()
    active = sum(1 for e in _GENERIC_STORE.values() if e.expires_at > now)
    named.append({"name": "generic", "total_keys": len(_GENERIC_STORE), "active_keys": active})
    return named


def purge_all_expired() -> int:
    """Sweep every named cache and the generic store; return total entries removed."""
    removed = sum(c.purge_expired() for c in (
        fixture_cache, odds_cache, stats_cache,
        h2h_cache, injury_cache,
    ))
    now     = time.monotonic()
    with _GENERIC_LOCK:
        stale = [k for k, e in list(_GENERIC_STORE.items()) if e.expires_at <= now]
        for k in stale:
            _GENERIC_STORE.pop(k, None)
    removed += len(stale)
    if removed:
        logger.debug("purge_all_expired: removed %d entries total.", removed)
    return removed
