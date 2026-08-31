"""The one-off apology mail for terms that expired before the check-in existed.

Pre-PR-#70 expiries got no warning: digests just stopped. The script mails the
still-renewable part of that cohort once — renew link, unsubscribe link, an
apology — through the quota-aware batch path, and stamps the same once-per-term
latch the check-in uses so nobody is asked twice.
"""
import pathlib
import subprocess
import sys
from datetime import time
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.notify_silent_expired import cohort, run  # noqa: E402

from app.config import load_config  # noqa: E402
from app.db import connect, init_schema  # noqa: E402
from app.models import Filter  # noqa: E402
from app.repo import confirm, insert_pending  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE", "10")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("EXPIRED_GRACE_DAYS", "14")
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("MAILJET_API_KEY", "m"); monkeypatch.setenv("MAILJET_API_SECRET", "m")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x"); monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_HOURLY_QUOTA", "200")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "200")
    monkeypatch.setenv("ADMIN_TOKEN", "a"*32)
    monkeypatch.setenv("DEDUP_WINDOW_HOURS", "24"); monkeypatch.setenv("RATE_LIMIT_MINUTES", "15")
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "10"); monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS", "2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "99")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "99")
    monkeypatch.setenv("DEVELOPER_EMAIL", "dev@x"); monkeypatch.setenv("KOFI_URL", "https://k")
    monkeypatch.setenv("WEBHOOK_SECRET", "w"*32)
    conn = connect(db_path); init_schema(conn)
    return conn


def _f():
    return Filter(appointment_types=["A"], locations="all", weekdays=[1,2,3,4,5,6,7],
                  time_window_start=time(0,0), time_window_end=time(23,59))


def _sub(db, email="a@example.com", lang="de", expired_days_ago=3,
         confirmed=True):
    sid = insert_pending(db, email=email, city="leipzig", language=lang,
                         filter_=_f(), ttl_days=90)
    if confirmed:
        confirm(db, sid)
    db.execute("UPDATE subscriptions SET expires_at=datetime('now', ?) "
               "WHERE id=?", (f"{-expired_days_ago:+d} days", sid))
    return sid


def test_cohort_is_expired_unwarned_and_still_renewable(db):
    inside = _sub(db, "in@example.com", expired_days_ago=3)
    _sub(db, "future@example.com", expired_days_ago=-30)       # check-in's job
    _sub(db, "gone@example.com", expired_days_ago=20)          # grace over
    _sub(db, "pending@example.com", confirmed=False)           # never confirmed
    asked = _sub(db, "asked@example.com", expired_days_ago=3)  # check-in sent
    db.execute("UPDATE subscriptions SET reminder_sent_at=CURRENT_TIMESTAMP "
               "WHERE id=?", (asked,))
    deleted = _sub(db, "deleted@example.com", expired_days_ago=3)
    db.execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP "
               "WHERE id=?", (deleted,))
    rows = cohort(db, load_config())
    assert [r["id"] for r in rows] == [inside]


def test_dry_run_reports_and_sends_nothing(db):
    _sub(db)
    with patch("app.mail._call_mailjet_batch") as mb:
        stats = run(db, load_config(), send=False)
    assert stats["cohort"] == 1 and stats["delivered"] == 0
    mb.assert_not_called()
    row = db.execute("SELECT reminder_sent_at FROM subscriptions").fetchone()
    assert row["reminder_sent_at"] is None
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency"
                      ).fetchone()["n"] == 0


def test_send_delivers_apology_with_both_links_and_stamps_latch(db):
    from datetime import datetime, timedelta
    sid = _sub(db, expired_days_ago=3)
    sent_items = []
    def _capture(items):
        sent_items.extend(items)
        return 200
    with patch("app.mail._call_mailjet_batch", side_effect=_capture):
        stats = run(db, load_config(), send=True)
    assert stats == {"cohort": 1, "delivered": 1, "deferred": 0,
                     "undeliverable": 0, "marked": 1,
                     "by_provider": {"mailjet": 1}}
    (item,) = sent_items
    assert item.to == "a@example.com"
    assert "sorry" in item.subject
    assert "https://x/renew/" in item.body
    assert "https://x/unsubscribe/" in item.body
    assert item.unsub_url.startswith("https://x/unsubscribe/")
    # Both dates: when it stopped, and until when the renew link revives it.
    expires = db.execute("SELECT expires_at FROM subscriptions WHERE id=?",
                         (sid,)).fetchone()["expires_at"]
    stop = datetime.fromisoformat(expires[:19]).date()
    assert stop.strftime("%d.%m.%Y") in item.body
    assert (stop + timedelta(days=14)).strftime("%d.%m.%Y") in item.body
    row = db.execute("SELECT reminder_sent_at FROM subscriptions WHERE id=?",
                     (sid,)).fetchone()
    assert row["reminder_sent_at"] is not None


def test_english_subscriber_gets_the_english_mail(db):
    _sub(db, lang="en")
    sent_items = []
    def _capture(items):
        sent_items.extend(items)
        return 200
    with patch("app.mail._call_mailjet_batch", side_effect=_capture):
        run(db, load_config(), send=True)
    (item,) = sent_items
    assert "Still looking?" in item.body
    assert "Yes, keep looking: https://x/renew/" in item.body


def test_second_send_run_mails_nobody(db):
    _sub(db)
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        run(db, load_config(), send=True)
        stats = run(db, load_config(), send=True)
    assert mb.call_count == 1
    assert stats["cohort"] == 0 and stats["delivered"] == 0


def test_over_quota_defers_without_stamping_so_a_rerun_retries(db, monkeypatch):
    monkeypatch.setenv("MAILJET_HOURLY_QUOTA", "0")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "0")
    sid = _sub(db)
    with patch("app.mail._call_mailjet_batch") as mb:
        stats = run(db, load_config(), send=True)
    mb.assert_not_called()
    assert stats["deferred"] == 1 and stats["marked"] == 0
    assert db.execute("SELECT reminder_sent_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["reminder_sent_at"] is None
    # The quota frees; the rerun delivers what was deferred.
    monkeypatch.setenv("MAILJET_HOURLY_QUOTA", "200")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "200")
    with patch("app.mail._call_mailjet_batch", return_value=200):
        stats = run(db, load_config(), send=True)
    assert stats["delivered"] == 1 and stats["marked"] == 1


def test_suppressed_address_is_left_alone(db):
    sid = _sub(db)
    db.execute("INSERT INTO email_suppressions (email, reason) "
               "VALUES ('a@example.com', 'bounce')")
    with patch("app.mail._call_mailjet_batch") as mb:
        stats = run(db, load_config(), send=True)
    mb.assert_not_called()
    assert stats["undeliverable"] == 1 and stats["marked"] == 0
    assert db.execute("SELECT reminder_sent_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["reminder_sent_at"] is None


def test_runs_as_a_plain_script_from_a_foreign_cwd(tmp_path):
    """Guards the sys.path bootstrap: module-scope imports run before main."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "notify_silent_expired.py"),
         "--help"],
        cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
