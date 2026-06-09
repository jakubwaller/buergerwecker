from datetime import datetime, time, timedelta
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.models import Filter
from app.repo import insert_pending, confirm
from app.housekeeping import run_once

@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE", "10")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("MAILJET_API_KEY", "m"); monkeypatch.setenv("MAILJET_API_SECRET", "m")
    monkeypatch.setenv("MAILJET_FROM_EMAIL","x@x"); monkeypatch.setenv("MAILJET_FROM_NAME","x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA","6000"); monkeypatch.setenv("RESEND_API_KEY","r")
    monkeypatch.setenv("ADMIN_TOKEN","a"*32)
    monkeypatch.setenv("DEDUP_WINDOW_HOURS","24"); monkeypatch.setenv("RATE_LIMIT_MINUTES","15")
    monkeypatch.setenv("MAX_PLANS_PER_CITY","10"); monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS","2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR","99")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY","99")
    monkeypatch.setenv("DEVELOPER_EMAIL","dev@x"); monkeypatch.setenv("KOFI_URL","https://k")
    conn = connect(db_path); init_schema(conn)
    return conn

def _f():
    return Filter(appointment_types=["A"], locations="all", weekdays=[1,2,3,4,5,6,7],
                  time_window_start=time(0,0), time_window_end=time(23,59))


def test_ops_summary_email_uses_dashboard_layout(db):
    from app.config import load_config
    from app.housekeeping import _send_summary_email
    with patch("app.mail.send") as send:
        _send_summary_email(db, load_config())
    assert send.call_count == 1
    body = send.call_args.args[3]   # send(conn, to, subject, body, *, idem_key=...)
    assert "OVERVIEW" in body and "CITIES" in body and "SYSTEM" in body
    assert "active_subscriptions:" not in body   # not the old raw key:value dump

def test_expired_subscriptions_soft_deleted(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig", language="de",
                         filter_=_f(), ttl_days=90)
    confirm(db, sid)
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') WHERE id=?", (sid,))
    with patch("app.mail.send"):
        run_once(db)
    row = db.execute("SELECT deleted_at FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["deleted_at"] is not None

def test_old_deleted_rows_hard_purged(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig", language="de",
                         filter_=_f(), ttl_days=90)
    db.execute("UPDATE subscriptions SET deleted_at=datetime('now','-31 days') WHERE id=?",
               (sid,))
    with patch("app.mail.send"):
        run_once(db)
    row = db.execute("SELECT id FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row is None

def test_renewal_reminder_sent_in_window(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig", language="de",
                         filter_=_f(), ttl_days=90)
    confirm(db, sid)
    # Move expires_at to 5 days from now (within 10-day reminder window)
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','+5 days') WHERE id=?", (sid,))
    with patch("app.mail.send") as send:
        run_once(db)
    # send() should be called at least once (for renewal reminder)
    assert send.called
