from __future__ import annotations
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
import requests
from app.filters import matches
from app.planning import build_plans
from app.repo import active_subscriptions, has_seen_slot
from app.scrapers import get_scraper
from app.http_session import CountingSession
from app.models import Slot

# Imported here so tests can monkey-patch it.
from app.digest import send_digest, flush_digests  # noqa: E402

def run_cycle(conn: sqlite3.Connection, *, max_plans_per_city: int,
              rate_limit_minutes: int, cycle_id: str,
              cfg=None,
              http: requests.Session | None = None) -> None:
    if cfg is None:
        from app.config import load_config
        cfg = load_config()
    subs = active_subscriptions(conn)
    if not subs:
        return
    http = http or CountingSession()
    plans = build_plans([(s.city, s.sub_filter) for s in subs],
                        max_plans_per_city=max_plans_per_city)
    # Collect slots per plan + per-city canary tracking + upstream-call counters
    slots_by_plan: dict[str, list[Slot]] = {}
    cities_with_any_slot: set[str] = set()
    cities_polled: set[str] = set()
    polls_delta: dict[str, int] = {}
    requests_delta: dict[str, int] = {}
    for p in plans:
        cities_polled.add(p.city)
        # Snapshot the HTTP-request counter so we can attribute the requests
        # this single poll makes to its city (a CountingSession exposes it; a
        # plain/mocked session does not, in which case we just skip HTTP counts).
        before = getattr(http, "request_count", None)
        try:
            slots_by_plan[p.key()] = get_scraper(p.city).poll(p, http=http)
            if slots_by_plan[p.key()]:
                cities_with_any_slot.add(p.city)
        except Exception:
            slots_by_plan[p.key()] = []
        polls_delta[p.city] = polls_delta.get(p.city, 0) + 1
        if before is not None:
            requests_delta[p.city] = (requests_delta.get(p.city, 0)
                                      + (http.request_count - before))
    # Update per-city canary state + upstream counters in the typed city_state
    # table. Clear `zero_match_since` when at least one plan returned slots;
    # set it on the first all-zero cycle. The canary write and the counter
    # write touch the same row, so wrap them in one transaction — otherwise a
    # concurrent admin reader could observe a half-updated row (fresh
    # last_polled_at with stale counters, or vice versa).
    from app.db import transaction
    now_iso = datetime.utcnow().isoformat()
    today = now_iso[:10]  # UTC date the *_today counters belong to
    with transaction(conn):
        for city in cities_polled:
            # Ensure the row exists.
            conn.execute(
                "INSERT INTO city_state (city) VALUES (?) "
                "ON CONFLICT (city) DO NOTHING",
                (city,),
            )
            if city in cities_with_any_slot:
                conn.execute(
                    "UPDATE city_state SET zero_match_since=NULL, "
                    "last_polled_at=? WHERE city=?",
                    (now_iso, city),
                )
            else:
                conn.execute(
                    "UPDATE city_state "
                    "SET zero_match_since=COALESCE(zero_match_since, ?), "
                    "    last_polled_at=? "
                    "WHERE city=?",
                    (now_iso, now_iso, city),
                )
            # Upstream poll/request counters. The CASE resets the *_today values
            # lazily when the UTC day rolls over; the all-time totals keep growing.
            pd = polls_delta.get(city, 0)
            rd = requests_delta.get(city, 0)
            conn.execute(
                "UPDATE city_state SET "
                "  polls_today    = (CASE WHEN counts_date = ? THEN polls_today    ELSE 0 END) + ?, "
                "  requests_today = (CASE WHEN counts_date = ? THEN requests_today ELSE 0 END) + ?, "
                "  polls_total    = polls_total    + ?, "
                "  requests_total = requests_total + ?, "
                "  counts_date    = ? "
                "WHERE city = ?",
                (today, pd, today, rd, pd, rd, today, city),
            )
    now = datetime.utcnow()
    rate_cutoff = now - timedelta(minutes=rate_limit_minutes)
    # Fairness: serve longest-waiting subscribers first (never-notified, then
    # oldest last_notified_at). When a burst exceeds the daily send quota, the
    # deferred tail is whoever was most recently served — so nobody is
    # permanently starved across cycles. datetime.min sorts NULLs to the front.
    outbox: list = []
    for sub in sorted(subs, key=lambda s: s.last_notified_at or datetime.min):
        if sub.last_notified_at and sub.last_notified_at > rate_cutoff:
            continue
        # Gather candidate slots from any plan that covers this subscription's filter.
        # Dedupe by hash within the cycle: the same logical slot (day/time/office/
        # service) can surface from two resources (counters) or two overlapping
        # plans — Slot.hash() excludes the resource, so collapse them to one line.
        candidates: list[Slot] = []
        seen_in_cycle: set[str] = set()
        for plan in plans:
            if plan.city != sub.city:
                continue
            if plan.appointment_type not in sub.sub_filter.appointment_types:
                continue
            for slot in slots_by_plan.get(plan.key(), []):
                if not matches(sub.sub_filter, slot):
                    continue
                slot_hash = slot.hash()
                if slot_hash in seen_in_cycle:
                    continue
                if has_seen_slot(conn, sub.id, slot_hash):
                    continue
                seen_in_cycle.add(slot_hash)
                candidates.append(slot)
        if not candidates:
            continue
        # Cache each slot's city + upstream URL so /go/<token> works for
        # any city without hardcoding Leipzig. The scrapers know their
        # own upstream URL format; ask them via the catalog.
        from app.catalog import load_catalog
        scfg = load_catalog(sub.city).scraper_config
        # One transaction for the whole candidate batch: in autocommit each
        # INSERT is its own fsync, and an abundant tenant (leipzig-abh holds
        # 1000+ open slots) times N subscribers would overrun the one-minute
        # cycle on fsyncs alone.
        with transaction(conn):
            for slot in candidates:
                upstream = _build_upstream_url(scfg, slot)
                # The booking_token is a URL-encoded datetime (e.g.
                # ...T17%3a20%3a00%2b02%3a00). The email links to
                # /go/<city>:<booking_token>, and Flask URL-DECODES the path
                # param on click — so the slots_cache key must use the DECODED
                # form, or the /go lookup misses and every link 410s.
                # (upstream_url keeps the encoded token: it sits in a query
                # string the city site decodes itself.)
                #
                # The key is prefixed with the tenant because the bare token is
                # just a wall-clock datetime: two tenants (leipzig, leipzig-abh)
                # sharing the same instant would otherwise collide on the
                # single-column primary key and /go would 302 subscribers to
                # whichever tenant's URL was cached first.
                slot_token = f"{sub.city}:{urllib.parse.unquote(slot.booking_token)}"
                conn.execute(
                    "INSERT INTO slots_cache (slot_token, city, upstream_url) "
                    "VALUES (?, ?, ?) ON CONFLICT (slot_token) DO NOTHING",
                    (slot_token, sub.city, upstream),
                )
        # Stage for batched delivery. seen_slots + last_notified are recorded
        # inside flush_digests, but only for digests that were actually sent —
        # quota-deferred ones stay unrecorded so a later cycle re-sends them.
        send_digest(conn=conn, subscription=sub, matched_slots=candidates,
                    cycle_id=cycle_id, cfg=cfg, sink=outbox)
    flush_digests(conn, outbox, cfg)

def _build_upstream_url(scfg: dict, slot) -> str:
    """Vendor-specific upstream booking URL composition.

    For Smart-CJM, the URL is `{base_url}/?uid={uid}&appointment_reserve={token}`.
    Add new branches when adding non-Smart-CJM vendors.
    """
    vendor = scfg.get("vendor")
    if vendor == "smartcjm":
        return (f"{scfg['base_url']}/?uid={scfg['uid']}"
                f"&appointment_reserve={slot.booking_token}")
    raise RuntimeError(f"no upstream-URL builder for vendor: {vendor}")
