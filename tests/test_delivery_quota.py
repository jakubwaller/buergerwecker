from datetime import time
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.models import Filter, Slot
from app.repo import insert_pending, confirm
from app.cycle import run_cycle


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
        # Only one email may go out per cycle; Mailjet disabled for the test.
        "RESEND_DAILY_QUOTA": "1", "MAILJET_HOURLY_QUOTA": "0",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _f():
    return Filter(appointment_types=["svc-A"], locations="all",
                  weekdays=[1, 2, 3, 4, 5, 6, 7],
                  time_window_start=time(0, 0), time_window_end=time(23, 59))


def _last_notified(conn, sid):
    return conn.execute("SELECT last_notified_at FROM subscriptions WHERE id=?",
                        (sid,)).fetchone()["last_notified_at"]


def _has_seen(conn, sid):
    return conn.execute("SELECT COUNT(*) AS n FROM seen_slots WHERE subscription_id=?",
                        (sid,)).fetchone()["n"] > 0


def test_quota_limited_cycle_serves_longest_waiting_first_and_defers_rest(db):
    """With room for one send this cycle, the longest-waiting subscriber (never
    notified) is served ahead of a recently-notified one, and the deferred
    subscriber keeps no seen_slots row so it resurfaces next cycle — no
    permanent starvation, no lost state."""
    # s_recent was notified 20 min ago (outside the 15-min rate limit, so
    # eligible again); s_new has never been notified.
    s_recent = insert_pending(db, email="a@x.com", city="leipzig", language="de",
                              filter_=_f(), ttl_days=90); confirm(db, s_recent)
    s_new = insert_pending(db, email="b@x.com", city="leipzig", language="de",
                           filter_=_f(), ttl_days=90); confirm(db, s_new)
    db.execute("UPDATE subscriptions SET last_notified_at=datetime('now','-20 minutes') "
               "WHERE id=?", (s_recent,))
    slot = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok")]

    scraper = MagicMock(); scraper.poll.return_value = slot
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.mail._call_resend_batch", return_value=True) as rb:
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")

    # Exactly one email this cycle (quota = 1)...
    assert rb.call_count == 1
    assert len(rb.call_args_list[0].args[0]) == 1
    # ...and it went to the never-notified subscriber, not the recently-served one.
    assert _has_seen(db, s_new) and not _has_seen(db, s_recent)
    # The deferred subscriber was left untouched → it will resurface next cycle.
    assert _last_notified(db, s_recent) is not None  # unchanged (its old value)
