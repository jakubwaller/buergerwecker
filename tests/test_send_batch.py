from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.mail import send_batch, maybe_quota_alert, Outgoing


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


@pytest.fixture
def resend_on(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    # Mailjet creds are read by the batch call even when mocked out.
    for k, v in {"MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "s",
                 "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x"}.items():
        monkeypatch.setenv(k, v)


def _cfg(**over):
    base = dict(resend_daily_quota=100, mailjet_hourly_quota=10,
                quota_alert_threshold_pct=80, developer_email="dev@x")
    base.update(over)
    return SimpleNamespace(**base)


def _items(n, prefix="k"):
    return [Outgoing(to=f"u{i}@x.com", subject="s", body="b",
                     idem_key=f"{prefix}{i}") for i in range(n)]


def _sent(conn, provider):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sent_idempotency WHERE provider=?",
        (provider,)).fetchone()["n"]


def test_delivers_all_within_quota_via_resend(db, resend_on):
    with patch("app.mail._call_resend_batch", return_value=True) as rb, \
         patch("app.mail._call_mailjet_batch") as mb:
        res = send_batch(db, _items(3), _cfg())
    assert len(res.delivered) == 3 and res.deferred == 0
    rb.assert_called_once()          # one batch call, not three
    mb.assert_not_called()
    assert _sent(db, "resend") == 3


def test_chunks_into_batches_of_100(db, resend_on):
    with patch("app.mail._call_resend_batch", return_value=True) as rb, \
         patch("app.mail._call_mailjet_batch"):
        res = send_batch(db, _items(150), _cfg(resend_daily_quota=1000))
    assert len(res.delivered) == 150 and res.deferred == 0
    assert rb.call_count == 2        # 100 + 50
    assert len(rb.call_args_list[0].args[0]) == 100
    assert len(rb.call_args_list[1].args[0]) == 50


def test_defers_overflow_past_quota(db, resend_on):
    # Resend capped at 2, Mailjet disabled → 2 sent, 3 deferred.
    with patch("app.mail._call_resend_batch", return_value=True), \
         patch("app.mail._call_mailjet_batch") as mb:
        res = send_batch(db, _items(5), _cfg(resend_daily_quota=2,
                                             mailjet_hourly_quota=0))
    assert len(res.delivered) == 2 and res.deferred == 3
    mb.assert_not_called()
    assert _sent(db, "resend") == 2
    # Deferred claims must be released so a later cycle retries them.
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency").fetchone()["n"] == 2


def test_falls_over_to_mailjet_when_resend_errors(db, resend_on):
    with patch("app.mail._call_resend_batch", return_value=False) as rb, \
         patch("app.mail._call_mailjet_batch", return_value=True) as mb:
        res = send_batch(db, _items(4), _cfg())
    assert len(res.delivered) == 4 and res.deferred == 0
    rb.assert_called_once()
    mb.assert_called_once()
    assert _sent(db, "mailjet") == 4 and _sent(db, "resend") == 0


def test_existing_usage_counts_against_quota(db, resend_on):
    # Pre-existing resend sends in the last 24h eat into the daily quota.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"old{i}",) for i in range(9)])
    with patch("app.mail._call_resend_batch", return_value=True), \
         patch("app.mail._call_mailjet_batch", return_value=True):
        res = send_batch(db, _items(5), _cfg(resend_daily_quota=10,
                                             mailjet_hourly_quota=0))
    assert len(res.delivered) == 1 and res.deferred == 4   # only 1 resend slot left


def test_already_sent_idem_key_is_skipped(db, resend_on):
    db.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('k0','resend')")
    with patch("app.mail._call_resend_batch", return_value=True) as rb:
        res = send_batch(db, _items(1), _cfg())   # idem_key 'k0' already sent
    assert res.delivered == set() and res.deferred == 0
    rb.assert_not_called()


def test_quota_alert_fires_on_deferral_and_is_rate_limited(db):
    cfg = _cfg()
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, cfg, deferred=5)
        maybe_quota_alert(db, cfg, deferred=5)   # within 24h → suppressed
    snd.assert_called_once()
    assert db.execute(
        "SELECT COUNT(*) AS n FROM meta WHERE key='last_quota_alert_at'"
    ).fetchone()["n"] == 1


def test_quota_alert_fires_near_threshold(db):
    # 8/10 resend sends today = 80% → at threshold, alert even with no deferral.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"r{i}",) for i in range(8)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=10), deferred=0)
    snd.assert_called_once()


def test_quota_alert_silent_when_healthy(db):
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=100), deferred=0)
    snd.assert_not_called()
