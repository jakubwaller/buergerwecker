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
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
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
