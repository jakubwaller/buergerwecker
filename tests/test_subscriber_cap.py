"""The per-subscriber daily cap: at most MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY
digests per rolling 24h, whatever the adaptive cadence would allow. A held
digest is dropped, not queued — its slots stay unseen and go out in the first
cycle after the window frees."""
from datetime import time
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.models import Filter, Slot
from app.repo import insert_pending, confirm, digests_in_window
from app.cycle import run_cycle
from datetime import datetime
from app.admin import stats, render_summary_email


_ENV = {
    "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m", "MAILJET_FROM_EMAIL": "x@x",
    "MAILJET_FROM_NAME": "x", "MAILJET_DAILY_QUOTA": "6000", "BREVO_API_KEY": "b",
    "TOKEN_SECRET_PRIMARY": "x" * 32, "TOKEN_SECRET_PREVIOUS": "",
    "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
    "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
    "SUBSCRIPTION_TTL_DAYS": "90", "RENEWAL_REMINDER_DAYS_BEFORE": "10",
    "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
    "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
    "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
    "DEVELOPER_EMAIL": "dev@x", "KOFI_URL": "https://k",
    "BREVO_DAILY_QUOTA": "300", "MAILJET_HOURLY_QUOTA": "100",
    # The adaptive ladder off, so only the cap decides who is held.
    "ADAPTIVE_RATE_LIMIT_MAX_MULTIPLIER": "1",
    "MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY": "2",
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _f():
    return Filter(appointment_types=["svc-A"], locations="all",
                  weekdays=[1, 2, 3, 4, 5, 6, 7],
                  time_window_start=time(0, 0), time_window_end=time(23, 59))


def _slot(i):
    return Slot(f"2026-09-{(i % 28) + 1:02d}", f"{8 + (i % 12):02d}:{i % 60:02d}",
                "loc-1", "svc-A", f"tok-{i}")


def _sub(db, email="a@example.com"):
    sid = insert_pending(db, email=email, city="leipzig", language="de",
                         filter_=_f(), ttl_days=90)
    confirm(db, sid)
    return sid


def _cycle(db, slots, cycle_id):
    scraper = MagicMock()
    scraper.poll.return_value = slots
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_brevo_batch", return_value=201):
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id=cycle_id)
    return mb.call_count


def _age_last_notified(db, sid, minutes):
    db.execute(f"UPDATE subscriptions SET last_notified_at="
               f"datetime('now','-{minutes} minutes') WHERE id=?", (sid,))


def _seen(db, sid):
    return db.execute("SELECT COUNT(*) FROM seen_slots WHERE subscription_id=?",
                      (sid,)).fetchone()[0]


def _holds(db):
    return [tuple(r) for r in
            db.execute("SELECT day, subscription_id FROM digest_cap_holds").fetchall()]


def test_third_digest_in_24h_is_held_and_its_slots_stay_unseen(db):
    sid = _sub(db)
    assert _cycle(db, [_slot(1)], "c1") == 1
    _age_last_notified(db, sid, 16)
    assert _cycle(db, [_slot(2)], "c2") == 1
    assert digests_in_window(db, sid) == 2
    _age_last_notified(db, sid, 16)
    # Fresh slot, cadence allows it, the cap does not.
    assert _cycle(db, [_slot(3)], "c3") == 0
    assert _seen(db, sid) == 2          # slot 3 was not recorded as seen
    assert _holds(db) == [(db.execute("SELECT date('now')").fetchone()[0], sid)]
    # Re-evaluated every cycle while capped, recorded once per day.
    _age_last_notified(db, sid, 16)
    assert _cycle(db, [_slot(3)], "c4") == 0
    assert len(_holds(db)) == 1


def test_digest_goes_out_once_the_window_frees(db):
    sid = _sub(db)
    _cycle(db, [_slot(1)], "c1")
    _age_last_notified(db, sid, 16)
    _cycle(db, [_slot(2)], "c2")
    _age_last_notified(db, sid, 16)
    assert _cycle(db, [_slot(3)], "c3") == 0
    # The oldest delivery ages out of the rolling window.
    db.execute("UPDATE digest_deliveries SET sent_at=datetime('now','-25 hours') "
               "WHERE rowid = (SELECT MIN(rowid) FROM digest_deliveries)")
    assert _cycle(db, [_slot(3)], "c4") == 1
    assert _seen(db, sid) == 3
    assert digests_in_window(db, sid) == 2


def test_a_capped_subscriber_with_nothing_new_is_not_a_hold(db):
    sid = _sub(db)
    _cycle(db, [_slot(1)], "c1")
    _age_last_notified(db, sid, 16)
    _cycle(db, [_slot(2)], "c2")
    _age_last_notified(db, sid, 16)
    # Same slots again: nothing to send, so nothing was held either.
    assert _cycle(db, [_slot(1), _slot(2)], "c3") == 0
    assert _holds(db) == []


def test_zero_disables_the_cap(db, monkeypatch):
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    sid = _sub(db)
    for i in range(1, 5):
        _age_last_notified(db, sid, 16)
        assert _cycle(db, [_slot(i)], f"c{i}") == 1
    assert _holds(db) == []


def test_cap_is_per_subscriber(db):
    a = _sub(db, "a@example.com")
    b = _sub(db, "b@example.com")
    assert _cycle(db, [_slot(1)], "c1") == 1     # one batch call, two mails
    for sid in (a, b):
        _age_last_notified(db, sid, 16)
    _cycle(db, [_slot(2)], "c2")
    # Only `a` gets a third delivery pre-loaded into its window.
    db.execute("INSERT INTO digest_deliveries (subscription_id) VALUES (?)", (a,))
    for sid in (a, b):
        _age_last_notified(db, sid, 16)
    # Wait — a already has 3 ≥ 2, b has 2 ≥ 2: both capped. Free b by ageing
    # one of its deliveries out of the window.
    db.execute("UPDATE digest_deliveries SET sent_at=datetime('now','-25 hours') "
               "WHERE rowid = (SELECT MIN(rowid) FROM digest_deliveries "
               "WHERE subscription_id=?)", (b,))
    assert _cycle(db, [_slot(3)], "c3") == 1
    assert _seen(db, b) == 3
    assert _seen(db, a) == 2
    assert [h[1] for h in _holds(db)] == [a]


def test_migration_seeds_deliveries_from_seen_slots(db):
    sid = _sub(db)
    # Two digests' worth of seen_slots in the last day, one older.
    for h, minutes in (("h1", 30), ("h2", 30), ("h3", 90), ("h4", 26 * 60)):
        db.execute("INSERT INTO seen_slots (subscription_id, slot_hash, sent_at) "
                   "VALUES (?, ?, datetime('now', ?))", (sid, h, f"-{minutes} minutes"))
    db.execute("DELETE FROM digest_deliveries")
    init_schema(db)
    assert digests_in_window(db, sid) == 2
    # Idempotent: a second init on a non-empty table adds nothing.
    init_schema(db)
    assert digests_in_window(db, sid) == 2


def test_deliveries_go_with_the_subscription(db):
    sid = _sub(db)
    _cycle(db, [_slot(1)], "c1")
    assert digests_in_window(db, sid) == 1
    db.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
    assert db.execute("SELECT COUNT(*) FROM digest_deliveries").fetchone()[0] == 0


def test_housekeeping_prunes_old_rows(db):
    from app.housekeeping import _prune_digest_deliveries, _prune_cap_holds
    sid = _sub(db)
    db.execute("INSERT INTO digest_deliveries (subscription_id, sent_at) "
               "VALUES (?, datetime('now','-8 days'))", (sid,))
    db.execute("INSERT INTO digest_deliveries (subscription_id) VALUES (?)", (sid,))
    db.execute("INSERT INTO digest_cap_holds (day, subscription_id) "
               "VALUES (date('now','-91 days'), ?)", (sid,))
    db.execute("INSERT INTO digest_cap_holds (day, subscription_id) "
               "VALUES (date('now'), ?)", (sid,))
    _prune_digest_deliveries(db)
    _prune_cap_holds(db)
    assert db.execute("SELECT COUNT(*) FROM digest_deliveries").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM digest_cap_holds").fetchone()[0] == 1


def test_admin_stats_and_summary_carry_the_cap(db):
    from app.config import load_config
    cfg = load_config()
    sid = _sub(db)
    _cycle(db, [_slot(1)], "c1")
    _age_last_notified(db, sid, 16)
    _cycle(db, [_slot(2)], "c2")
    _age_last_notified(db, sid, 16)
    _cycle(db, [_slot(3)], "c3")
    s = stats(db, cfg)
    assert s["subscriber_cap"] == 2
    assert s["cap_holds_today"] == 1
    assert s["cap_holds_7d"] == 1
    assert s["capped_now"] == 1
    assert s["digests_per_sub_24h"] == {"subs": 1, "digests": 2, "mean": 2.0}
    text = render_summary_email(s, now=datetime.utcnow(), anomalies=[], base_url="https://x")
    assert "Sub cap 2/24h  held today 1 · capped now 1 · 2 digests to 1 subscribers, 2.0/sub" in text


def test_summary_is_silent_when_the_cap_is_off(db, monkeypatch):
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    from app.config import load_config
    s = stats(db, load_config())
    assert s["subscriber_cap"] == 0 and s["capped_now"] == 0
    assert "Sub cap" not in render_summary_email(s, now=datetime.utcnow(), anomalies=[], base_url="https://x")


# ---------------------------------------------------------------------------
# The same cap on both channels, and the mail cap under pool pressure

def _ec_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


@pytest.fixture
def apns(monkeypatch):
    monkeypatch.setenv("APNS_TEAM_ID", "TEAM123456")
    monkeypatch.setenv("APNS_KEY_ID", "KEY1234567")
    monkeypatch.setenv("APNS_KEY_P8", _ec_pem())
    monkeypatch.setenv("APNS_TOPIC", "de.buergerwecker.app")
    from app import push
    push._apns_jwt.clear(); push._clients.clear()
    yield
    push._apns_jwt.clear(); push._clients.clear()


class _Relay:
    def __init__(self):
        self.calls = 0

    def __call__(self, platform, url, *, headers=None, json=None, data=None):
        import httpx
        self.calls += 1
        return httpx.Response(200, json={})


def _push_sub(db):
    from app.repo import insert_push_subscription, register_device
    dev = register_device(db, platform="apns", token="tok-1", secret_hash="h" * 64,
                          language="de")
    db.execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP WHERE id=?",
               (dev,))
    return insert_push_subscription(db, device_id=dev, city="leipzig", language="de",
                                    filter_=_f(), ttl_days=90)


def _cycle_both(db, slots, cycle_id, relay):
    scraper = MagicMock()
    scraper.poll.return_value = slots
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_brevo_batch", return_value=201), \
         patch("app.push._post", relay):
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id=cycle_id)
    return mb.call_count


def test_a_push_subscriber_is_capped_exactly_like_a_mail_subscriber(db, apns):
    """Push is not a fast lane: the daily cap is the same on both channels."""
    pid = _push_sub(db)
    relay = _Relay()
    _cycle_both(db, [_slot(1)], "c1", relay)
    _age_last_notified(db, pid, 16)
    _cycle_both(db, [_slot(2)], "c2", relay)
    assert relay.calls == 2 and digests_in_window(db, pid) == 2
    _age_last_notified(db, pid, 16)
    _cycle_both(db, [_slot(3)], "c3", relay)
    assert relay.calls == 2                 # held
    assert _seen(db, pid) == 2
    assert [h[1] for h in _holds(db)] == [pid]


def test_subscriber_caps_tighten_mail_only_once_the_pool_is_under_pressure(db, monkeypatch):
    from app.config import load_config
    from app.cycle import subscriber_caps
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "10")
    monkeypatch.setenv("BREVO_DAILY_QUOTA", "10")
    monkeypatch.setenv("MAIL_POOL_PRESSURE_PCT", "50")
    cfg = load_config()
    assert subscriber_caps(db, cfg) == (2, 2, False)
    # 9 of 20 sent in the last 24h: 45%, below the line.
    db.executemany("INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
                   [(f"k{i}",) for i in range(9)])
    assert subscriber_caps(db, cfg) == (2, 2, False)
    db.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('k9', 'mailjet')")
    assert subscriber_caps(db, cfg) == (2, 1, True)
    # Yesterday's sends are outside the rolling window.
    db.execute("UPDATE sent_idempotency SET sent_at=datetime('now','-25 hours') "
               "WHERE idem_key='k9'")
    assert subscriber_caps(db, cfg) == (2, 2, False)


def test_tightening_is_off_with_zero_or_a_cap_no_lower_than_the_ordinary_one(db, monkeypatch):
    from app.config import load_config
    from app.cycle import subscriber_caps
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "10")
    monkeypatch.setenv("BREVO_DAILY_QUOTA", "10")
    monkeypatch.setenv("MAIL_POOL_PRESSURE_PCT", "50")
    db.executemany("INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
                   [(f"k{i}",) for i in range(20)])
    monkeypatch.setenv("MAIL_CAP_UNDER_PRESSURE", "0")
    assert subscriber_caps(db, load_config()) == (2, 2, False)
    monkeypatch.setenv("MAIL_CAP_UNDER_PRESSURE", "2")
    assert subscriber_caps(db, load_config()) == (2, 2, False)
    monkeypatch.setenv("MAIL_CAP_UNDER_PRESSURE", "1")
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    # The ordinary cap turned off is off, pressure or not: /admin and the
    # summary only show the cap row while one is configured, so a hidden
    # pressure cap would be invisible everywhere but the poller log.
    assert subscriber_caps(db, load_config()) == (0, 0, False)


def test_under_pressure_mail_gets_one_digest_a_day_and_push_keeps_two(db, apns, monkeypatch):
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "10")
    monkeypatch.setenv("BREVO_DAILY_QUOTA", "10")
    monkeypatch.setenv("MAIL_POOL_PRESSURE_PCT", "50")
    db.executemany("INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
                   [(f"k{i}",) for i in range(10)])
    sid = _sub(db)
    pid = _push_sub(db)
    relay = _Relay()
    _cycle_both(db, [_slot(1)], "c1", relay)
    assert digests_in_window(db, sid) == 1 and digests_in_window(db, pid) == 1
    for s in (sid, pid):
        _age_last_notified(db, s, 16)
    _cycle_both(db, [_slot(2)], "c2", relay)
    assert digests_in_window(db, sid) == 1          # mail: held at the tightened cap
    assert digests_in_window(db, pid) == 2          # push: the ordinary cap
    assert [h[1] for h in _holds(db)] == [sid]
    from app.config import load_config
    s = stats(db, load_config())
    assert s["mail_cap_tightened"] is True and s["mail_cap"] == 1
    text = render_summary_email(s, now=datetime.utcnow(), anomalies=[], base_url="https://x")
    assert "Mail pool under pressure: mail subscribers capped at 1/24h" in text
