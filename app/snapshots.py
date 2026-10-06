"""The last poll's slots, kept for the app's overview and widget.

The poller already fetches every watched service's free slots each cycle and
then forgets them: the digest path only needs what is new per subscriber.
The app shows a live overview per city ("what is free right now") and a
widget with the earliest slot, and both read from here rather than from the
city's site, so the app adds no upstream request: one GET per watched
Anliegen per cycle stays exactly that. A service nobody watches has no
snapshot, and the overview says so instead of polling it.

One row per (city, service), the union of every plan that covers the
service, replaced each cycle the service is polled. Keyed by service, not by
plan: a plan's key names its office set, which changes whenever a
subscriber edits offices or the planner collapses to "all", and a row keyed
that way would go stale and still look current. The row is written only
when every plan of the service succeeded this cycle; a failed poll leaves
the previous row, with its older `polled_at`, so the reader can say "as of"
rather than show an empty list for an outage. Rows older than a day are
pruned: a service nobody subscribes to any more is no longer polled, and
its snapshot must not outlive the inventory it describes.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime

from app.db import sql_ts
from app.models import Slot

# Soonest slots kept per service. An abundant tenant lists thousands (Bonn,
# all offices: 2792 on 2026-07-28); the overview shows the soonest few dozen
# and the count of the rest.
MAX_SNAPSHOT_SLOTS = 100
RETENTION_HOURS = 24


def record_snapshots(conn: sqlite3.Connection, plans: list, polled_cities: set,
                     ok_plans: list, slots_by_plan: dict[str, list[Slot]],
                     *, now: datetime | None = None) -> None:
    """Replace the snapshot of every service whose plans all succeeded this
    cycle. `plans` are the cycle's plans, `polled_cities` the cities that
    were due, `ok_plans` the plans whose poll did not raise. Slots are
    deduped by (day, time, office): two counters offering the same minute
    are one slot to a reader, as they are to a subscriber. Never raises:
    the overview must not be able to break a polling cycle."""
    ok_keys = {p.key() for p in ok_plans}
    by_service: dict[tuple[str, str], list[Slot]] = {}
    incomplete: set[tuple[str, str]] = set()
    for plan in plans:
        if plan.city not in polled_cities:
            continue
        svc = (plan.city, plan.appointment_type)
        by_service.setdefault(svc, [])
        if plan.key() in ok_keys:
            by_service[svc].extend(slots_by_plan.get(plan.key(), []))
        else:
            incomplete.add(svc)
    now_ts = sql_ts(now or datetime.utcnow())
    rows = []
    for (city, service), slots in by_service.items():
        if (city, service) in incomplete:
            continue
        seen: set[tuple[str, str, str]] = set()
        compact: list[tuple[str, str, str]] = []
        for s in sorted(slots, key=lambda s: (s.date, s.time_str, s.location_uuid)):
            key = (s.date, s.time_str, s.location_uuid)
            if key in seen:
                continue
            seen.add(key)
            compact.append(key)
        rows.append((city, service, json.dumps(compact[:MAX_SNAPSHOT_SLOTS]),
                     len(compact), now_ts))
    if not rows:
        return
    try:
        conn.executemany(
            "INSERT INTO slot_snapshots "
            "(city, service_uuid, slots_json, n_total, polled_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT (city, service_uuid) DO UPDATE SET "
            "slots_json=excluded.slots_json, n_total=excluded.n_total, "
            "polled_at=excluded.polled_at",
            rows,
        )
    except sqlite3.Error as exc:
        print(f"snapshots: not recorded: {exc!r}", flush=True)


def prune_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute(f"DELETE FROM slot_snapshots WHERE polled_at < "
                 f"datetime('now','-{RETENTION_HOURS} hours')")


def _parse_slots(raw: str) -> list[tuple[str, str, str]]:
    """The compact list back, or as much of it as has the right shape. A
    hand-edited or corrupted row must cost that row's slots, not the city's
    endpoint."""
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if (isinstance(item, list) and len(item) == 3
                and all(isinstance(x, str) for x in item)):
            out.append((item[0], item[1], item[2]))
    return out


def city_slots(conn: sqlite3.Connection, city: str) -> list[dict]:
    """What the last polls found for `city`, one entry per watched service:
    `service_uuid`, `polled_at` (SQLite's UTC shape), `n_total` (before the
    cap) and `slots`, soonest first, as (date, time, location_uuid) tuples.
    Empty for a city nobody watches."""
    rows = conn.execute(
        "SELECT service_uuid, slots_json, n_total, polled_at FROM slot_snapshots "
        "WHERE city=? ORDER BY service_uuid", (city,)).fetchall()
    return [{"service_uuid": r["service_uuid"], "polled_at": r["polled_at"],
             "n_total": r["n_total"], "slots": _parse_slots(r["slots_json"])}
            for r in rows]
