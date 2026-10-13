"""Move existing subscriptions onto a tenant's new service and office ids.

A TEVIS tenant can renumber every service and office id at once (five did
around 2026-10-10: hagen, dresden, duesseldorf, kassel, moenchengladbach). A
poll for a dead id returns no slots and no error, so every subscription still
holding the old ids goes silent without anyone noticing. The catalog fix ships
the new ids to the sign-up form; this script carries the subscriptions over.

**The map** is a JSON file `{city: {"services": {old: new}, "locations": {old:
new}}}`, built by matching names between the old catalog and the live site and
committed next to this script (`catalog_remap_2026-10-10.json`). Within each
city and namespace no new id may also be an old one, or a second run would
remap again; the script refuses such a map.

For every not-deleted subscription in a mapped city, mail and app rows alike,
ids that are keys in the map are replaced, anything else is left as it is
(`"all"` locations stay `"all"`). Only changed rows are written, all in one
transaction. Ids in neither the map's keys nor its values are counted under
`unknown`: they were already dead before the renumbering, or the map missed
one. Investigate a non-zero count before applying.

Dry run by default; `--apply` writes. Idempotent: a second run changes nothing.

    python scripts/remap_catalog_ids.py scripts/catalog_remap_2026-10-10.json --db /data/app.db
    python scripts/remap_catalog_ids.py scripts/catalog_remap_2026-10-10.json --db /data/app.db --apply

On the VPS the DB lives inside the poller container:

    docker exec buergerwecker-poller-1 python scripts/remap_catalog_ids.py \\
        scripts/catalog_remap_2026-10-10.json --db /data/app.db
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sqlite3
import sys

# Same bootstrap as backfill_day_keys.py: `python scripts/...` puts this
# directory on sys.path, not the repo root, and the poller image has no
# installed copy of `app`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.models import Filter  # noqa: E402


def _check(mapping: dict) -> None:
    for city, m in mapping.items():
        for ns in ("services", "locations"):
            clash = set(m[ns]) & set(m[ns].values())
            if clash:
                raise ValueError(f"{city} {ns}: new ids {sorted(clash)} are also "
                                 f"old ids, so a rerun would remap them again")


def remap(conn: sqlite3.Connection, mapping: dict, *, apply: bool) -> dict:
    _check(mapping)
    stats: dict[str, dict[str, int]] = {}
    conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
    try:
        for city, m in mapping.items():
            svc, loc = m["services"], m["locations"]
            known_svc, known_loc = set(svc) | set(svc.values()), set(loc) | set(loc.values())
            st = stats[city] = {"scanned": 0, "changed": 0, "unknown": 0}
            rows = conn.execute(
                "SELECT id, filters_json FROM subscriptions "
                "WHERE city=? AND deleted_at IS NULL", (city,)).fetchall()
            for sub_id, filters_json in rows:
                st["scanned"] += 1
                f = Filter.from_json(filters_json)
                old_locs = [] if f.locations == "all" else f.locations
                st["unknown"] += (sum(t not in known_svc for t in f.appointment_types)
                                  + sum(x not in known_loc for x in old_locs))
                # dict.fromkeys: a row holding both an old id and its new one
                # must not end up with the new one twice.
                new = dataclasses.replace(
                    f,
                    appointment_types=list(dict.fromkeys(
                        svc.get(t, t) for t in f.appointment_types)),
                    locations=f.locations if f.locations == "all" else list(
                        dict.fromkeys(loc.get(x, x) for x in f.locations)))
                if new == f:
                    continue
                st["changed"] += 1
                if apply:
                    conn.execute("UPDATE subscriptions SET filters_json=? WHERE id=?",
                                 (new.to_json(), sub_id))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("map", help="JSON map, e.g. scripts/catalog_remap_2026-10-10.json")
    ap.add_argument("--db", required=True, help="path to app.db")
    ap.add_argument("--apply", action="store_true",
                    help="write the remapped filters (default: report only)")
    args = ap.parse_args(argv)

    mapping = json.loads(pathlib.Path(args.map).read_text())
    # isolation_level=None so the explicit BEGIN IMMEDIATE above is the only
    # transaction; timeout waits out the live poller's write lock.
    conn = sqlite3.connect(args.db, timeout=30.0, isolation_level=None)
    stats = remap(conn, mapping, apply=args.apply)

    print(f"{'city':<20}{'scanned':>9}{'changed':>9}{'unknown':>9}")
    for city, st in stats.items():
        print(f"{city:<20}{st['scanned']:>9}{st['changed']:>9}{st['unknown']:>9}")
    print("applied." if args.apply else "dry run: nothing written. Re-run with --apply.")
    if any(st["unknown"] for st in stats.values()):
        print("\nWARNING: some subscriptions hold ids the map does not know. They "
              "are left as they are; check whether the map missed one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
