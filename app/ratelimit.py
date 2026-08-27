from __future__ import annotations
import sqlite3
import time
from collections import deque

class IPRateLimiter:
    """In-memory sliding-window per-IP counter. Per-process state; with N
    gunicorn workers the effective limit is N*limit. Acceptable for a
    soft bot deterrent. Do NOT use for security-critical decisions.

    Keys are swept every `sweep_every` hits: a key whose newest event is older
    than the longest window seen can never influence a verdict again, and
    without the sweep every distinct client IP for the life of the worker
    stayed in the dict — scanner traffic from many source addresses made it
    grow without bound.
    """
    sweep_every = 1000

    def __init__(self):
        self._events: dict[str, deque] = {}
        self._hits = 0
        self._max_window = 0

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        self._max_window = max(self._max_window, window_seconds)
        self._hits += 1
        if self._hits % self.sweep_every == 0:
            self._sweep(now)
        dq = self._events.get(key)
        if dq is None:
            dq = self._events[key] = deque()
        while dq and now - dq[0] > window_seconds:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def _sweep(self, now: float) -> None:
        stale = [k for k, dq in self._events.items()
                 if not dq or now - dq[-1] > self._max_window]
        for k in stale:
            del self._events[k]

    def tracked_keys(self) -> int:
        return len(self._events)

GLOBAL_IP_LIMITER = IPRateLimiter()

def email_rate_limit_ok(conn: sqlite3.Connection, email: str,
                        per_day_limit: int) -> bool:
    """DB-backed per-email rate limit (shared across workers).

    Counts subscription rows created for this address in the last 24h.
    Returns True if under the limit.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM subscriptions "
        "WHERE LOWER(email) = LOWER(?) "
        "AND created_at > datetime('now','-1 day')",
        (email,),
    ).fetchone()
    return (row["n"] if row else 0) < per_day_limit
