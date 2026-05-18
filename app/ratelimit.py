from __future__ import annotations
import sqlite3
import time
from collections import deque

class IPRateLimiter:
    """In-memory sliding-window per-IP counter. Per-process state; with N
    gunicorn workers the effective limit is N*limit. Acceptable for a
    soft bot deterrent. Do NOT use for security-critical decisions."""
    def __init__(self):
        self._events: dict[str, deque] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.time()
        dq = self._events.setdefault(key, deque())
        while dq and now - dq[0] > window_seconds:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

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
