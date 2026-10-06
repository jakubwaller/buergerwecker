"""The last poll's slots, kept for the app's overview and widget.

The poller already fetches every watched service's free slots each cycle and
then forgets them: the digest path only needs what is new per subscriber.
The app shows a live overview per city ("what is free right now") and a
widget with the earliest slot, and both read from here rather than from the
city's site, so the app adds no upstream request: one GET per watched
Anliegen per cycle stays exactly that. A service nobody watches has no
snapshot, and the overview says so instead of polling it.

One row per poll plan, replaced on every successful poll. A failed poll
leaves the previous row, with its older `polled_at`, so the reader can say
"as of" rather than show an empty list for an outage. Rows older than a day
are pruned: a plan nobody subscribes to any more is no longer polled, and
its snapshot must not outlive the inventory it describes.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime

from app.db import sql_ts
from app.models import Slot

# Soonest slots kept per plan. An abundant tenant lists thousands (Bonn, all
# offices: 2792 on 2026-07-28); the overview shows the soonest few dozen and
# the count of the rest.
MAX_SNAPSHOT_SLOTS = 100
RETENTION_HOURS = 24


def record_snapshots(conn: sqlite3.Connection, plans: list,
                     slots_by_plan: dict[str, list[Slot]],
                     *, now: datetime | None = None) -> None:
    """Replace the snapshot of every plan in `plans` (the ones whose poll
    succeeded this cycle) with what `slots_by_plan` holds for it. Deduped by
    (day, time, office): two counters offering the same minute are one slot
    to a reader, as they are to a subscriber. Never raises: the overview
    must not be able to break a polling cycle."""
    now_ts = sql_ts(now or datetime.utcnow())
    rows = []
    for plan in plans:
        slots = slots_by_plan.get(plan.key(), [])
        seen: set[tuple[str, str, str]] = set()
        compact: list[tuple[str, str, str]] = []
        for s in sorted(slots, key=lambda s: (s.date, s.time_str, s.location_uuid)):
            key = (s.date, s.time_str, s.location_uuid)
            if key in seen:
                continue
            seen.add(key)
            compact.append(key)
        rows.append((plan.key(), plan.city, plan.appointment_type,
                     json.dumps(compact[:MAX_SNAPSHOT_SLOTS]), len(compact), now_ts))
    if not rows:
        return
    try:
        conn.executemany(
            "INSERT INTO slot_snapshots "
            "(plan_key, city, service_uuid, slots_json, n_total, polled_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (plan_key) DO UPDATE SET "
            "slots_json=excluded.slots_json, n_total=excluded.n_total, "
            "polled_at=excluded.polled_at",
            rows,
        )
    except sqlite3.Error as exc:
        print(f"snapshots: not recorded: {exc!r}", flush=True)


def prune_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute(f"DELETE FROM slot_snapshots WHERE polled_at < "
                 f"datetime('now','-{RETENTION_HOURS} hours')")


def city_slots(conn: sqlite3.Connection, city: str) -> list[dict]:
    """What the last polls found for `city`, one entry per watched service:
    `service_uuid`, `polled_at` (the newest plan's), `n_total` (across its
    plans, before the cap) and `slots`, soonest first, as (date, time,
    location_uuid) tuples deduped across the plans that cover the service.
    Empty for a city nobody watches."""
    rows = conn.execute(
        "SELECT service_uuid, slots_json, n_total, polled_at FROM slot_snapshots "
        "WHERE city=? ORDER BY service_uuid, polled_at DESC", (city,)).fetchall()
    by_service: dict[str, dict] = {}
    for r in rows:
        entry = by_service.setdefault(r["service_uuid"], {
            "service_uuid": r["service_uuid"], "polled_at": r["polled_at"],
            "n_total": 0, "_seen": set(), "slots": []})
        entry["n_total"] += r["n_total"]
        try:
            slots = json.loads(r["slots_json"])
        except ValueError:
            slots = []
        for item in slots:
            key = tuple(item)
            if key in entry["_seen"] or len(key) != 3:
                continue
            entry["_seen"].add(key)
            entry["slots"].append(key)
    out = []
    for entry in by_service.values():
        entry.pop("_seen")
        entry["slots"] = sorted(entry["slots"])[:MAX_SNAPSHOT_SLOTS]
        out.append(entry)
    return out
