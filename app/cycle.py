from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
import requests
from app.filters import matches
from app.planning import build_plans
from app.repo import active_subscriptions, has_seen_slot
from app.scrapers import get_scraper
from app.http_session import CountingSession
from app.models import Slot
from app.analytics import record_availability

# Imported here so tests can monkey-patch it.
from app.digest import send_digest, flush_digests  # noqa: E402


# Adaptive send cadence.
#
# RATE_LIMIT_MINUTES is a floor, not a schedule: it is the gap a subscriber
# gets when their filter is matching almost nothing — the scarce case, where a
# single slot is worth an immediate mail. The more slots a filter is already
# matching, the less any individual one matters, so the floor is multiplied
# out. Without this the floor is the ONLY bound on volume, and in a plentiful
# tenant the inventory churns faster than it, so every subscriber sits pinned
# to 15-minute mails all day (14 subscribers produced 184 digests on
# 2026-07-27, against a 200/day provider cap).
#
# Thresholds are raw slot counts, and they are deliberately low because vendor
# granularity spans orders of magnitude: measured live on 2026-07-28, an
# all-locations Bonn filter (smartCJM, every free slot) matched 2792 slots
# while an all-locations Braunschweig filter (TEVIS, earliest slot per office
# only) matched 6 — and the Braunschweig subscriber was the one sending 40
# mails a day. So 6 has to land well up the ladder, not near the bottom.
#
# The trade-off that buys: a genuinely scarce Leipzig filter showing ~7 slots
# also gets an hour. That is judged acceptable — seven standing options is not
# an emergency, and nothing is dropped, only batched into the next digest.
# Provisional calibration; re-measure against a daytime sample.
_ABUNDANCE_LADDER = ((2, 1), (5, 2), (15, 4))
_MAX_ABUNDANCE_MULTIPLIER = 8


def adaptive_rate_limit_minutes(base_minutes: int, match_count: int | None, *,
                                max_multiplier: int = _MAX_ABUNDANCE_MULTIPLIER) -> int:
    """Minimum minutes between digests for a subscriber whose filter matched
    `match_count` slots at its last delivered digest.

    `None` — never notified, or a row predating the column — gets the base
    floor: serve a new subscriber fast until we have actually measured them.
    `max_multiplier=1` pins everyone to the base, i.e. the pre-adaptive
    behaviour, which is what makes it a usable kill switch.
    """
    if match_count is None:
        return base_minutes
    multiplier = _MAX_ABUNDANCE_MULTIPLIER
    for threshold, m in _ABUNDANCE_LADDER:
        if match_count <= threshold:
            multiplier = m
            break
    return base_minutes * min(multiplier, max(1, max_multiplier))


def _poll_interval_s(city: str) -> int:
    """Per-tenant minimum seconds between polls (scraper_config key
    `poll_interval_seconds`, default 60 = every cycle). Lets a tenant honor a
    mandated slower cadence — e.g. Berlin's ZMS team requires >=180s between
    requests — without changing the poller's one-minute heartbeat."""
    try:
        from app.catalog import load_catalog
        return int(load_catalog(city).scraper_config.get("poll_interval_seconds", 60))
    except Exception:
        return 60


def _due_cities(conn: sqlite3.Connection, cities: set[str]) -> set[str]:
    """Cities whose poll interval has elapsed since city_state.last_polled_at.

    Default-cadence cities (<=60s) are always due. The 5s grace absorbs cycle
    -boundary jitter so a 180s interval polls every 3rd cycle, not every 4th.
    Unparseable or missing timestamps count as due (fail open: poll)."""
    due: set[str] = set()
    now = datetime.utcnow()
    for city in cities:
        interval = _poll_interval_s(city)
        if interval <= 60:
            due.add(city)
            continue
        row = conn.execute(
            "SELECT last_polled_at FROM city_state WHERE city=?", (city,)
        ).fetchone()
        last = row["last_polled_at"] if row else None
        if not last:
            due.add(city)
            continue
        try:
            elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            due.add(city)
            continue
        if elapsed >= interval - 5:
            due.add(city)
    return due


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
    # Skip tenants whose per-tenant poll interval hasn't elapsed. A skipped
    # city is left out of cities_polled entirely: its canary, counters, and
    # last_polled_at stay untouched, and its subscribers simply see no new
    # candidates this cycle.
    due = _due_cities(conn, {p.city for p in plans})
    for p in plans:
        if p.city not in due:
            continue
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
    # Availability analytics: a thinned-out time series of how many free slots
    # each tenant/type/office is showing. Deduped by slot hash first — the same
    # slot can surface from two resources or two overlapping plans, and counting
    # it twice would inflate the series. Best-effort; never blocks delivery.
    slots_by_city: dict[str, list[Slot]] = {c: [] for c in cities_polled}
    seen_hashes: dict[str, set[str]] = {c: set() for c in cities_polled}
    for p in plans:
        if p.city not in slots_by_city:
            continue
        for slot in slots_by_plan.get(p.key(), []):
            h = slot.hash()
            if h in seen_hashes[p.city]:
                continue
            seen_hashes[p.city].add(h)
            slots_by_city[p.city].append(slot)
    record_availability(conn, slots_by_city)

    now = datetime.utcnow()
    max_multiplier = getattr(cfg, "adaptive_rate_limit_max_multiplier",
                             _MAX_ABUNDANCE_MULTIPLIER)
    # Fairness: serve longest-waiting subscribers first (never-notified, then
    # oldest last_notified_at). When a burst exceeds the daily send quota, the
    # deferred tail is whoever was most recently served — so nobody is
    # permanently starved across cycles. datetime.min sorts NULLs to the front.
    outbox: list = []
    for sub in sorted(subs, key=lambda s: s.last_notified_at or datetime.min):
        # Each subscriber's floor is their own: scarce filters keep the base
        # interval, filters swimming in slots wait longer. Cheap to evaluate
        # here because the abundance was measured at their last delivery
        # rather than recomputed for every skipped subscriber every cycle.
        required_gap = adaptive_rate_limit_minutes(
            rate_limit_minutes, sub.last_match_count,
            max_multiplier=max_multiplier)
        if (sub.last_notified_at
                and sub.last_notified_at > now - timedelta(minutes=required_gap)):
            continue
        # Gather candidate slots from any plan that covers this subscription's filter.
        # Dedupe by hash within the cycle: the same logical slot (day/time/office/
        # service) can surface from two resources (counters) or two overlapping
        # plans — Slot.hash() excludes the resource, so collapse them to one line.
        candidates: list[Slot] = []
        seen_in_cycle: set[str] = set()
        matched_total = 0
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
                seen_in_cycle.add(slot_hash)
                # Counted before the seen filter: the adaptive interval needs
                # how much this filter is matching *in total*, not how much of
                # it is new. A subscriber drip-fed one fresh slot per cycle out
                # of thirty standing ones is the abundant case, not the scarce
                # one, and counting only candidates would read it backwards.
                matched_total += 1
                if has_seen_slot(conn, sub.id, slot_hash):
                    continue
                candidates.append(slot)
        if not candidates:
            continue
        # No per-slot slots_cache writes anymore: Smart-CJM bookings are
        # session-bound (the step machine rejects /booking without walking
        # services→locations→search_results in the same cookie session), so a
        # per-slot deep link cannot work. Digests link to /go/<city>, resolved
        # from the catalog at click time (see web.go_route). The slots_cache
        # table stays: /go/<city>:<token> keeps serving links from old emails
        # until housekeeping prunes the rows.
        #
        # Stage for batched delivery. seen_slots + last_notified are recorded
        # inside flush_digests, but only for digests that were actually sent —
        # quota-deferred ones stay unrecorded so a later cycle re-sends them.
        send_digest(conn=conn, subscription=sub, matched_slots=candidates,
                    cycle_id=cycle_id, cfg=cfg, sink=outbox,
                    match_count=matched_total)
    flush_digests(conn, outbox, cfg)
