from __future__ import annotations
import heapq
import sqlite3
from datetime import datetime, timedelta
import requests
from app.filters import matches
from app.planning import build_plans
from app.repo import (active_subscriptions, digests_in_window, has_seen_slot,
                      record_cap_hold, reset_digest_streak)
from app.scrapers import get_scraper, UnsupportedCity
from app.http_session import CountingSession
from app.models import SeenKey, Slot, per_slot_key
from app.analytics import record_availability
from app.snapshots import record_snapshots

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

# The horizon: per subscription and cycle, only the soonest
# MAX_SLOTS_PER_CYCLE matching slots are looked at at all. They are what is
# checked against seen_slots, what the digest is made of, and (through the
# carried seen_keys) what its delivery records; nothing past the horizon is
# checked, mentioned or recorded. Without it a cycle cost one seen_slots
# query per subscriber per matching slot, and a delivery one row each: 2000
# subscriptions on a 1000-slot calendar took 12.6 s a cycle and left 2M rows.
#
# Why a horizon and not a cap on what a digest *records*: recording the
# soonest 50 of 1000 notified slots leaves 950 the subscriber was told about
# unrecorded, so they come back as candidates at every eligible cycle, a
# digest per interval about the same inventory. That is the check/record
# mismatch CLAUDE.md warns about. Here check and record cover one set.
#
# What a horizon does instead: a slot past it is treated exactly like a slot
# past the filter's max_days_ahead window (models.Filter): not marked seen,
# and news the cycle it moves within reach (a booking ahead of it, a day
# going by), which is the first time the subscriber hears of it. The horizon
# is over *matching* slots, seen or not: one over unseen slots would page
# through the calendar fifty at a time. Bookings move it up one slot at a
# time, at the subscriber's own cadence and daily cap. The abundance count
# stops here too, well past the ladder's top rung, so the cadence reads the
# same.
MAX_SLOTS_PER_CYCLE = 50


def _soonest(slot: Slot) -> tuple[str, str]:
    return slot.date, slot.time_str


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
        return per_slot_key


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


def subscriber_cap(conn: sqlite3.Connection, cfg) -> tuple[int, bool]:
    """(the daily digest cap for every subscriber, tightened?) for this
    cycle. One number for both channels: the same daily cap for mail and
    push is a fairness rule, not a coincidence, and it stays one rule under
    pressure. It is MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY, dropping to
    MAIL_CAP_UNDER_PRESSURE while the free provider pool's rolling-24h usage
    is at MAIL_POOL_PRESSURE_PCT or above, so the pool thins everyone's day a
    little before it defers anyone's entirely. Push has no pool of its own
    and still gets no more than mail: a tighter day for mail alone made the
    app the way to more notifications. 0 for the ordinary cap means no cap
    at all, pressure or not: an operator who turned the cap off must not
    find a hidden one; 0 for the pressure cap means never tighten."""
    from app.mail import pool_usage
    cap = getattr(cfg, "max_digests_per_subscriber_per_day", 0) or 0
    tight = getattr(cfg, "mail_cap_under_pressure", 0) or 0
    pct = getattr(cfg, "mail_pool_pressure_pct", 0) or 0
    if not cap or not tight or not pct or tight >= cap:
        return cap, False
    used, pool = pool_usage(conn, cfg)
    if pool and used * 100 >= pool * pct:
        return tight, True
    return cap, False


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
    plans_ok: list = []
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
            plans_ok.append(p)
            if slots_by_plan[p.key()]:
                cities_with_any_slot.add(p.city)
        except UnsupportedCity as exc:
            # Not an upstream hiccup but a tenant nobody can poll — its
            # subscribers would otherwise wait in silence until the parser
            # canary fires hours later.
            print(f"cycle {cycle_id}: no scraper for {exc}", flush=True)
            slots_by_plan[p.key()] = []
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
    from app.db import sql_ts, transaction
    now_ts = sql_ts(datetime.utcnow())
    today = now_ts[:10]  # UTC date the *_today counters belong to
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
                    (now_ts, city),
                )
            else:
                conn.execute(
                    "UPDATE city_state "
                    "SET zero_match_since=COALESCE(zero_match_since, ?), "
                    "    last_polled_at=? "
                    "WHERE city=?",
                    (now_ts, now_ts, city),
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
    # The app's overview reads the last poll from here rather than polling
    # the city itself. One row per service, written only when every plan of
    # the service succeeded this cycle: a failed poll keeps the previous
    # snapshot, with its older polled_at.
    record_snapshots(conn, plans, cities_polled, plans_ok, slots_by_plan)

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
    # Every plan's slots soonest first, once per cycle, so each subscriber
    # can stop at its horizon (MAX_SLOTS_PER_CYCLE) instead of walking a
    # whole calendar.
    soonest_by_plan = {k: sorted(v, key=_soonest) for k, v in slots_by_plan.items()}
    cap, tightened = subscriber_cap(conn, cfg)
    if tightened:
        print(f"cycle {cycle_id}: mail pool under pressure, every subscriber "
              f"(mail and app) capped at {cap}/day this cycle", flush=True)
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
        candidate_keys: list[SeenKey] = []
        seen_in_cycle: set[str] = set()
        matched_total = 0
        if sub.city not in seen_key_fns:
            seen_key_fns[sub.city] = _seen_key_fn(sub.city)
        seen_key = seen_key_fns[sub.city]
        sources = [soonest_by_plan.get(plan.key(), []) for plan in plans
                   if plan.city == sub.city
                   and plan.appointment_type in sub.sub_filter.appointment_types]
        for slot in heapq.merge(*sources, key=_soonest):
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
            # not news — unless it is *earlier* than the time already
            # reported, which only a cancellation can produce. See
            # Catalog.seen_key and models.SeenKey.
            key = seen_key(slot)
            if not has_seen_slot(conn, sub.id, key.key, at=key.best_time):
                candidates.append(slot)
                candidate_keys.append(key)
            if matched_total >= MAX_SLOTS_PER_CYCLE:
                break       # the horizon, see MAX_SLOTS_PER_CYCLE
        if not candidates:
            continue
        # The per-subscriber daily cap, checked only once there is something
        # to send so a hold always means a real digest was held. Nothing is
        # recorded as seen and last_notified_at is not stamped: the first
        # cycle after the rolling window frees re-evaluates the live slots
        # and sends whatever is still open — never a queued, stale digest.
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
