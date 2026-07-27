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


def _cfg(order=("mailjet", "resend"), **over):
    base = dict(resend_daily_quota=100, mailjet_hourly_quota=10,
                mailjet_daily_quota=100_000,  # effectively unbounded unless set
                quota_alert_threshold_pct=80, developer_email="dev@x",
                email_provider_order=order)
    base.update(over)
    return SimpleNamespace(**base)


def _items(n, prefix="k"):
    return [Outgoing(to=f"u{i}@x.com", subject="s", body="b",
                     idem_key=f"{prefix}{i}") for i in range(n)]


def _sent(conn, provider):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM sent_idempotency WHERE provider=?",
        (provider,)).fetchone()["n"]


_RESEND_FIRST = ("resend", "mailjet")


def test_delivers_all_within_quota_via_resend(db, resend_on):
    with patch("app.mail._call_resend_batch", return_value=200) as rb, \
         patch("app.mail._call_mailjet_batch") as mb:
        res = send_batch(db, _items(3), _cfg(order=_RESEND_FIRST))
    assert len(res.delivered) == 3 and res.deferred == 0
    rb.assert_called_once()          # one batch call, not three
    mb.assert_not_called()
    assert _sent(db, "resend") == 3


def test_default_order_sends_via_mailjet_first(db, resend_on):
    # Production default is Mailjet-first: within its hourly quota, digests go
    # through Mailjet and Resend is not touched.
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_resend_batch") as rb:
        res = send_batch(db, _items(4), _cfg())   # default order = mailjet,resend
    assert len(res.delivered) == 4 and res.deferred == 0
    mb.assert_called_once()
    rb.assert_not_called()
    assert _sent(db, "mailjet") == 4


def test_order_is_configurable(db, resend_on):
    # Flipping the order routes the same batch to the other provider.
    with patch("app.mail._call_mailjet_batch") as mb, \
         patch("app.mail._call_resend_batch", return_value=200) as rb:
        send_batch(db, _items(2), _cfg(order=("resend", "mailjet")))
    rb.assert_called_once()
    mb.assert_not_called()


def test_mailjet_daily_cap_binds_when_tighter_than_hourly(db, resend_on):
    # Hourly headroom is generous (50) but the daily cap (200/day free tier) has
    # only 3 left → Mailjet sends 3, the rest spill to Resend. The 197 prior
    # sends are dated 2h ago: inside the daily window, outside the hourly one.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider, sent_at) "
        "VALUES (?, 'mailjet', datetime('now','-2 hours'))",
        [(f"mj{i}",) for i in range(197)])
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_resend_batch", return_value=200) as rb:
        res = send_batch(db, _items(5), _cfg(mailjet_hourly_quota=50,
                                             mailjet_daily_quota=200,
                                             resend_daily_quota=100))
    assert len(res.delivered) == 5 and res.deferred == 0
    assert res.sent_by_provider.get("mailjet") == 3    # 200 - 197
    assert res.sent_by_provider.get("resend") == 2


def test_mailjet_overflow_spills_to_resend(db, resend_on):
    # Mailjet-first with only 2/hour of headroom: 2 go via Mailjet, the rest
    # spill to Resend — this is the warm-up-period behaviour.
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_resend_batch", return_value=200) as rb:
        res = send_batch(db, _items(5), _cfg(mailjet_hourly_quota=2,
                                             resend_daily_quota=100))
    assert len(res.delivered) == 5 and res.deferred == 0
    assert _sent(db, "mailjet") == 2 and _sent(db, "resend") == 3
    mb.assert_called_once()
    rb.assert_called_once()


def test_chunks_into_batches_of_100(db, resend_on):
    with patch("app.mail._call_resend_batch", return_value=200) as rb, \
         patch("app.mail._call_mailjet_batch"):
        res = send_batch(db, _items(150),
                         _cfg(order=_RESEND_FIRST, resend_daily_quota=1000))
    assert len(res.delivered) == 150 and res.deferred == 0
    assert rb.call_count == 2        # 100 + 50
    assert len(rb.call_args_list[0].args[0]) == 100
    assert len(rb.call_args_list[1].args[0]) == 50


def test_defers_overflow_past_quota(db, resend_on):
    # Resend capped at 2, Mailjet disabled → 2 sent, 3 deferred.
    with patch("app.mail._call_resend_batch", return_value=200), \
         patch("app.mail._call_mailjet_batch") as mb:
        res = send_batch(db, _items(5), _cfg(order=_RESEND_FIRST,
                                             resend_daily_quota=2,
                                             mailjet_hourly_quota=0))
    assert len(res.delivered) == 2 and res.deferred == 3
    mb.assert_not_called()
    assert _sent(db, "resend") == 2
    # Deferred claims must be released so a later cycle retries them.
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency").fetchone()["n"] == 2


def test_falls_over_to_mailjet_when_resend_errors(db, resend_on):
    with patch("app.mail._call_resend_batch", return_value=500) as rb, \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        res = send_batch(db, _items(4), _cfg(order=_RESEND_FIRST))
    assert len(res.delivered) == 4 and res.deferred == 0
    rb.assert_called_once()
    mb.assert_called_once()
    assert _sent(db, "mailjet") == 4 and _sent(db, "resend") == 0


def test_existing_usage_counts_against_quota(db, resend_on):
    # Pre-existing resend sends in the last 24h eat into the daily quota.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"old{i}",) for i in range(9)])
    with patch("app.mail._call_resend_batch", return_value=200), \
         patch("app.mail._call_mailjet_batch", return_value=200):
        res = send_batch(db, _items(5), _cfg(order=_RESEND_FIRST,
                                             resend_daily_quota=10,
                                             mailjet_hourly_quota=0))
    assert len(res.delivered) == 1 and res.deferred == 4   # only 1 resend slot left


def test_already_sent_idem_key_is_skipped(db, resend_on):
    db.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('k0','resend')")
    with patch("app.mail._call_resend_batch", return_value=200) as rb, \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        res = send_batch(db, _items(1), _cfg())   # idem_key 'k0' already sent
    assert res.delivered == set() and res.deferred == 0
    rb.assert_not_called()
    mb.assert_not_called()


def test_quota_alert_fires_on_deferral_and_is_rate_limited(db):
    cfg = _cfg()
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, cfg, deferred=5)
        maybe_quota_alert(db, cfg, deferred=5)   # within 24h → suppressed
    snd.assert_called_once()
    assert db.execute(
        "SELECT COUNT(*) AS n FROM meta WHERE key='last_quota_alert_at'"
    ).fetchone()["n"] == 1


def test_quota_alert_fires_near_threshold(db, resend_on):
    # 8/10 resend sends today = 80% → at threshold, alert even with no deferral.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"r{i}",) for i in range(8)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=10), deferred=0)
    snd.assert_called_once()


def test_quota_alert_fires_when_mailjet_nears_its_cap(db):
    """Regression: the alert used to measure Resend only. Mailjet carries all
    the notification traffic and Resend just absorbs the overflow, so on
    2026-07-27 Mailjet sat at 197/200 while the alert read 0% and stayed
    silent."""
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
        [(f"m{i}",) for i in range(197)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(mailjet_daily_quota=200), deferred=0)
    snd.assert_called_once()
    body = snd.call_args.args[3]
    assert "mailjet: 197/200 (98%)" in body


def test_quota_alert_ignores_providers_that_cannot_send(db):
    """No RESEND_API_KEY → Resend is not in the send path, so its (unused)
    cap must not raise an alarm on its own."""
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"r{i}",) for i in range(99)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=100), deferred=0)
    snd.assert_not_called()


def test_quota_alert_silent_when_healthy(db, resend_on):
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=100), deferred=0)
    snd.assert_not_called()


def test_batch_bumps_daily_counters_per_provider(db, resend_on):
    # Mailjet's hourly quota (10) takes the first ten; the rest overflow to Resend.
    with patch("app.mail._call_mailjet_batch", return_value=200), \
         patch("app.mail._call_resend_batch", return_value=200):
        send_batch(db, _items(13), _cfg())
    def day_count(p):
        row = db.execute(
            "SELECT n FROM email_send_counts WHERE provider=? AND day=date('now')",
            (p,)).fetchone()
        return row["n"] if row else 0
    assert day_count("mailjet") == 10
    assert day_count("resend") == 3


# --- one bad recipient must not sink the batch ------------------------------

def _poisoned(bad_addresses, calls=None):
    """A provider that rejects any batch containing one of `bad_addresses`
    with 400 — which is exactly how Mailjet v3.1 treats a malformed recipient:
    the whole batch, not just the offending message."""
    bad = set(bad_addresses)
    def send(chunk):
        if calls is not None:
            calls.append([i.to for i in chunk])
        return 400 if any(i.to in bad for i in chunk) else 200
    return send


def test_one_bad_address_does_not_starve_the_rest_of_the_batch(db):
    """Regression: `subscriber@example-com` (a typo, no TLD) 400'd every batch it
    was in, and send_batch abandoned the provider outright — so any pending
    confirmation sharing that batch was deferred forever."""
    items = _items(5)
    items[2] = Outgoing(to="subscriber@example-com", subject="s", body="b",
                        idem_key="poison")
    with patch("app.mail._call_mailjet_batch", _poisoned(["subscriber@example-com"])):
        res = send_batch(db, items, _cfg(order=("mailjet",)))
    assert len(res.delivered) == 4            # everyone else got their mail
    assert res.undeliverable == {"poison"}
    assert res.deferred == 0
    assert _sent(db, "mailjet") == 4
    assert db.execute(
        "SELECT failures FROM email_failures WHERE email='subscriber@example-com'"
    ).fetchone()["failures"] == 1


def test_bisection_isolates_the_culprit_without_probing_every_address(db):
    calls = []
    items = _items(16)
    items[9] = Outgoing(to="bad@nope", subject="s", body="b", idem_key="poison")
    with patch("app.mail._call_mailjet_batch", _poisoned(["bad@nope"], calls)):
        res = send_batch(db, items, _cfg(order=("mailjet",),
                                         mailjet_hourly_quota=100))
    assert len(res.delivered) == 15 and res.undeliverable == {"poison"}
    # log2(16) splits ≈ 9 calls; a linear probe would be 16+.
    assert len(calls) < 16


def test_provider_outage_is_not_blamed_on_the_recipients(db, resend_on):
    """A 500 means the provider is down, not that 4 addresses went bad. Marking
    them would retire perfectly good subscribers."""
    with patch("app.mail._call_mailjet_batch", return_value=500), \
         patch("app.mail._call_resend_batch", return_value=200):
        res = send_batch(db, _items(4), _cfg())
    assert len(res.delivered) == 4
    assert res.undeliverable == set()
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0


def test_every_provider_failing_defers_rather_than_condemns(db, resend_on):
    with patch("app.mail._call_mailjet_batch", return_value=500), \
         patch("app.mail._call_resend_batch", return_value=503):
        res = send_batch(db, _items(3), _cfg())
    assert res.deferred == 3 and res.undeliverable == set()
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0
    # Claims released → a later cycle retries.
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency").fetchone()["n"] == 0


def test_address_is_retired_after_the_failure_cap(db):
    db.execute("INSERT INTO email_failures (email, failures) VALUES ('dead@nope', 3)")
    items = [Outgoing(to="dead@nope", subject="s", body="b", idem_key="d0")]
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        res = send_batch(db, items, _cfg(order=("mailjet",),
                                         max_send_failures_per_address=3))
    mb.assert_not_called()                    # no attempt, no quota spent
    assert res.undeliverable == {"d0"} and res.delivered == set()
    # Never claimed, so it can't block a future retry if the address recovers.
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency").fetchone()["n"] == 0


def test_under_the_cap_the_address_is_still_attempted(db):
    db.execute("INSERT INTO email_failures (email, failures) VALUES ('flaky@x.com', 2)")
    items = [Outgoing(to="flaky@x.com", subject="s", body="b", idem_key="f0")]
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        res = send_batch(db, items, _cfg(order=("mailjet",),
                                         max_send_failures_per_address=3))
    mb.assert_called_once()
    assert res.delivered == {"f0"}


def test_successful_delivery_clears_the_failure_history(db):
    """Transient 400s must not creep an otherwise-fine address up to the cap."""
    db.execute("INSERT INTO email_failures (email, failures) VALUES ('u0@x.com', 2)")
    with patch("app.mail._call_mailjet_batch", return_value=200):
        send_batch(db, _items(1), _cfg(order=("mailjet",)))
    assert db.execute(
        "SELECT COUNT(*) AS n FROM email_failures WHERE email='u0@x.com'"
    ).fetchone()["n"] == 0


def test_repeated_rejections_reach_the_cap_and_stop_costing_calls(db):
    items = [Outgoing(to="bad@nope", subject="s", body="b", idem_key=f"k{i}")
             for i in range(4)]
    cfg = _cfg(order=("mailjet",), max_send_failures_per_address=3)
    with patch("app.mail._call_mailjet_batch",
               _poisoned(["bad@nope"])) as _:
        for i in range(3):                    # three separate cycles
            send_batch(db, [items[i]], cfg)
    assert db.execute(
        "SELECT failures FROM email_failures WHERE email='bad@nope'"
    ).fetchone()["failures"] == 3
    with patch("app.mail._call_mailjet_batch") as mb:
        res = send_batch(db, [items[3]], cfg)
    mb.assert_not_called()
    assert res.undeliverable == {"k3"}


def test_a_rejection_at_one_provider_still_tries_the_other(db, resend_on):
    """Mailjet refusing a recipient doesn't make it undeliverable — Resend may
    well accept it. Only a rejection by every provider condemns an address."""
    items = _items(3)
    items[1] = Outgoing(to="odd@x.com", subject="s", body="b", idem_key="odd")
    with patch("app.mail._call_mailjet_batch", _poisoned(["odd@x.com"])), \
         patch("app.mail._call_resend_batch", return_value=200):
        res = send_batch(db, items, _cfg())
    assert len(res.delivered) == 3 and res.undeliverable == set()
    assert res.sent_by_provider == {"mailjet": 2, "resend": 1}
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0


def test_rejected_by_every_provider_is_condemned_once_not_twice(db, resend_on):
    items = [Outgoing(to="bad@nope", subject="s", body="b", idem_key="b0")]
    with patch("app.mail._call_mailjet_batch", _poisoned(["bad@nope"])), \
         patch("app.mail._call_resend_batch", _poisoned(["bad@nope"])):
        res = send_batch(db, items, _cfg())
    assert res.undeliverable == {"b0"} and res.delivered == set()
    # One failure for the address, not one per provider that refused it.
    assert db.execute(
        "SELECT failures FROM email_failures WHERE email='bad@nope'"
    ).fetchone()["failures"] == 1
