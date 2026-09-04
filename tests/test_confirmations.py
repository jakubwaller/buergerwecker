from types import SimpleNamespace
from datetime import time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.models import Filter
from app.repo import insert_pending, confirm, pending_confirmations
from app.confirmations import (build_confirmation, send_confirmation_now,
                               send_pending_confirmations)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "b")
    for k, v in {"MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "s",
                 "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x"}.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _cfg(**over):
    base = dict(brevo_daily_quota=300, mailjet_hourly_quota=10,
                mailjet_daily_quota=200, quota_alert_threshold_pct=80,
                developer_email="dev@x", email_provider_order=("mailjet", "brevo"),
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
    with patch("app.mail._call_mailjet_batch", return_value=200), \
         patch("app.mail._call_brevo_batch", return_value=201):
        delivered = send_confirmation_now(db, sid, "a@x.com", "de", "leipzig",
                                          _cfg(mailjet_hourly_quota=0,
                                               mailjet_daily_quota=0,
                                               brevo_daily_quota=0))
    assert delivered is False
    assert _sent_at(db, sid) is None          # not marked sent
    assert sid in _pending_ids(db)            # still awaiting confirmation

    # Later cycle, quota available → retry pass delivers it.
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_called_once()
    assert _sent_at(db, sid) is not None
    assert sid not in _pending_ids(db)


def test_retry_is_idempotent_once_delivered(db):
    sid = _sub(db)
    with patch("app.mail._call_mailjet_batch", return_value=200):
        assert send_confirmation_now(db, sid, "a@x.com", "de", "leipzig", _cfg()) is True
    # A second retry pass must not re-send an already-confirmed-sent sign-up.
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()


def test_retry_skips_already_confirmed_users(db):
    sid = _sub(db, "b@x.com")
    confirm(db, sid)                          # user clicked the link already
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()


def test_retry_abandons_stale_signups(db):
    sid = _sub(db, "old@x.com")
    db.execute("UPDATE subscriptions SET created_at=datetime('now','-10 days') "
               "WHERE id=?", (sid,))
    assert _pending_ids(db) == []             # outside the 7-day retry window
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()


@pytest.mark.parametrize("lang, click, own", [
    ("de", "Klick auf diesen Link", "Wenn du dich nicht angemeldet hast"),
    ("en", "Click this link", "If you did not sign up"),
])
def test_confirmation_says_to_click_and_puts_the_link_on_its_own_line(lang, click, own):
    # A bare "Bitte bestätige dein Abonnement: <url>" got answered by reply
    # mail instead of a click (twice, by 2026-09-04).
    item = build_confirmation(7, "a@example.com", lang, "leipzig", _cfg())
    assert item.to == "a@example.com"
    assert "Leipzig" in item.subject
    assert click in item.body
    assert own in item.body
    assert "https://x/confirm/" in item.body
    url_lines = [ln for ln in item.body.splitlines() if ln.startswith("https://x/confirm/")]
    assert len(url_lines) == 1 and " " not in url_lines[0]
    assert "Leipzig" in item.body and "x" in item.body   # who, where, from which site


def test_confirmation_without_a_city_name_still_reads_whole():
    item = build_confirmation(7, "a@example.com", "de", "no-such-city", _cfg())
    assert "(" not in item.subject
    assert "in None" not in item.body and "{" not in item.body
    assert "Klick auf diesen Link" in item.body
