from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
import requests
from app.filters import matches
from app.planning import build_plans
from app.repo import (active_subscriptions, digests_in_window, has_seen_slot,
                      record_cap_hold, reset_digest_streak)
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

# Abundance measures stock, and on an earliest-slot-per-office tenant a
# subscriber watching ONE office can never match more than one slot — so
# somebody being drip-fed a fresh single slot every cycle reads as maximally
# scarce and keeps the fastest cadence. Two of those (Augsburg ×10, Darmstadt
# ×16 on 2026-07-27) were invisible to the ladder above for exactly this
# reason. This second signal measures flow instead: how many digests a
# subscriber has had in an unbroken run.
#
# It is safe for genuinely scarce subscribers because the run ends as soon as
# the stream goes quiet — and for real scarcity it goes quiet constantly.
_STREAK_LADDER = ((1, 1), (2, 2), (3, 4))

# "Quiet" has to mean the stream dried up, not that one cycle happened to find
# nothing. An earlier version reset the run on any empty cycle, and measuring
# it against 2026-07-27's real traffic showed it almost never engaged: someone
# getting mail every ~20 minutes is idle in between, so the run was wiped
# before it could build. A run is over when the silence since the last digest
# has run to twice the cadence that digest earned — a live stream always comes
# back well inside that.
_QUIET_FACTOR = 2


def _ladder_multiplier(ladder, value: int) -> int:
    for threshold, multiplier in ladder:
        if value <= threshold:
            return multiplier
    return _MAX_ABUNDANCE_MULTIPLIER


def adaptive_rate_limit_minutes(base_minutes: int, match_count: int | None, *,
                                streak: int = 0,
                                max_multiplier: int = _MAX_ABUNDANCE_MULTIPLIER) -> int:
    """Minimum minutes between digests for a subscriber whose filter matched
    `match_count` slots at its last delivered digest, and who has had `streak`
    digests in an unbroken run.

    The two signals compose by taking the larger multiplier: either "you have
    plenty of options" or "you are hearing from us constantly" is reason enough
    to slow down, and they catch different subscribers.

    `match_count is None` — never notified, or a row predating the column —
    contributes nothing, so a new subscriber is served fast until measured.
    `max_multiplier=1` pins everyone to the base, i.e. the pre-adaptive
    behaviour, which is what makes it a usable kill switch.
    """
    abundance = (1 if match_count is None
                 else _ladder_multiplier(_ABUNDANCE_LADDER, match_count))
    flow = _ladder_multiplier(_STREAK_LADDER, max(0, streak))
    multiplier = max(abundance, flow)
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


# Cities already warned about a catalog that would not load, so a persistent
# failure says so once per process rather than once a minute forever.
_KEY_FALLBACK_WARNED: set[str] = set()


def _seen_key_fn(city: str):
    """Return this tenant's slot → seen_slots key function.

    Falls back to per-slot identity when the catalog cannot be read: a missing
    or malformed file must never coarsen a tenant's notifications, because the
    coarse direction is the one that can *withhold* mail. The fallback is loud
    — silently reverting a `day` tenant to per-slot keys shows up only as mail
    volume creeping back, which nothing alerts on.
    """
    try:
        from app.catalog import load_catalog
        return load_catalog(city).seen_key
    except Exception as exc:
        if city not in _KEY_FALLBACK_WARNED:
            _KEY_FALLBACK_WARNED.add(city)
            print(f"notify_granularity: catalog unreadable for {city}, "
                  f"falling back to per-slot keys: {exc}", flush=True)
        return Slot.hash


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
    polled_ok: dict[str, set[str]] = {}
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
            # Only a poll that didn't raise proves the service was looked at —
            # the availability series must not read a failed scrape as "empty".
            polled_ok.setdefault(p.city, set()).add(p.appointment_type)
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
    record_availability(conn, slots_by_city, polled_ok)

    now = datetime.utcnow()
    max_multiplier = getattr(cfg, "adaptive_rate_limit_max_multiplier",
                             _MAX_ABUNDANCE_MULTIPLIER)
    # Fairness: serve longest-waiting subscribers first (never-notified, then
    # oldest last_notified_at). When a burst exceeds the daily send quota, the
    # deferred tail is whoever was most recently served — so nobody is
    # permanently starved across cycles. datetime.min sorts NULLs to the front.
    outbox: list = []
    # Per-cycle memo so a tenant's catalog is resolved once, not per subscriber.
    seen_key_fns: dict = {}
    for sub in sorted(subs, key=lambda s: s.last_notified_at or datetime.min):
        # Each subscriber's floor is their own: scarce filters keep the base
        # interval, filters swimming in slots wait longer. Cheap to evaluate
        # here because the abundance was measured at their last delivery
        # rather than recomputed for every skipped subscriber every cycle.
        streak = sub.consecutive_digests
        required_gap = adaptive_rate_limit_minutes(
            rate_limit_minutes, sub.last_match_count, streak=streak,
            max_multiplier=max_multiplier)
        # Has the run gone quiet for long enough to be over? Checked here
        # rather than on empty cycles, so an unpolled or briefly idle tenant
        # can't be mistaken for a stream that ended.
        if (streak and sub.last_notified_at and required_gap
                and sub.last_notified_at <= now - timedelta(
                    minutes=required_gap * _QUIET_FACTOR)):
            reset_digest_streak(conn, sub.id)
            streak = 0
            required_gap = adaptive_rate_limit_minutes(
                rate_limit_minutes, sub.last_match_count, streak=0,
                max_multiplier=max_multiplier)
        if (sub.last_notified_at
                and sub.last_notified_at > now - timedelta(minutes=required_gap)):
            continue
        # Gather candidate slots from any plan that covers this subscription's filter.
        # Dedupe by hash within the cycle: the same logical slot (day/time/office/
        # service) can surface from two resources (counters) or two overlapping
        # plans — Slot.hash() excludes the resource, so collapse them to one line.
        candidates: list[Slot] = []
        candidate_keys: list[str] = []
        seen_in_cycle: set[str] = set()
        matched_total = 0
        if sub.city not in seen_key_fns:
            seen_key_fns[sub.city] = _seen_key_fn(sub.city)
        seen_key = seen_key_fns[sub.city]
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
                # What counts as already-told is the tenant's call, not the
                # slot's: an earliest-slot-only tenant keys on the day, so the
                # replacement slot that appears the moment someone books is
                # not news. See Catalog.seen_key.
                key = seen_key(slot)
                if has_seen_slot(conn, sub.id, key):
                    continue
                candidates.append(slot)
                candidate_keys.append(key)
        if not candidates:
            continue
        # The per-subscriber daily cap, checked only once there is something
        # to send so a hold always means a real digest was held. Nothing is
        # recorded as seen and last_notified_at is not stamped: the first
        # cycle after the rolling window frees re-evaluates the live slots
        # and sends whatever is still open — never a queued, stale digest.
        cap = getattr(cfg, "max_digests_per_subscriber_per_day", 0)
        if cap and digests_in_window(conn, sub.id) >= cap:
            record_cap_hold(conn, sub.id)
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
                    match_count=matched_total,
                    seen_keys=candidate_keys)
    flush_digests(conn, outbox, cfg)
