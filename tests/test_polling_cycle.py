from datetime import datetime, time
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.models import Filter, Slot
from app.repo import insert_pending, confirm
from app.cycle import run_cycle

@pytest.fixture
def db(tmp_path, monkeypatch):
    # Set the env vars that `cfg=None → load_config()` requires inside run_cycle.
    for k, v in {
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
        "TOKEN_SECRET_PRIMARY":"x"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"a"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "DEVELOPER_EMAIL":"dev@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn

def _f(types, locs="all"):
    return Filter(
        appointment_types=list(types),
        locations="all" if locs == "all" else list(locs),
        weekdays=[1,2,3,4,5,6,7],
        time_window_start=time(0,0), time_window_end=time(23,59),
    )

def test_cycle_sends_one_digest_per_subscriber_on_match(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    fake_slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok")]
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        scraper = MagicMock()
        scraper.poll.return_value = fake_slots
        gs.return_value = scraper
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id="c1")
        send_d.assert_called_once()
        args = send_d.call_args
        assert args.kwargs["subscription"].id == sid
        assert args.kwargs["matched_slots"] == fake_slots

def test_cycle_dedups_same_slot_offered_by_multiple_resources(db):
    """Two counters (resources) offering the same service slot at the same office
    and minute are ONE notification — they share a hash (resource excluded), so the
    digest must contain a single line, not a duplicate."""
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    dup_slots = [
        Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok", "resource-1"),
        Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok", "resource-2"),
    ]
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        scraper = MagicMock()
        scraper.poll.return_value = dup_slots
        gs.return_value = scraper
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
        send_d.assert_called_once()
        assert len(send_d.call_args.kwargs["matched_slots"]) == 1

def test_cycle_aggregates_slots_for_multi_type_subscriber(db):
    """A subscriber with two appointment types fans into two plans; a digest
    aggregates the matching slots from both."""
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A", "svc-B"]), ttl_days=90)
    confirm(db, sid)
    def poll_by_type(plan, http):
        if plan.appointment_type == "svc-A":
            return [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tA", "r1")]
        return [Slot("2026-06-11", "09:00", "loc-2", "svc-B", "tB", "r2")]
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        scraper = MagicMock()
        scraper.poll.side_effect = poll_by_type
        gs.return_value = scraper
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
        send_d.assert_called_once()
        services = {s.service_uuid for s in send_d.call_args.kwargs["matched_slots"]}
        assert services == {"svc-A", "svc-B"}

def test_cycle_skips_already_seen_slot(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    fake_slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok")]
    from app.repo import record_seen_slot
    record_seen_slot(db, sid, fake_slots[0].hash())
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        gs.return_value.poll.return_value = fake_slots
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id="c1")
    send_d.assert_not_called()

def test_cycle_records_poll_and_request_counts(db):
    """run_cycle must attribute one poll and the HTTP-request delta per poll to
    the polled city's counters."""
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    fake_slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok")]
    def fake_poll(plan, http):
        http.request_count += 3   # simulate 3 upstream HTTP calls
        return fake_slots
    with patch("app.cycle.get_scraper") as gs, patch("app.cycle.send_digest"):
        scraper = MagicMock()
        scraper.poll.side_effect = fake_poll
        gs.return_value = scraper
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    row = db.execute(
        "SELECT polls_today, polls_total, requests_today, requests_total "
        "FROM city_state WHERE city='leipzig'").fetchone()
    assert row["polls_today"] == 1 and row["polls_total"] == 1
    assert row["requests_today"] == 3 and row["requests_total"] == 3

def test_cycle_resets_today_counters_on_date_rollover(db):
    """A stale `counts_date` resets the *_today counters on the next cycle, but
    the all-time totals keep accumulating."""
    db.execute(
        "INSERT INTO city_state (city, polls_today, polls_total, "
        "requests_today, requests_total, counts_date) "
        "VALUES ('leipzig', 99, 99, 99, 99, '2000-01-01')")
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    def fake_poll(plan, http):
        http.request_count += 2
        return [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok")]
    with patch("app.cycle.get_scraper") as gs, patch("app.cycle.send_digest"):
        scraper = MagicMock(); scraper.poll.side_effect = fake_poll
        gs.return_value = scraper
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    row = db.execute("SELECT * FROM city_state WHERE city='leipzig'").fetchone()
    assert row["polls_today"] == 1      # reset from 99, then +1
    assert row["requests_today"] == 2   # reset from 99, then +2
    assert row["polls_total"] == 100    # 99 + 1, not reset
    assert row["requests_total"] == 101  # 99 + 2, not reset

def test_cycle_respects_rate_limit(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                        language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    db.execute("UPDATE subscriptions SET last_notified_at=CURRENT_TIMESTAMP "
               "WHERE id=?", (sid,))
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        gs.return_value.poll.return_value = [
            Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok"),
        ]
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id="c1")
    send_d.assert_not_called()

def test_cycle_window_filter_defers_far_slots_until_they_enter_window(db):
    """A subscriber with max_days_ahead=7 must only be notified about slots
    inside the window — and a farther slot must NOT be marked seen, so a later
    cycle (once the slot is within 7 days) still notifies about it."""
    from datetime import date, timedelta
    from freezegun import freeze_time
    from app.repo import has_seen_slot
    f = Filter(appointment_types=["svc-A"], locations="all",
               weekdays=[1,2,3,4,5,6,7],
               time_window_start=time(0,0), time_window_end=time(23,59),
               max_days_ahead=7)
    sid = insert_pending(db, email="w@x.com", city="leipzig",
                         language="de", filter_=f, ttl_days=90)
    confirm(db, sid)
    near = Slot("2026-06-03", "10:00", "loc-1", "svc-A", "t-near")
    far = Slot("2026-06-20", "10:00", "loc-1", "svc-A", "t-far")
    with freeze_time("2026-06-01"), \
         patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        gs.return_value.poll.return_value = [near, far]
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    send_d.assert_called_once()
    sent = send_d.call_args.kwargs["matched_slots"]
    assert [s.booking_token for s in sent] == ["t-near"]
    # The far slot was never presented, so it must not be marked seen —
    # otherwise it could never be notified once it enters the window.
    assert has_seen_slot(db, sid, far.hash()) is False

    # Mocking send_digest bypasses flush_digests, where delivered slots get
    # recorded — record the near slot's delivery manually, as the flush would.
    from app.repo import record_seen_slot
    record_seen_slot(db, sid, near.hash())

    # Two weeks later the same slot is inside the window: it fires now.
    db.execute("UPDATE subscriptions SET last_notified_at=NULL WHERE id=?", (sid,))
    with freeze_time("2026-06-15"), \
         patch("app.cycle.get_scraper") as gs2, \
         patch("app.cycle.send_digest") as send_d2:
        gs2.return_value.poll.return_value = [near, far]
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c2")
    send_d2.assert_called_once()
    assert [s.booking_token for s in send_d2.call_args.kwargs["matched_slots"]] == ["t-far"]

def test_poll_interval_skips_city_until_due(db):
    """A tenant with poll_interval_seconds=180 is skipped while its interval
    hasn't elapsed (no poll, no counter/last_polled_at update) and polled once
    it has. Default-cadence tenants are unaffected (covered by every other
    test in this file)."""
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                         language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    # last polled 60s ago; interval 180 → not due
    db.execute("INSERT INTO city_state (city, last_polled_at, polls_total) "
               "VALUES ('leipzig', datetime('now','-60 seconds'), 7)")
    with patch("app.cycle._poll_interval_s", return_value=180), \
         patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest") as send_d:
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    gs.return_value.poll.assert_not_called()
    send_d.assert_not_called()
    row = db.execute("SELECT polls_total FROM city_state WHERE city='leipzig'").fetchone()
    assert row["polls_total"] == 7          # untouched — not even a zero-delta write

    # 200s ago → due
    db.execute("UPDATE city_state SET last_polled_at=datetime('now','-200 seconds') "
               "WHERE city='leipzig'")
    with patch("app.cycle._poll_interval_s", return_value=180), \
         patch("app.cycle.get_scraper") as gs2, \
         patch("app.cycle.send_digest"):
        gs2.return_value.poll.return_value = []
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c2")
    assert gs2.return_value.poll.called


def test_poll_interval_fails_open_without_last_polled(db):
    """No city_state row yet (first ever cycle) → the tenant is due."""
    sid = insert_pending(db, email="b@x.com", city="leipzig",
                         language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    with patch("app.cycle._poll_interval_s", return_value=600), \
         patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest"):
        gs.return_value.poll.return_value = []
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    assert gs.return_value.poll.called


# ---------------------------------------------------------------------------
# The horizon (cycle.MAX_SLOTS_PER_CYCLE): per subscription and cycle only the
# soonest matching slots are checked, sent and recorded, and exactly the same
# ones. Security review 2026-10-07: 2000 subscriptions on a 1000-slot calendar
# took 12.6 s a cycle and left 2M seen_slots rows.

def _calendar(n, locs=("loc-1",)):
    """n distinct slots, soonest first: twenty a day from 08:00, every half
    hour, cycling through `locs`."""
    from datetime import date, timedelta
    base = date(2026, 11, 2)
    return [Slot((base + timedelta(days=i // 20)).isoformat(),
                 f"{8 + (i % 20) // 2:02d}:{(i % 2) * 30:02d}",
                 locs[i % len(locs)], "svc-A", f"t{i}")
            for i in range(n)]


def _cycle_spying(db, slots, cycle_id):
    """A real cycle and a real flush (the provider call mocked); returns the
    slots each digest was made of."""
    from app import digest
    made = []

    def spy(**kw):
        made.append(list(kw["matched_slots"]))
        return digest.send_digest(**kw)

    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.send_digest", spy), \
         patch("app.mail._call_mailjet_batch", return_value=200):
        gs.return_value.poll.return_value = slots
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id=cycle_id)
    return made


def _seen(db, sid):
    return {r[0] for r in db.execute(
        "SELECT slot_hash FROM seen_slots WHERE subscription_id=?", (sid,))}


def test_a_digest_is_the_soonest_slots_up_to_the_horizon_and_records_exactly_them(db):
    import random
    from app.cycle import MAX_SLOTS_PER_CYCLE as K
    sid = insert_pending(db, email="h@example.com", city="leipzig",
                         language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    cal = _calendar(3 * K)
    shuffled = random.Random(7).sample(cal, len(cal))   # the scraper's order is no order
    made = _cycle_spying(db, shuffled, "c1")
    assert [len(m) for m in made] == [K]
    # Check and record cover one set: what the digest was made of is what
    # its delivery recorded, and that is the soonest K.
    assert {s.hash() for s in made[0]} == _seen(db, sid) == {s.hash() for s in cal[:K]}
    # Due again on the same calendar: everything within the horizon is seen,
    # so nothing goes out. The horizon is over matching slots, seen or not;
    # it does not page on to the next K.
    db.execute("UPDATE subscriptions SET last_notified_at=datetime('now','-1 day') "
               "WHERE id=?", (sid,))
    assert _cycle_spying(db, shuffled, "c2") == []
    # The soonest slot is booked: the next one moves within reach, and it is
    # news (the subscriber never heard of it), alone.
    made = _cycle_spying(db, cal[1:], "c3")
    assert [[s.hash() for s in m] for m in made] == [[cal[K].hash()]]
    assert _seen(db, sid) == {s.hash() for s in cal[:K + 1]}


def test_the_horizon_merges_overlapping_plans_soonest_first(db):
    """An all-offices subscriber reads every plan of its service; with a
    one-office plan next to the all-offices one the same slot arrives twice,
    and the horizon counts it once."""
    from app.cycle import MAX_SLOTS_PER_CYCLE as K
    wide = insert_pending(db, email="w@example.com", city="leipzig", language="de",
                          filter_=_f(["svc-A"]), ttl_days=90)
    narrow = insert_pending(db, email="n@example.com", city="leipzig", language="de",
                            filter_=_f(["svc-A"], ["loc-2"]), ttl_days=90)
    confirm(db, wide)
    confirm(db, narrow)
    cal = _calendar(4 * K, locs=("loc-1", "loc-2"))
    _cycle_spying(db, cal, "c1")
    assert _seen(db, wide) == {s.hash() for s in cal[:K]}
    # The one-office subscriber: the soonest K slots *it* matches.
    its_own = [s for s in cal if s.location_uuid == "loc-2"]
    assert _seen(db, narrow) == {s.hash() for s in its_own[:K]}


def test_the_horizon_bounds_the_seen_slots_lookups(db):
    from app import cycle
    from app.cycle import MAX_SLOTS_PER_CYCLE as K
    for i in range(3):
        sid = insert_pending(db, email=f"s{i}@example.com", city="leipzig",
                             language="de", filter_=_f(["svc-A"]), ttl_days=90)
        confirm(db, sid)
    calls = []
    real = cycle.has_seen_slot

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    with patch("app.cycle.get_scraper") as gs, \
         patch("app.cycle.has_seen_slot", counting), \
         patch("app.cycle.send_digest"):
        gs.return_value.poll.return_value = _calendar(1000)
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    assert len(calls) == 3 * K          # not 3 * 1000


def test_the_horizon_lies_past_the_abundance_ladder():
    """The abundance count stops at the horizon too; past the ladder's top
    rung every count reads the same, so the cadence does not change."""
    from app.cycle import MAX_SLOTS_PER_CYCLE, _ABUNDANCE_LADDER, adaptive_rate_limit_minutes
    assert MAX_SLOTS_PER_CYCLE > _ABUNDANCE_LADDER[-1][0]
    assert (adaptive_rate_limit_minutes(15, MAX_SLOTS_PER_CYCLE)
            == adaptive_rate_limit_minutes(15, 2792))
