from types import SimpleNamespace
from datetime import time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.models import Filter
from app.repo import insert_pending, confirm, pending_confirmations
from app.confirmations import build_confirmation, send_pending_confirmations


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
    # All quota exhausted → the cycle's send defers.
    with patch("app.mail._call_mailjet_batch", return_value=200), \
         patch("app.mail._call_brevo_batch", return_value=201):
        send_pending_confirmations(db, _cfg(mailjet_hourly_quota=0,
                                            mailjet_daily_quota=0,
                                            brevo_daily_quota=0))
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
        send_pending_confirmations(db, _cfg())
    assert _sent_at(db, sid) is not None
    # A second retry pass must not re-send an already-confirmed-sent sign-up.
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg())
    mb.assert_not_called()


def _spend_pool(conn, n):
    """`n` sends in the rolling 24h, what pool_usage counts."""
    conn.executemany("INSERT INTO sent_idempotency (idem_key, provider) "
                     "VALUES (?, 'brevo')", [(f"k{i}",) for i in range(n)])


def test_confirmations_stop_at_the_pool_pressure_line(db):
    """A flood of sign-ups must not spend the share of the pool the slot
    digests need: with mailjet 200 + brevo 300 a day and an 80% line,
    confirmations may take the pool to 400 and no further."""
    first, second = _sub(db, "one@x.com"), _sub(db, "two@x.com")
    _spend_pool(db, 399)
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg(mail_pool_pressure_pct=80))
    assert [len(c.args[0]) for c in mb.call_args_list] == [1]
    assert _sent_at(db, first) is not None    # oldest first
    assert second in _pending_ids(db)         # held, not dropped

    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg(mail_pool_pressure_pct=80))
    mb.assert_not_called()                    # at the line: nothing goes
    assert second in _pending_ids(db)


def test_a_dead_address_does_not_hold_the_head_of_the_queue(db):
    """send_batch never mails a suppressed address, so its row stays
    pending: it must not take the one confirmation the line has room for."""
    from app.repo import suppress_address
    dead = _sub(db, "dead@x.com")
    db.execute("UPDATE subscriptions SET created_at=datetime('now','-1 hour') "
               "WHERE id=?", (dead,))
    suppress_address(db, "dead@x.com", reason="hard_bounce")
    live = _sub(db, "live@x.com")
    _spend_pool(db, 399)
    with patch("app.mail._call_mailjet_batch", return_value=200):
        send_pending_confirmations(db, _cfg(mail_pool_pressure_pct=80))
    assert _sent_at(db, live) is not None


def test_a_signup_is_held_when_the_queue_ahead_fills_the_room(db):
    from app.confirmations import confirmation_held
    _sub(db, "one@x.com")
    _spend_pool(db, 399)                       # room for one
    assert not confirmation_held(db, _cfg(mail_pool_pressure_pct=80))
    _sub(db, "two@x.com")                      # one ahead of it
    assert confirmation_held(db, _cfg(mail_pool_pressure_pct=80))
    assert not confirmation_held(db, _cfg(mail_pool_pressure_pct=0))


def test_no_pressure_line_sends_everything_pending(db):
    _sub(db, "one@x.com"), _sub(db, "two@x.com")
    _spend_pool(db, 450)
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        send_pending_confirmations(db, _cfg(mail_pool_pressure_pct=0))
    assert [len(c.args[0]) for c in mb.call_args_list] == [2]


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
    item = build_confirmation(7, "a@example.com", lang, "leipzig",
                              _cfg(public_base_url="https://site.example"))
    assert item.to == "a@example.com"
    assert "Leipzig" in item.subject
    assert click in item.body
    assert own in item.body
    assert "https://site.example/confirm/" in item.body
    url_lines = [ln for ln in item.body.splitlines()
                 if ln.startswith("https://site.example/confirm/")]
    assert len(url_lines) == 1 and " " not in url_lines[0]
    # who signed up where: the city, and the site as a host, not the URL
    assert "Leipzig" in item.body
    assert " site.example " in item.body


def test_confirmation_without_a_city_name_still_reads_whole():
    item = build_confirmation(7, "a@example.com", "de", "no-such-city", _cfg())
    assert "(" not in item.subject
    assert "in None" not in item.body and "{" not in item.body
    assert "Klick auf diesen Link" in item.body
