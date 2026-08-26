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
                        polled_by_city: dict | None = None,
                        *, now: datetime | None = None) -> None:
    """Persist one availability sample per due tenant.

    `slots_by_city` maps city → list[Slot] (all slots seen this cycle, already
    deduped). Counts are grouped by (service_uuid, location_uuid).

    `polled_by_city` maps city → set of service_uuids whose poll *succeeded*
    this cycle. A polled service with no slot anywhere gets an explicit
    (service_uuid, '', 0) row — that is what lets the reader tell "we looked
    and found nothing" (scarcity) apart from "nobody was subscribed, so we
    never looked" (coverage). A failed poll records nothing for the service.

    Never raises: analytics must not be able to break a polling cycle.
    """
    now = now or datetime.utcnow()
    now_iso = now.isoformat()
    polled_by_city = polled_by_city or {}
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
            seen_services = {svc for svc, _ in counts}
            for svc in polled_by_city.get(city, ()):
                if svc not in seen_services:
                    counts[(svc, "")] = 0
            if not counts:
                # Still record the sample point, so the city-level time series
                # has no hole even when every poll failed. Empty uuids mark it.
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
    """Per (city, type, office) stats over `days`, newest sample included.

    Every number is relative to the samples where the service was actually
    *polled* (someone was subscribed and the scrape succeeded), so scarcity
    and coverage don't get conflated:

    - `coverage`: % of the city's samples in which this service was polled.
    - `avg_slots`: mean free slots over polled samples, absence counted as 0.
    - `zero_rate`: % of polled samples where this office had nothing — real
      scarcity, the number that matters to a subscriber.

    A polled service that never had a slot at any office surfaces as one row
    with an empty location_uuid. Samples from before the polled-marker existed
    undercount coverage (presence is the floor), never scarcity.
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
        # Any row for a service in a sample — slots seen, or the explicit
        # (service, '', 0) marker — means the service was polled then.
        polled = {
            (r["city"], r["service_uuid"]): r["n"] for r in conn.execute(
                "SELECT city, service_uuid, COUNT(DISTINCT sampled_at) AS n "
                "FROM availability_samples "
                f"WHERE sampled_at > datetime('now','-{int(days)} days') "
                "  AND service_uuid != '' "
                "GROUP BY city, service_uuid"
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT city, service_uuid, location_uuid, "
            "  COUNT(*) AS samples, SUM(n_slots) AS sum_slots, "
            "  MAX(n_slots) AS max_slots, "
            "  SUM(CASE WHEN n_slots = 0 THEN 1 ELSE 0 END) AS zero_samples "
            "FROM availability_samples "
            f"WHERE sampled_at > datetime('now','-{int(days)} days') "
            "  AND service_uuid != '' AND location_uuid != '' "
            "GROUP BY city, service_uuid, location_uuid"
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    seen_services = set()
    for r in rows:
        key = (r["city"], r["service_uuid"])
        seen_services.add(key)
        n_polled = polled.get(key, r["samples"]) or r["samples"]
        total = samples_per_city.get(r["city"], n_polled) or n_polled
        zeros = (n_polled - r["samples"]) + r["zero_samples"]
        out.append({
            "city": r["city"],
            "service_uuid": r["service_uuid"],
            "location_uuid": r["location_uuid"],
            "avg_slots": round((r["sum_slots"] or 0) / n_polled, 1),
            "max_slots": r["max_slots"] or 0,
            "samples": r["samples"],
            "coverage": round(100 * n_polled / total),
            "zero_rate": round(100 * zeros / n_polled),
        })
    # Polled services that never produced a single slot at any office.
    for (city, svc), n_polled in polled.items():
        if (city, svc) in seen_services:
            continue
        total = samples_per_city.get(city, n_polled) or n_polled
        out.append({
            "city": city, "service_uuid": svc, "location_uuid": "",
            "avg_slots": 0.0, "max_slots": 0, "samples": 0,
            "coverage": round(100 * n_polled / total), "zero_rate": 100,
        })
    out.sort(key=lambda r: (r["city"], -r["avg_slots"]))
    return out


def availability_daily(conn: sqlite3.Connection, *, days: int = 14) -> list[dict]:
    """Per-city daily mean of free slots *per polled service* — the trend line.

    Normalising by the number of services polled in each sample keeps the
    series comparable across subscription churn: a subscriber appearing for a
    slot-flooded service no longer spikes the whole city's line. The all-failed
    marker sample ('' service) divides 0 by 1 and correctly reads as 0.
    """
    try:
        rows = conn.execute(
            "SELECT city, day, AVG(per_service) AS avg_per_service FROM ("
            "  SELECT city, date(sampled_at) AS day, sampled_at, "
            "         CAST(SUM(n_slots) AS REAL) "
            "           / COUNT(DISTINCT service_uuid) AS per_service "
            "  FROM availability_samples "
            f"  WHERE sampled_at > datetime('now','-{int(days)} days') "
            "  GROUP BY city, sampled_at"
            ") GROUP BY city, day ORDER BY day"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"city": r["city"], "day": r["day"],
             "avg_per_service": round(r["avg_per_service"] or 0, 1)} for r in rows]


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


def _day_series(conn: sqlite3.Connection, days: int, select_sql: str) -> list[dict]:
    """Run `select_sql` once per UTC day of the last `days` days, oldest first.

    The recursive CTE zero-fills: a quiet day is a row with zeros, not a hole,
    so a column chart keeps its time axis honest. `select_sql` sees the
    columns `day` (YYYY-MM-DD) and `cutoff` — the end of that day, clamped to
    now for today, so the last point agrees with the live headline figures.
    """
    try:
        rows = conn.execute(
            "WITH RECURSIVE days(day) AS ("
            f"  SELECT date('now','-{int(days) - 1} days') "
            "  UNION ALL SELECT date(day,'+1 day') FROM days WHERE day < date('now')"
            "), spans AS ("
            "  SELECT day, min(datetime(day,'+1 day'), datetime('now')) AS cutoff "
            "  FROM days"
            ") "
            f"SELECT day, {select_sql} FROM spans ORDER BY day"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def subscribers_daily(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    """Distinct active subscribers (people, not rows) at the end of each UTC day.

    Reconstructed from the subscriptions table rather than snapshotted: a
    subscription was active on a day if it had been confirmed by then, was
    not yet deleted, and had not expired. `expires_at` only ever moves
    forward (renewal), so a paused-then-renewed subscription reads as active
    across its pause — a small overcount. Housekeeping hard-purges rows 30
    days after deletion, so points older than that undercount; the default
    window stops exactly where the record is still complete.
    """
    return _day_series(conn, days, (
        "(SELECT COUNT(DISTINCT lower(email)) FROM subscriptions "
        " WHERE confirmed_at IS NOT NULL AND confirmed_at <= cutoff "
        "   AND (deleted_at IS NULL OR deleted_at > cutoff) "
        "   AND datetime(expires_at) > cutoff) AS people, "
        "(SELECT COUNT(*) FROM subscriptions "
        " WHERE confirmed_at IS NOT NULL AND confirmed_at <= cutoff "
        "   AND (deleted_at IS NULL OR deleted_at > cutoff) "
        "   AND datetime(expires_at) > cutoff) AS subscriptions"
    ))


def cancellations_daily(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    """Distinct people whose subscriptions ended on each UTC day, by cause.

    `deleted_at` is stamped both by an unsubscribe and by housekeeping
    soft-deleting an expired subscription after its grace period. The table
    keeps no reason column, so the split is inferred: a deletion that comes
    after `expires_at` is an expiry (or a cancellation during the grace
    pause, which amounts to the same thing), anything earlier is a person
    choosing to leave. Never-confirmed sign-ups are excluded — nobody was
    subscribed, so nothing was cancelled.
    """
    live = "confirmed_at IS NOT NULL AND deleted_at >= day AND deleted_at < cutoff"
    return _day_series(conn, days, (
        f"(SELECT COUNT(DISTINCT lower(email)) FROM subscriptions WHERE {live}) "
        "  AS people, "
        f"(SELECT COUNT(*) FROM subscriptions WHERE {live}) AS subscriptions, "
        f"(SELECT COUNT(DISTINCT lower(email)) FROM subscriptions WHERE {live} "
        "   AND datetime(expires_at) > deleted_at) AS unsubscribed, "
        f"(SELECT COUNT(DISTINCT lower(email)) FROM subscriptions WHERE {live} "
        "   AND datetime(expires_at) <= deleted_at) AS expired"
    ))
