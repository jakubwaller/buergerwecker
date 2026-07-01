from types import SimpleNamespace
from datetime import time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.models import Filter
from app.repo import insert_pending, confirm, pending_confirmations
from app.confirmations import send_confirmation_now, send_pending_confirmations


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re")
    for k, v in {"MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "s",
                 "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x"}.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _cfg(**over):
    base = dict(resend_daily_quota=100, mailjet_hourly_quota=10,
                mailjet_daily_quota=200, quota_alert_threshold_pct=80,
                developer_email="dev@x", email_provider_order=("mailjet", "resend"),
                token_secret_primary="x" * 32, token_secret_previous="",
                public_base_url="https://x")
    base.update(over)
    return SimpleNamespace(**base)


def _sub(conn, email="a@x.com"):
    return insert_pending(conn, email=email, city="leipzig", language="de",
                          filter_=Filter(["svc-A"], "all", [1],
                                         time(0, 0), time(23, 59)), ttl_days=90)


def _sent_at(conn, sid):
    return conn.execute("SELECT confirmation_sent_at FROM subscriptions WHERE id=?",
                        (sid,)).fetchone()[0]


def _pending_ids(conn):
    return [p[0] for p in pending_confirmations(conn)]


def test_deferred_confirmation_is_kept_then_delivered_by_retry(db):
    sid = _sub(db)
    # All quota exhausted → the immediate send defers.
    with patch("app.mail._call_mailjet_batch", return_value=True), \
         patch("app.mail._call_resend_batch", return_value=True):
        delivered = send_confirmation_now(db, sid, "a@x.com", "de",
                                          _cfg(mailjet_hourly_quota=0,
                                               mailjet_daily_quota=0,
                                               resend_daily_quota=0))
    assert delivered is False
    assert _sent_at(db, sid) is None          # not marked sent
    assert sid in _pending_ids(db)            # still awaiting confirmation

    # Later cycle, quota available → retry pass delivers it.
    with patch("app.mail._call_mailjet_batch", return_value=True) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_called_once()
    assert _sent_at(db, sid) is not None
    assert sid not in _pending_ids(db)


def test_retry_is_idempotent_once_delivered(db):
    sid = _sub(db)
    with patch("app.mail._call_mailjet_batch", return_value=True):
        assert send_confirmation_now(db, sid, "a@x.com", "de", _cfg()) is True
    # A second retry pass must not re-send an already-confirmed-sent sign-up.
    with patch("app.mail._call_mailjet_batch", return_value=True) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()


def test_retry_skips_already_confirmed_users(db):
    sid = _sub(db, "b@x.com")
    confirm(db, sid)                          # user clicked the link already
    with patch("app.mail._call_mailjet_batch", return_value=True) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()


def test_retry_abandons_stale_signups(db):
    sid = _sub(db, "old@x.com")
    db.execute("UPDATE subscriptions SET created_at=datetime('now','-10 days') "
               "WHERE id=?", (sid,))
    assert _pending_ids(db) == []             # outside the 7-day retry window
    with patch("app.mail._call_mailjet_batch", return_value=True) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()
