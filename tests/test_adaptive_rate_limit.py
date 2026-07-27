"""Adaptive send cadence: RATE_LIMIT_MINUTES is a floor for scarce filters,
stretched for subscribers whose filter is already matching plenty of slots."""
from datetime import time
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.models import Filter, Slot
from app.repo import insert_pending, confirm
from app.cycle import run_cycle, adaptive_rate_limit_minutes


@pytest.fixture
def db(tmp_path, monkeypatch):
    for k, v in {
        "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m", "MAILJET_FROM_EMAIL": "x@x",
        "MAILJET_FROM_NAME": "x", "MAILJET_DAILY_QUOTA": "6000", "RESEND_API_KEY": "r",
        "TOKEN_SECRET_PRIMARY": "x" * 32, "TOKEN_SECRET_PREVIOUS": "",
        "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
        "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
        "SUBSCRIPTION_TTL_DAYS": "90", "RENEWAL_REMINDER_DAYS_BEFORE": "10",
        "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
        "DEVELOPER_EMAIL": "dev@x", "KOFI_URL": "https://k",
        "RESEND_DAILY_QUOTA": "100", "MAILJET_HOURLY_QUOTA": "100",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _f():
    return Filter(appointment_types=["svc-A"], locations="all",
                  weekdays=[1, 2, 3, 4, 5, 6, 7],
                  time_window_start=time(0, 0), time_window_end=time(23, 59))


def _slots(n, offset=0):
    """n distinct slots — distinct (date, time, office, service) so each hashes
    differently."""
    return [Slot(f"2026-09-{(i % 28) + 1:02d}", f"{8 + (i % 12):02d}:{i % 60:02d}",
                 "loc-1", "svc-A", f"tok-{i}")
            for i in range(offset, offset + n)]


def _sub(db, email="a@x.com"):
    sid = insert_pending(db, email=email, city="leipzig", language="de",
                         filter_=_f(), ttl_days=90)
    confirm(db, sid)
    return sid


def _cycle(db, slots, cycle_id):
    scraper = MagicMock()
    scraper.poll.return_value = slots
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_resend_batch", return_value=200):
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id=cycle_id)
    return mb


def _state(db, sid):
    row = db.execute("SELECT last_match_count, last_notified_at "
                     "FROM subscriptions WHERE id=?", (sid,)).fetchone()
    return row["last_match_count"], row["last_notified_at"]


def _age(db, sid, minutes):
    db.execute(f"UPDATE subscriptions SET last_notified_at="
               f"datetime('now','-{minutes} minutes') WHERE id=?", (sid,))


# --- the ladder itself -----------------------------------------------------

@pytest.mark.parametrize("count,expected", [
    (None, 15),   # never measured → base, so new subscribers are served fast
    (0, 15),      # matched nothing
    (2, 15),      # scarce: every slot matters
    (3, 30),
    (5, 30),
    (6, 60),      # the Braunschweig all-locations case (40 mails on 2026-07-27)
    (15, 60),
    (16, 120),    # swimming in slots → 2h
    (2792, 120),  # the Bonn all-locations case: capped, never unbounded
])
def test_ladder(count, expected):
    assert adaptive_rate_limit_minutes(15, count) == expected


def test_max_multiplier_one_is_the_kill_switch():
    for count in (None, 0, 3, 30, 5000):
        assert adaptive_rate_limit_minutes(15, count, max_multiplier=1) == 15


def test_max_multiplier_clamps_the_ladder():
    assert adaptive_rate_limit_minutes(15, 5000, max_multiplier=2) == 30
    # A nonsensical value can't produce a zero or negative interval, which
    # would disable rate limiting altogether.
    assert adaptive_rate_limit_minutes(15, 5000, max_multiplier=0) == 15
    assert adaptive_rate_limit_minutes(15, 5000, max_multiplier=-4) == 15


def test_ladder_scales_with_the_configured_base():
    assert adaptive_rate_limit_minutes(60, 30) == 480
    assert adaptive_rate_limit_minutes(0, 30) == 0   # 0 = no rate limiting


# --- measurement -----------------------------------------------------------

def test_delivery_records_the_match_count(db):
    sid = _sub(db)
    _cycle(db, _slots(30), "c1")
    assert _state(db, sid)[0] == 30


def test_match_count_includes_already_seen_slots(db):
    """The drip-feed case: a subscriber sitting on 30 standing slots who gets
    one fresh one per cycle is abundant, not scarce. Counting only the new
    candidate would read it backwards and keep them on the fast floor."""
    sid = _sub(db)
    _cycle(db, _slots(30), "c1")
    _age(db, sid, 200)                       # eligible again
    mb = _cycle(db, _slots(31), "c2")        # 30 old + 1 new
    mb.assert_called_once()
    assert len(mb.call_args_list[0].args[0]) == 1   # one email, one new slot
    assert _state(db, sid)[0] == 31                 # ...but measured as 31


def test_deferred_digest_does_not_record_a_count(db):
    """A digest that never went out must not move the subscriber's cadence."""
    sid = _sub(db)
    scraper = MagicMock()
    scraper.poll.return_value = _slots(30)
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.mail._call_mailjet_batch", return_value=500), \
         patch("app.mail._call_resend_batch", return_value=500):
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    assert _state(db, sid) == (None, None)


# --- the gate --------------------------------------------------------------

def test_abundant_subscriber_waits_out_the_stretched_interval(db):
    sid = _sub(db)
    _cycle(db, _slots(30), "c1")             # 30 matches → 8x → 120 min
    assert _state(db, sid)[0] == 30

    _age(db, sid, 30)                        # 30 min later: still inside 120
    assert _cycle(db, _slots(40), "c2").call_count == 0

    _age(db, sid, 121)                       # past it → served
    mb = _cycle(db, _slots(40), "c3")
    mb.assert_called_once()


def test_scarce_subscriber_keeps_the_base_interval(db):
    """The whole point: a filter matching almost nothing is not slowed down."""
    sid = _sub(db)
    _cycle(db, _slots(1), "c1")              # 1 match → base 15 min
    assert _state(db, sid)[0] == 1

    _age(db, sid, 16)
    mb = _cycle(db, _slots(2), "c2")         # one new slot, 16 min later
    mb.assert_called_once()


def test_kill_switch_restores_the_flat_floor(db, monkeypatch):
    monkeypatch.setenv("ADAPTIVE_RATE_LIMIT_MAX_MULTIPLIER", "1")
    sid = _sub(db)
    _cycle(db, _slots(30), "c1")
    _age(db, sid, 16)                        # would be throttled at 8x
    mb = _cycle(db, _slots(40), "c2")
    mb.assert_called_once()


def test_never_notified_subscriber_is_served_immediately(db):
    """Abundance must never delay a first digest — last_match_count is NULL
    until we have measured, and NULL means base."""
    sid = _sub(db)
    mb = _cycle(db, _slots(500), "c1")
    mb.assert_called_once()
    assert _state(db, sid)[1] is not None
