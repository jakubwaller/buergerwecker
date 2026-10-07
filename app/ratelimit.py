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
        return self.hit_all([(key, limit)], window_seconds) is None

    def hit_all(self, hits: list[tuple[str, int]], window_seconds: int) -> str | None:
        """`hit` for several (key, limit) at once, all or nothing: an event is
        recorded under every key only when every one has room, so a request
        one key refuses does not use up another's. Returns the first full key,
        or None when the request was counted."""
        now = time.time()
        self._max_window = max(self._max_window, window_seconds)
        self._hits += 1
        if self._hits % self.sweep_every == 0:
            self._sweep(now)
        queues = []
        for key, limit in hits:
            dq = self._events.get(key)
            if dq is None:
                dq = self._events[key] = deque()
            while dq and now - dq[0] > window_seconds:
                dq.popleft()
            if len(dq) >= limit:
                return key
            queues.append(dq)
        for dq in queues:
            dq.append(now)
        return None

    def retry_after(self, key: str, window_seconds: int) -> int:
        """Seconds until `key` has room again in a window of `window_seconds`
        (at least 1): when its oldest event ages out."""
        dq = self._events.get(key)
        if not dq:
            return 1
        return max(1, int(window_seconds - (time.time() - dq[0])) + 1)

    def _sweep(self, now: float) -> None:
        stale = [k for k, dq in self._events.items()
                 if not dq or now - dq[-1] > self._max_window]
        for k in stale:
            del self._events[k]

    def tracked_keys(self) -> int:
        return len(self._events)

GLOBAL_IP_LIMITER = IPRateLimiter()

def db_rate_hit(conn: sqlite3.Connection, bucket: str, limit: int,
                window_seconds: int) -> int:
    """DB-backed sliding-window limit, shared across workers (rate_events).

    Records one event in `bucket` and returns 0 when fewer than `limit` are
    already in the last `window_seconds`; otherwise records nothing and
    returns the seconds until the oldest one ages out. The check and the
    insert are one statement, so two workers cannot both take the last place.
    `limit <= 0` disables the limit."""
    if limit <= 0:
        return 0
    window = f"-{int(window_seconds)} seconds"
    cur = conn.execute(
        "INSERT INTO rate_events (bucket) SELECT ? WHERE "
        "(SELECT COUNT(*) FROM rate_events WHERE bucket=? "
        " AND at > datetime('now', ?)) < ?",
        (bucket, bucket, window, int(limit)))
    if cur.rowcount == 1:
        return 0
    age = conn.execute(
        "SELECT CAST(strftime('%s','now') - strftime('%s', MIN(at)) AS INTEGER) "
        "FROM rate_events WHERE bucket=? AND at > datetime('now', ?)",
        (bucket, window)).fetchone()[0]
    return max(1, int(window_seconds) - int(age or 0))

def unverified_devices(conn: sqlite3.Connection, bucket: str,
                       window_seconds: int) -> int:
    """How many devices registered in `bucket` (a client network) in the last
    `window_seconds` have not verified: still waiting, refused, retired or
    deleted. A verified one drops out, so real phones, which verify within
    seconds, never fill a shared network's count; only registrations that
    never prove a working token do."""
    return conn.execute(
        "SELECT COUNT(*) FROM rate_events e "
        "LEFT JOIN push_devices d ON d.id = e.device_id "
        "WHERE e.bucket=? AND e.at > datetime('now', ?) "
        "AND (d.id IS NULL OR d.verified_at IS NULL)",
        (bucket, f"-{int(window_seconds)} seconds")).fetchone()[0]


def record_new_device(conn: sqlite3.Connection, buckets: list[str],
                      device_id: int, window_seconds: int) -> None:
    """Note a new device in each network bucket, and drop the bucket's events
    older than the window: they link a device to a hashed network, and no
    verdict reads them any more."""
    for bucket in buckets:
        conn.execute("DELETE FROM rate_events WHERE bucket=? AND at <= datetime('now', ?)",
                     (bucket, f"-{int(window_seconds)} seconds"))
        conn.execute("INSERT INTO rate_events (bucket, device_id) VALUES (?, ?)",
                     (bucket, device_id))

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
