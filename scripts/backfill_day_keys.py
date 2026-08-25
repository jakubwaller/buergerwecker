"""Backfill day-granularity seen_slots keys before flipping a tenant to
`notify_granularity: "day"`.

Without this, the first cycle after the flip finds no day key for anybody and
mails every subscriber once about a day they have usually just been told about
— one last round of exactly the noise the setting exists to remove. Measured on
muenster-kfz before its flip: 50 of 66 subscribers had already been told about
the live date, 40 of them more than once, one of them eight times.

**How it recovers the dates.** `seen_slots` stores only a sha256, so the dates
cannot be read back out of it. They can be *enumerated*: a tenant's slot
identity is (date, time, office, service), and all four are small, bounded sets
— the catalog knows its offices and services, the dates that matter span at most
a year, and TEVIS/smartCJM times are minutes of a day. Hashing that space and
testing each candidate against the rows a subscriber actually holds recovers
every (date, office, service) they were told about, exactly.

Anything not recovered is not a correctness problem: that subscriber gets one
redundant mail, the same as with no backfill at all. The `unrecognized` count in
the report is the honest measure of how complete the run was — investigate it if
it is not near zero, because it means the enumeration missed part of the space
(an unusual time, or an office the catalog no longer lists).

Dry run by default; `--apply` writes. Idempotent — a second run inserts nothing.

    python scripts/backfill_day_keys.py muenster-kfz --db /data/app.db
    python scripts/backfill_day_keys.py muenster-kfz --db /data/app.db --apply

On the VPS the DB lives inside the poller container:

    docker exec termine-notifier-poller-1 \
        python scripts/backfill_day_keys.py muenster-kfz --db /data/app.db
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys
from datetime import date, timedelta

# Running `python scripts/backfill_day_keys.py` puts *this* directory on
# sys.path, not the repo root, so `app` would not import. The poller image
# installs only the dependencies from pyproject.toml (the `app/` tree is copied
# in afterwards and reached via WORKDIR), so there is no installed copy to fall
# back on there either — in-container this bootstrap is the only thing that
# makes the runbook's command work.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.catalog import load_catalog  # noqa: E402
from app.models import Filter, Slot  # noqa: E402

# seen_slots rows are pruned at 7 days, so anything still present was recorded
# recently — but the *slot* it points at can sit far in the future (Münster's
# Führerschein Pflichtumtausch ran 16 days out on 2026-08-25, and a quieter
# tenant can be months). Past days are included because a row can outlive the
# day it names by up to the prune window.
DAYS_BACK = 14
DAYS_AHEAD = 400


def _services_in_play(conn: sqlite3.Connection, city: str, catalog) -> set[str]:
    """Every service id worth enumerating: what the catalog offers now, plus
    what subscribers actually hold. A service withdrawn from the catalog (or
    excluded under `exclude_services`) still has live subscriptions and live
    seen_slots rows, and skipping it would leave exactly those people with the
    redundant mail this script exists to prevent."""
    services = set(catalog.appointment_types.values())
    for row in conn.execute(
            "SELECT filters_json FROM subscriptions WHERE city=?", (city,)):
        try:
            services.update(Filter.from_json(row["filters_json"]).appointment_types)
        except Exception:
            continue
    return {str(s) for s in services if s}


def _locations_in_play(conn: sqlite3.Connection, city: str, catalog) -> set[str]:
    locations = set(catalog.locations.values())
    for row in conn.execute(
            "SELECT filters_json FROM subscriptions WHERE city=?", (city,)):
        try:
            locs = Filter.from_json(row["filters_json"]).locations
        except Exception:
            continue
        if locs != "all":
            locations.update(locs)
    return {str(loc) for loc in locations if loc}


def _seen_rows(conn: sqlite3.Connection, city: str) -> dict[int, dict[str, str]]:
    """subscription_id → {slot_hash: sent_at} for the tenant's active subs."""
    rows = conn.execute(
        """SELECT s.subscription_id sid, s.slot_hash h, s.sent_at t
             FROM seen_slots s
             JOIN subscriptions sub ON sub.id = s.subscription_id
            WHERE sub.city = ?
              AND sub.deleted_at IS NULL
              AND sub.confirmed_at IS NOT NULL
              AND sub.expires_at > datetime('now')""", (city,)).fetchall()
    out: dict[int, dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["sid"], {})[r["h"]] = r["t"]
    return out


def backfill(conn: sqlite3.Connection, city: str, *, apply: bool,
             days_ahead: int = DAYS_AHEAD) -> dict:
    catalog = load_catalog(city)
    if catalog.notify_granularity != "day":
        print(f"note: {city} is not set to notify_granularity=day. Backfilling "
              f"anyway is harmless — the keys are simply unused until it is.",
              file=sys.stderr)

    seen = _seen_rows(conn, city)
    if not seen:
        return {"subs": 0, "rows": 0, "recognized": 0, "already_day_keys": 0,
                "unrecognized": 0, "keys": 0, "written": 0}

    services = _services_in_play(conn, city, catalog)
    locations = _locations_in_play(conn, city, catalog)
    wanted = {h for rows in seen.values() for h in rows}

    # One pass over the candidate space, testing against the hashes actually
    # held. Inverted deliberately: building the full hash→slot map would be
    # hundreds of megabytes on a multi-office tenant, while the rows we are
    # trying to explain number in the hundreds.
    found: dict[str, tuple[str, str, str]] = {}
    already_day: set[str] = set()
    start = date.today() - timedelta(days=DAYS_BACK)
    for offset in range(DAYS_BACK + days_ahead):
        day = (start + timedelta(days=offset)).isoformat()
        for loc in locations:
            for svc in services:
                # A rerun sees the day keys a previous run wrote. They can never
                # match a slot hash, so without recognising them here every one
                # of them lands in `unrecognized` — the single number the
                # runbook tells you to check before applying — and a clean
                # second run reads as a broken one.
                day_key = Slot(day, "00:00", loc, svc, "").day_hash()
                if day_key in wanted:
                    already_day.add(day_key)
                for minute in range(24 * 60):
                    slot = Slot(day, f"{minute // 60:02d}:{minute % 60:02d}",
                                loc, svc, "")
                    h = slot.hash()
                    if h in wanted:
                        found[h] = (day, loc, svc)
        if len(found) + len(already_day) == len(wanted):
            break  # every row explained; the rest of the calendar is empty

    # Earliest sighting wins, so the day key ages out on the same schedule the
    # original rows would have — a backfilled key must not outlive them.
    to_write: dict[tuple[int, str], str] = {}
    recognized = 0
    for sid, rows in seen.items():
        for h, sent_at in rows.items():
            hit = found.get(h)
            if hit is None:
                continue
            recognized += 1
            day, loc, svc = hit
            key = Slot(day, "00:00", loc, svc, "").day_hash()
            prev = to_write.get((sid, key))
            if prev is None or sent_at < prev:
                to_write[(sid, key)] = sent_at

    written = 0
    if apply and to_write:
        with conn:
            for (sid, key), sent_at in to_write.items():
                cur = conn.execute(
                    "INSERT OR IGNORE INTO seen_slots "
                    "(subscription_id, slot_hash, sent_at) VALUES (?,?,?)",
                    (sid, key, sent_at))
                written += cur.rowcount

    total_rows = sum(len(r) for r in seen.values())
    already = sum(1 for rows in seen.values() for h in rows if h in already_day)
    return {"subs": len(seen), "rows": total_rows, "recognized": recognized,
            "already_day_keys": already,
            "unrecognized": total_rows - recognized - already,
            "keys": len(to_write), "written": written}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("city", help="tenant slug, e.g. muenster-kfz")
    ap.add_argument("--db", required=True, help="path to app.db")
    ap.add_argument("--apply", action="store_true",
                    help="write the keys (default: report only)")
    ap.add_argument("--days-ahead", type=int, default=DAYS_AHEAD,
                    help="how far forward to enumerate dates. The scan is "
                         "days x offices x services x 1440 and stops early "
                         "once every row is explained, so a one-office tenant "
                         "is seconds; lower this for a tenant with many "
                         "offices if the run drags.")
    args = ap.parse_args(argv)

    # The live poller writes to this DB while the backfill runs (that is the
    # point — it runs before the stack is recreated), so wait out a held write
    # lock rather than dying on it. Python's default is 5s; a cycle mid-flush
    # can exceed that.
    conn = sqlite3.connect(args.db, timeout=30.0)
    conn.row_factory = sqlite3.Row
    stats = backfill(conn, args.city, apply=args.apply,
                     days_ahead=args.days_ahead)

    print(f"tenant:              {args.city}")
    print(f"active subscribers:  {stats['subs']}")
    print(f"seen_slots rows:     {stats['rows']}")
    print(f"  recognized:        {stats['recognized']}")
    print(f"  already day keys:  {stats['already_day_keys']}")
    print(f"  unrecognized:      {stats['unrecognized']}")
    print(f"day keys implied:    {stats['keys']}")
    if args.apply:
        print(f"rows written:        {stats['written']}")
    else:
        print("dry run — nothing written. Re-run with --apply.")
    if stats["unrecognized"]:
        print(f"\nWARNING: {stats['unrecognized']} rows could not be mapped to a "
              f"(date, office, service). Each is one subscriber who may still "
              f"get a single redundant mail. Check that the catalog lists every "
              f"office and service those subscriptions use.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
