"""Time-series analytics: appointment-slot availability + signup usage.

Two independent things live here:

* **Availability** — a periodic sample of how many free slots each
  (tenant, appointment type, office) is showing. The poller already fetches
  this data every cycle; we persist a thinned-out copy so the admin page can
  answer "is Leipzig actually scarce, and which office is the bottleneck?"
  without re-polling upstream. Sampling is throttled per tenant
  (`ANALYTICS_SAMPLE_MINUTES`, default 15) — a per-cycle write would be 60×
  the rows for no extra signal.

* **Usage** — daily signup/confirmation counts, derived on the fly from
  `subscriptions.created_at`. No new writes: the rows are already there, and
  housekeeping only hard-purges long-deleted subscriptions, so recent history
  is intact.
"""
from __future__ import annotations
import os
import sqlite3
from datetime import datetime

# Per-tenant minimum gap between availability samples.
SAMPLE_INTERVAL_MINUTES = int(os.environ.get("ANALYTICS_SAMPLE_MINUTES", "15"))
# How much history the admin page keeps (housekeeping prunes past this).
RETENTION_DAYS = int(os.environ.get("ANALYTICS_RETENTION_DAYS", "90"))


def record_availability(conn: sqlite3.Connection, slots_by_city: dict,
                        *, now: datetime | None = None) -> None:
    """Persist one availability sample per due tenant.

    `slots_by_city` maps city → list[Slot] (all slots seen this cycle, already
    deduped). Counts are grouped by (service_uuid, location_uuid). A tenant
    that was polled but returned nothing still gets rows only if it has none —
    scarcity is visible as an *absence* of rows for that (type, office), which
    the reader treats as zero.

    Never raises: analytics must not be able to break a polling cycle.
    """
    now = now or datetime.utcnow()
    now_iso = now.isoformat()
    try:
        for city, slots in slots_by_city.items():
            row = conn.execute(
                "SELECT MAX(sampled_at) AS last FROM availability_samples WHERE city=?",
                (city,),
            ).fetchone()
            last = row["last"] if row else None
            if last:
                try:
                    age = (now - datetime.fromisoformat(last)).total_seconds()
                except ValueError:
                    age = None
                if age is not None and age < SAMPLE_INTERVAL_MINUTES * 60:
                    continue
            counts: dict[tuple[str, str], int] = {}
            for s in slots:
                key = (s.service_uuid, s.location_uuid)
                counts[key] = counts.get(key, 0) + 1
            if not counts:
                # Still record the sample point, so "we looked and found zero"
                # is distinguishable from "we never looked". Empty uuids mark it.
                counts[("", "")] = 0
            conn.executemany(
                "INSERT INTO availability_samples "
                "(sampled_at, city, service_uuid, location_uuid, n_slots) "
                "VALUES (?,?,?,?,?)",
                [(now_iso, city, svc, loc, n) for (svc, loc), n in counts.items()],
            )
    except sqlite3.Error:
        pass


def prune_availability(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"DELETE FROM availability_samples "
        f"WHERE sampled_at < datetime('now','-{RETENTION_DAYS} days')"
    )


def availability_summary(conn: sqlite3.Connection, *, days: int = 7) -> list[dict]:
    """Per (city, type, office) averages over `days`, newest sample included.

    `avg_slots` averages over the *samples that included this key*; `zero_rate`
    is the share of that tenant's samples where the key had no slots at all —
    the scarcity number that actually matters to a subscriber.
    """
    try:
        samples_per_city = {
            r["city"]: r["n"] for r in conn.execute(
                "SELECT city, COUNT(DISTINCT sampled_at) AS n "
                "FROM availability_samples "
                f"WHERE sampled_at > datetime('now','-{int(days)} days') "
                "GROUP BY city"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT city, service_uuid, location_uuid, "
            "  COUNT(*) AS samples, AVG(n_slots) AS avg_slots, "
            "  MAX(n_slots) AS max_slots, "
            "  SUM(CASE WHEN n_slots = 0 THEN 1 ELSE 0 END) AS zero_samples "
            "FROM availability_samples "
            f"WHERE sampled_at > datetime('now','-{int(days)} days') "
            "  AND service_uuid != '' "
            "GROUP BY city, service_uuid, location_uuid "
            "ORDER BY city, avg_slots DESC"
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        total = samples_per_city.get(r["city"], r["samples"]) or r["samples"]
        # Samples where this key was absent entirely count as zeros too.
        zeros = (total - r["samples"]) + r["zero_samples"]
        out.append({
            "city": r["city"],
            "service_uuid": r["service_uuid"],
            "location_uuid": r["location_uuid"],
            "avg_slots": round(r["avg_slots"] or 0, 1),
            "max_slots": r["max_slots"] or 0,
            "samples": r["samples"],
            "zero_rate": round(100 * zeros / total) if total else 0,
        })
    return out


def availability_daily(conn: sqlite3.Connection, *, days: int = 14) -> list[dict]:
    """Per-city daily mean of total free slots per sample — the trend line."""
    try:
        rows = conn.execute(
            "SELECT city, day, AVG(total) AS avg_total FROM ("
            "  SELECT city, date(sampled_at) AS day, sampled_at, "
            "         SUM(n_slots) AS total "
            "  FROM availability_samples "
            f"  WHERE sampled_at > datetime('now','-{int(days)} days') "
            "  GROUP BY city, sampled_at"
            ") GROUP BY city, day ORDER BY day"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"city": r["city"], "day": r["day"],
             "avg_total": round(r["avg_total"] or 0, 1)} for r in rows]


def usage_daily(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    """Signups / confirmations / cancellations per UTC day, newest first.

    Derived from the subscriptions table — no separate event log, so a
    hard-purged (long-deleted) subscription drops out of history. Acceptable:
    the purge window is far longer than this report.
    """
    rows = conn.execute(
        "SELECT date(created_at) AS day, COUNT(*) AS signups, "
        "  SUM(CASE WHEN confirmed_at IS NOT NULL THEN 1 ELSE 0 END) AS confirmed, "
        "  SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted "
        "FROM subscriptions "
        f"WHERE created_at > datetime('now','-{int(days)} days') "
        "GROUP BY day ORDER BY day DESC"
    ).fetchall()
    by_city = {}
    for r in conn.execute(
        "SELECT date(created_at) AS day, city, COUNT(*) AS n FROM subscriptions "
        f"WHERE created_at > datetime('now','-{int(days)} days') "
        "GROUP BY day, city"
    ).fetchall():
        by_city.setdefault(r["day"], {})[r["city"]] = r["n"]
    return [{"day": r["day"], "signups": r["signups"],
             "confirmed": r["confirmed"], "deleted": r["deleted"],
             "by_city": by_city.get(r["day"], {})} for r in rows]
