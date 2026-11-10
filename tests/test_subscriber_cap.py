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
    "RATE_LIMIT_MINUTES": "15",
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
    assert "Sub cap mail 2 · app 10 per 24h  held today 1 · capped now 1 · 2 digests to 1 subscribers, 2.0/sub" in text


def test_summary_is_silent_when_the_cap_is_off(db, monkeypatch):
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    monkeypatch.setenv("MAX_PUSH_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    from app.config import load_config
    s = stats(db, load_config())
    assert s["subscriber_cap"] == 0 and s["capped_now"] == 0
    assert "Sub cap" not in render_summary_email(s, now=datetime.utcnow(), anomalies=[], base_url="https://x")


# ---------------------------------------------------------------------------
# The push cap, and the mail cap under pool pressure

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


def _push_sub(db, token="tok-1"):
    from app.repo import insert_push_subscription, register_device
    dev = register_device(db, platform="apns", token=token, secret_hash="h" * 64,
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


def test_a_push_subscriber_has_its_own_cap_of_ten(db, apns):
    """Push has no provider quota: the mail cap of 2 does not apply to it."""
    pid = _push_sub(db)
    relay = _Relay()
    for n in range(10):
        _cycle_both(db, [_slot(n)], f"c{n}", relay)
        _age_last_notified(db, pid, 16)
    assert relay.calls == 10 and digests_in_window(db, pid) == 10
    _cycle_both(db, [_slot(11)], "c11", relay)
    assert relay.calls == 10                # held at 10, not at 2
    assert [h[1] for h in _holds(db)] == [pid]


def test_zero_disables_the_push_cap_and_leaves_mail_capped(db, apns, monkeypatch):
    monkeypatch.setenv("MAX_PUSH_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    sid, pid = _sub(db), _push_sub(db)
    relay = _Relay()
    for n in range(12):
        _cycle_both(db, [_slot(n)], f"c{n}", relay)
        for s in (sid, pid):
            _age_last_notified(db, s, 16)
    assert digests_in_window(db, pid) == 12
    assert digests_in_window(db, sid) == 2
    assert [h[1] for h in _holds(db)] == [sid]



def test_the_mail_cap_tightens_once_the_pool_is_under_pressure(db, monkeypatch):
    from app.config import load_config
    from app.cycle import subscriber_cap
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "10")
    monkeypatch.setenv("BREVO_DAILY_QUOTA", "10")
    monkeypatch.setenv("MAIL_POOL_PRESSURE_PCT", "50")
    cfg = load_config()
    assert subscriber_cap(db, cfg) == (2, False)
    # 9 of 20 sent in the last 24h: 45%, below the line.
    db.executemany("INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
                   [(f"k{i}",) for i in range(9)])
    assert subscriber_cap(db, cfg) == (2, False)
    db.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('k9', 'mailjet')")
    assert subscriber_cap(db, cfg) == (1, True)
    # Yesterday's sends are outside the rolling window.
    db.execute("UPDATE sent_idempotency SET sent_at=datetime('now','-25 hours') "
               "WHERE idem_key='k9'")
    assert subscriber_cap(db, cfg) == (2, False)


def test_tightening_is_off_with_zero_or_a_cap_no_lower_than_the_ordinary_one(db, monkeypatch):
    from app.config import load_config
    from app.cycle import subscriber_cap
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "10")
    monkeypatch.setenv("BREVO_DAILY_QUOTA", "10")
    monkeypatch.setenv("MAIL_POOL_PRESSURE_PCT", "50")
    db.executemany("INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
                   [(f"k{i}",) for i in range(20)])
    monkeypatch.setenv("MAIL_CAP_UNDER_PRESSURE", "0")
    assert subscriber_cap(db, load_config()) == (2, False)
    monkeypatch.setenv("MAIL_CAP_UNDER_PRESSURE", "2")
    assert subscriber_cap(db, load_config()) == (2, False)
    monkeypatch.setenv("MAIL_CAP_UNDER_PRESSURE", "1")
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    # The ordinary cap turned off is off, pressure or not: /admin and the
    # summary only show the cap row while one is configured, so a hidden
    # pressure cap would be invisible everywhere but the poller log.
    assert subscriber_cap(db, load_config()) == (0, False)


def test_under_pressure_mail_is_capped_at_one_and_push_keeps_its_own_cap(db, apns, monkeypatch):
    """Push uses no mail quota, so pressure on the mail pool leaves it alone."""
    monkeypatch.setenv("CONFIRMATION_HEADROOM", "0")   # a 20-mail pool, all headroom
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
    assert digests_in_window(db, pid) == 2          # push: own cap of 10
    assert relay.calls == 2
    assert [h[1] for h in _holds(db)] == [sid]
    for s in (sid, pid):
        _age_last_notified(db, s, 16)
    _cycle_both(db, [_slot(3)], "c3", relay)
    assert digests_in_window(db, pid) == 3          # push: still under its cap of 10
    assert [h[1] for h in _holds(db)] == [sid]
    from app.config import load_config
    s = stats(db, load_config())
    assert s["mail_cap_tightened"] is True and s["mail_cap"] == 1
    text = render_summary_email(s, now=datetime.utcnow(), anomalies=[], base_url="https://x")
    assert ("Mail pool under pressure: mail subscribers capped at 1/24h "
            "(app unaffected)") in text
    assert s["capped_now"] == 1


def _push_only_at_cap(db, monkeypatch, mail_cap):
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", mail_cap)
    monkeypatch.setenv("MAX_PUSH_DIGESTS_PER_SUBSCRIBER_PER_DAY", "3")
    pid = _push_sub(db)
    relay = _Relay()
    for n in range(5):
        _cycle_both(db, [_slot(n)], f"c{n}", relay)
        _age_last_notified(db, pid, 16)
    assert relay.calls == 3
    from app.config import load_config
    return stats(db, load_config())


def test_admin_counts_a_push_subscriber_at_its_own_cap(db, apns, monkeypatch):
    s = _push_only_at_cap(db, monkeypatch, "2")
    assert s["capped_now"] == 1 and s["push_cap"] == 3


def test_admin_shows_the_push_cap_when_the_mail_cap_is_off(db, apns, monkeypatch):
    s = _push_only_at_cap(db, monkeypatch, "0")
    assert s["capped_now"] == 1 and s["cap_holds_today"] == 1
    text = render_summary_email(s, now=datetime.utcnow(), anomalies=[], base_url="https://x")
    assert "Sub cap mail off · app 3 per 24h" in text
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("DB_PATH", db.execute("PRAGMA database_list").fetchone()[2])
    from app.web import create_app
    body = create_app().test_client().get("/admin?token=" + "a" * 32).data.decode()
    assert "mail off · app 3 per rolling 24h" in body


# ---------------------------------------------------------------------------
# The per-slot cap: one slot reaches at most MAIL_RECIPIENTS_PER_SLOT mail
# subscribers per cycle, longest-waiting first. The rest have nothing
# recorded, so they hear of it at the next poll if it is still free.

def _subs_waiting(db, ages):
    """Mail subscribers whose last digest was ages[i] minutes ago."""
    sids = []
    for i, age in enumerate(ages):
        sid = _sub(db, f"s{i}@example.com")
        _age_last_notified(db, sid, age)
        sids.append(sid)
    return sids


def _told(db, sids):
    return [sid for sid in sids if _seen(db, sid)]


def test_one_slot_reaches_the_longest_waiting_mail_subscribers_first(db, monkeypatch):
    monkeypatch.setenv("MAIL_RECIPIENTS_PER_SLOT", "2")
    a, b, c, d, e = _subs_waiting(db, [60, 300, 120, 240, 180])
    _cycle(db, [_slot(1)], "c1")
    assert _told(db, [a, b, c, d, e]) == [b, d]
    assert _holds(db) == []                 # not a daily-cap hold
    # Still free at the next poll: the next two in line hear of it.
    _cycle(db, [_slot(1)], "c2")
    assert _told(db, [a, b, c, d, e]) == [b, c, d, e]
    assert digests_in_window(db, b) == digests_in_window(db, d) == 1


def test_app_subscribers_all_hear_of_the_slot_and_use_no_mail_budget(db, apns, monkeypatch):
    monkeypatch.setenv("MAIL_RECIPIENTS_PER_SLOT", "2")
    pushes = [_push_sub(db, f"tok-{i}") for i in range(3)]
    for pid in pushes:
        _age_last_notified(db, pid, 600)    # ahead of every mail subscriber
    mails = _subs_waiting(db, [300, 240, 180])
    relay = _Relay()
    _cycle_both(db, [_slot(1)], "c1", relay)
    assert relay.calls == 3 and _told(db, pushes) == pushes
    assert _told(db, mails) == mails[:2]


def test_a_full_slot_is_trimmed_and_only_the_rest_is_recorded(db, monkeypatch):
    from app import cycle
    monkeypatch.setenv("MAIL_RECIPIENTS_PER_SLOT", "1")
    one, two = _slot(1), _slot(2)           # 09:01 and 10:02
    narrow = Filter(appointment_types=["svc-A"], locations="all",
                    weekdays=[1, 2, 3, 4, 5, 6, 7],
                    time_window_start=time(9, 0), time_window_end=time(9, 30))
    a = insert_pending(db, email="a@example.com", city="leipzig", language="de",
                       filter_=narrow, ttl_days=90)
    confirm(db, a)
    b = _sub(db, "b@example.com")
    _age_last_notified(db, a, 300)
    _age_last_notified(db, b, 200)
    staged = {}
    real = cycle.send_digest

    def spy(**kw):
        staged[kw["subscription"].id] = ([s.hash() for s in kw["matched_slots"]],
                                         [k.key for k in kw["seen_keys"]])
        return real(**kw)
    monkeypatch.setattr(cycle, "send_digest", spy)

    def seen_hashes(sid):
        return {r[0] for r in db.execute(
            "SELECT slot_hash FROM seen_slots WHERE subscription_id=?", (sid,))}
    _cycle(db, [one, two], "c1")
    assert staged == {a: ([one.hash()], [one.hash()]),
                      b: ([two.hash()], [two.hash()])}
    assert seen_hashes(b) == {two.hash()}
    # `one` is still free: b hears of it at the next poll.
    _age_last_notified(db, b, 16)
    _cycle(db, [one, two], "c2")
    assert seen_hashes(b) == {one.hash(), two.hash()}


def test_zero_turns_the_per_slot_cap_off(db, monkeypatch):
    monkeypatch.setenv("MAIL_RECIPIENTS_PER_SLOT", "0")
    sids = _subs_waiting(db, [60, 300, 120, 240, 180])
    _cycle(db, [_slot(1)], "c1")
    assert _told(db, sids) == sids


def test_a_daily_cap_hold_uses_no_per_slot_budget(db, monkeypatch):
    monkeypatch.setenv("MAIL_RECIPIENTS_PER_SLOT", "1")
    capped, b = _subs_waiting(db, [300, 200])
    db.executemany("INSERT INTO digest_deliveries (subscription_id) VALUES (?)",
                   [(capped,), (capped,)])
    _cycle(db, [_slot(1)], "c1")
    assert _told(db, [capped, b]) == [b]
    assert [h[1] for h in _holds(db)] == [capped]


def test_a_day_tenant_counts_by_its_day_key(db, monkeypatch):
    """Münster keys seen_slots on the day, so two times on one day are one
    slot to the cap: one digest with both counts once, and subscribers who
    each match a different time share the budget."""
    monkeypatch.setenv("MAIL_RECIPIENTS_PER_SLOT", "2")

    def sub(email, start, end, age):
        f = Filter(appointment_types=["2408"], locations="all",
                   weekdays=[1, 2, 3, 4, 5, 6, 7],
                   time_window_start=start, time_window_end=end)
        sid = insert_pending(db, email=email, city="muenster-kfz", language="de",
                             filter_=f, ttl_days=90)
        confirm(db, sid)
        _age_last_notified(db, sid, age)
        return sid
    both = sub("a@example.com", time(0, 0), time(23, 59), 300)
    early_only = sub("b@example.com", time(8, 0), time(10, 0), 200)
    late_only = sub("c@example.com", time(10, 0), time(12, 0), 100)
    _cycle(db, [Slot("2026-12-01", "09:00", "243", "2408", "t1"),
                Slot("2026-12-01", "11:00", "243", "2408", "t2")], "c1")
    assert _told(db, [both, early_only, late_only]) == [both, early_only]
