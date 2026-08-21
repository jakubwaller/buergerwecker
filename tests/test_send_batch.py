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


@pytest.fixture
def brevo_sweego_on(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-test")
    monkeypatch.setenv("SWEEGO_API_KEY", "sw_test")
    for k, v in {"MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "s",
                 "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x"}.items():
        monkeypatch.setenv(k, v)


def _cfg(order=("mailjet", "resend"), **over):
    base = dict(resend_daily_quota=100, mailjet_hourly_quota=10,
                mailjet_daily_quota=100_000,  # effectively unbounded unless set
                brevo_daily_quota=300, sweego_daily_quota=100,
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
    # 16 sends against a combined cap of 20 (10 + 10) = 80% → at threshold,
    # alert even with no deferral.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"r{i}",) for i in range(16)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=10, mailjet_daily_quota=10),
                          deferred=0)
    snd.assert_called_once()


def test_quota_alert_fires_when_mailjet_nears_its_cap(db):
    """Regression: the alert used to measure Resend only. Mailjet carries all
    the notification traffic and Resend just absorbs the overflow, so on
    2026-07-27 Mailjet sat at 197/200 while the alert read 0% and stayed
    silent. No RESEND_API_KEY here, so Mailjet IS the whole pool and 197/200
    is a genuine 98% of combined capacity."""
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
        [(f"m{i}",) for i in range(197)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(mailjet_daily_quota=200), deferred=0)
    snd.assert_called_once()
    body = snd.call_args.args[3]
    assert "mailjet: 197/200 (98%)" in body


def test_quota_alert_silent_while_the_overflow_provider_has_room(db, resend_on):
    """2026-08-19: mailjet 196/200 mailed "98%" while Resend's 100 sat
    untouched. Mailjet-first routing only spills once Mailjet is exhausted, so
    the primary filling up is an ordinary busy day, not an outage — 196 of a
    combined 300 is 65%, and nothing was deferred. Must stay quiet."""
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
        [(f"m{i}",) for i in range(196)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(mailjet_daily_quota=200, resend_daily_quota=100),
                          deferred=0)
    snd.assert_not_called()


def test_quota_alert_on_deferral_says_someone_missed_a_slot(db, resend_on):
    """The two conditions read differently: a deferral means a subscriber was
    not told, a threshold crossing does not. The old body claimed the former
    for both."""
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(), deferred=3)
    subject, body = snd.call_args.args[2], snd.call_args.args[3]
    assert "deferred" in subject
    assert "were not told about a slot" in body
    # The cycle count alone can't tell you what the day cost — the alert only
    # fires once per 24h — so the mail carries the UTC-day total beside it.
    assert "so far today" in body


def test_quota_alert_at_threshold_does_not_claim_anyone_missed_out(db, resend_on):
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'resend')",
        [(f"r{i}",) for i in range(16)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(resend_daily_quota=10, mailjet_daily_quota=10),
                          deferred=0)
    body = snd.call_args.args[3]
    assert "Nothing was deferred" in body
    assert "combined: 16/20 (80%)" in body


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


# --- Mailjet grades each message; believe it rather than retrying -----------

def _mj(verdicts, status=400):
    """A Mailjet-shaped ProviderResult: per-message verdicts, index-aligned."""
    from app.mail import ProviderResult
    return lambda chunk: ProviderResult(status, tuple(verdicts))


def test_partial_success_is_not_re_sent(db):
    """Mailjet processes the valid messages in a batch even when a sibling is
    rejected, so the successes are ALREADY delivered. Retrying them would send
    the same digest twice."""
    items = _items(4)
    items[1] = Outgoing(to="bad@nope", subject="s", body="b", idem_key="poison")
    calls = []
    def send(chunk):
        from app.mail import ProviderResult
        calls.append([i.idem_key for i in chunk])
        return ProviderResult(400, tuple(i.idem_key != "poison" for i in chunk))
    with patch("app.mail._call_mailjet_batch", send):
        res = send_batch(db, items, _cfg(order=("mailjet",)))
    assert len(calls) == 1                    # graded, so no bisection at all
    assert res.delivered == {"k0", "k2", "k3"}
    assert res.undeliverable == {"poison"}
    assert _sent(db, "mailjet") == 3


def test_per_message_errors_are_caught_even_on_http_200(db):
    """v3.1 can return 200 overall while individual messages errored. Trusting
    the HTTP status alone would mark a failed message as delivered and never
    retry it."""
    items = _items(2)
    with patch("app.mail._call_mailjet_batch", _mj([True, False], status=200)):
        res = send_batch(db, items, _cfg(order=("mailjet",)))
    assert res.delivered == {"k0"}
    assert res.undeliverable == {"k1"}


def test_falls_back_to_bisection_when_verdicts_are_unusable(db):
    """A body we can't line up with the request (wrong length, unparseable)
    must not be guessed at — fall back to isolating by bisection."""
    items = _items(4)
    items[2] = Outgoing(to="bad@nope", subject="s", body="b", idem_key="poison")
    calls = []
    with patch("app.mail._call_mailjet_batch", _poisoned(["bad@nope"], calls)):
        res = send_batch(db, items, _cfg(order=("mailjet",)))
    assert len(calls) > 1                     # bisected
    assert len(res.delivered) == 3 and res.undeliverable == {"poison"}


def test_graded_batch_where_everything_failed_is_not_a_provider_outage(db, resend_on):
    """All messages graded 'error' means the recipients are bad, not that
    Mailjet is down — so Resend gets a turn before anyone is condemned."""
    with patch("app.mail._call_mailjet_batch", _mj([False, False])), \
         patch("app.mail._call_resend_batch", return_value=200):
        res = send_batch(db, _items(2), _cfg())
    assert len(res.delivered) == 2 and res.undeliverable == set()
    assert res.sent_by_provider == {"resend": 2}


def test_deferral_is_recorded_per_utc_day(db, resend_on):
    """Deferral is the only trace that a subscriber was not told about a slot:
    the idempotency claim is deleted on the way out and the digest is never
    persisted. Without this counter the loss is invisible."""
    with patch("app.mail._call_mailjet_batch", return_value=200), \
         patch("app.mail._call_resend_batch", return_value=200):
        res = send_batch(db, _items(9), _cfg(mailjet_daily_quota=2,
                                             mailjet_hourly_quota=2,
                                             resend_daily_quota=3))
    assert res.deferred == 4                      # 9 staged, 2 + 3 sent
    from app.mail import deferrals_today
    assert deferrals_today(db) == 4


def test_deferral_counter_accumulates_across_cycles(db, resend_on):
    cfg = _cfg(mailjet_daily_quota=1, mailjet_hourly_quota=1, resend_daily_quota=0)
    with patch("app.mail._call_mailjet_batch", return_value=200), \
         patch("app.mail._call_resend_batch", return_value=200):
        send_batch(db, _items(3, "a"), cfg)       # 1 sent, 2 deferred
        send_batch(db, _items(3, "b"), cfg)       # quota spent, 3 deferred
    from app.mail import deferrals_today
    assert deferrals_today(db) == 5


# --- Brevo & Sweego: one message per call, same failover discipline ----------
# Neither API documents batch atomicity, so both are registered with
# batch_size=1 — a 4xx is attributable to exactly one recipient, and there is
# never a partially-delivered batch to mis-retry.

def test_order_routes_to_brevo_one_message_per_call(db, brevo_sweego_on):
    with patch("app.mail._call_brevo_batch", return_value=201) as bb, \
         patch("app.mail._call_sweego_batch") as sb, \
         patch("app.mail._call_mailjet_batch") as mb:
        res = send_batch(db, _items(3), _cfg(order=("brevo", "sweego")))
    assert len(res.delivered) == 3 and res.deferred == 0
    assert bb.call_count == 3                 # batch_size=1, never a real batch
    assert all(len(c.args[0]) == 1 for c in bb.call_args_list)
    sb.assert_not_called()
    mb.assert_not_called()
    assert _sent(db, "brevo") == 3


def test_brevo_and_sweego_skipped_without_api_keys(db, resend_on, monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    with patch("app.mail._call_brevo_batch") as bb, \
         patch("app.mail._call_sweego_batch") as sb, \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        res = send_batch(db, _items(2),
                         _cfg(order=("brevo", "sweego", "mailjet")))
    assert len(res.delivered) == 2
    bb.assert_not_called()
    sb.assert_not_called()
    mb.assert_called_once()


def test_brevo_overflow_spills_to_sweego(db, brevo_sweego_on):
    with patch("app.mail._call_brevo_batch", return_value=201), \
         patch("app.mail._call_sweego_batch", return_value=200):
        res = send_batch(db, _items(5), _cfg(order=("brevo", "sweego"),
                                             brevo_daily_quota=2))
    assert len(res.delivered) == 5 and res.deferred == 0
    assert _sent(db, "brevo") == 2 and _sent(db, "sweego") == 3


def test_existing_brevo_usage_counts_against_its_quota(db, brevo_sweego_on):
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'brevo')",
        [(f"old{i}",) for i in range(4)])
    with patch("app.mail._call_brevo_batch", return_value=201):
        res = send_batch(db, _items(3), _cfg(order=("brevo",),
                                             brevo_daily_quota=5))
    assert len(res.delivered) == 1 and res.deferred == 2


def test_brevo_402_is_an_outage_not_a_bad_recipient(db, brevo_sweego_on):
    """Brevo answers 402 when the account is out of credits. That is a provider
    problem — fall through to the next provider, condemn nobody."""
    with patch("app.mail._call_brevo_batch", return_value=402) as bb, \
         patch("app.mail._call_sweego_batch", return_value=200):
        res = send_batch(db, _items(3), _cfg(order=("brevo", "sweego")))
    assert len(res.delivered) == 3 and res.undeliverable == set()
    assert bb.call_count == 1                 # first 402 abandons the provider
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0


def test_sweego_rejection_feeds_the_failure_accounting(db, brevo_sweego_on):
    items = _items(3)
    items[1] = Outgoing(to="subscriber@example-com", subject="s", body="b",
                        idem_key="poison")
    with patch("app.mail._call_sweego_batch",
               _poisoned(["subscriber@example-com"])):
        res = send_batch(db, items, _cfg(order=("sweego",)))
    assert len(res.delivered) == 2
    assert res.undeliverable == {"poison"}
    assert db.execute(
        "SELECT failures FROM email_failures WHERE email='subscriber@example-com'"
    ).fetchone()["failures"] == 1


def test_sweego_422_is_attributed_to_the_single_recipient(db, brevo_sweego_on):
    """Sweego's documented validation error is 422; with one message per call
    it points at exactly one address."""
    with patch("app.mail._call_sweego_batch", return_value=422):
        res = send_batch(db, _items(1), _cfg(order=("sweego",)))
    assert res.undeliverable == {"k0"} and res.delivered == set()
    assert db.execute(
        "SELECT failures FROM email_failures WHERE email='u0@x.com'"
    ).fetchone()["failures"] == 1


def test_systemic_rejection_streak_defers_rather_than_retires(db, brevo_sweego_on):
    """A misconfigured provider (bad payload shape, unverified sender) answers
    400/422 to EVERY call — at batch_size=1 indistinguishable per-call from a
    bad recipient. A provider that rejects a whole cycle while delivering
    nothing is treated as unusable: the mail defers to the next cycle instead
    of striking every address, which at the failure cap would silently retire
    them all within three cycles."""
    with patch("app.mail._call_sweego_batch", return_value=422):
        res = send_batch(db, _items(4), _cfg(order=("sweego",)))
    assert res.deferred == 4 and res.undeliverable == set()
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0
    # Claims released so the next cycle (or a fixed provider) can retry.
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency").fetchone()["n"] == 0


def test_batch_adapters_reach_the_wire_with_the_single_send_payload(
        db, brevo_sweego_on, monkeypatch):
    """Route send_batch through the REAL _call_brevo_batch/_call_sweego_batch
    bodies — only requests.post is faked. Guards the adapter glue (the
    batch_size=1 unpack, the positional unsub_url pass-through, the
    ProviderResult wrap) that every other batch test mocks away."""
    posted = []
    def fake_post(url, **kwargs):
        posted.append((url, kwargs.get("json")))
        r = MagicMock()
        r.status_code = 201
        return r
    item = Outgoing(to="u0@x.com", subject="s", body="b", idem_key="w0",
                    unsub_url="https://x/unsubscribe/tok")
    with patch("app.mail.requests.post", side_effect=fake_post):
        res = send_batch(db, [item], _cfg(order=("brevo",)))
    assert res.delivered == {"w0"} and _sent(db, "brevo") == 1
    url, payload = posted[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert payload["to"] == [{"email": "u0@x.com"}]
    assert payload["headers"]["List-Unsubscribe"] == "<https://x/unsubscribe/tok>"
    posted.clear()
    monkeypatch.setenv("REPLY_TO_EMAIL", "termine@jakubwaller.eu")
    item = Outgoing(to="u1@x.com", subject="s", body="b", idem_key="w1",
                    unsub_url="https://x/unsubscribe/tok")
    with patch("app.mail.requests.post", side_effect=fake_post):
        res = send_batch(db, [item], _cfg(order=("sweego",)))
    assert res.delivered == {"w1"} and _sent(db, "sweego") == 1
    url, payload = posted[0]
    assert url == "https://api.sweego.io/send"
    assert payload["recipients"] == [{"email": "u1@x.com"}]
    assert payload["message-txt"] == "b"
    # Sweego's mailto+url form — the URL-only header the others send is 422'd.
    assert payload["headers"]["List-Unsubscribe"] == (
        "<mailto:termine@jakubwaller.eu?subject=abmelden>,"
        "<https://x/unsubscribe/tok>")


def test_a_rejection_at_brevo_still_tries_sweego(db, brevo_sweego_on):
    items = _items(3)
    items[1] = Outgoing(to="odd@x.com", subject="s", body="b", idem_key="odd")
    with patch("app.mail._call_brevo_batch", _poisoned(["odd@x.com"])), \
         patch("app.mail._call_sweego_batch", return_value=200):
        res = send_batch(db, items, _cfg(order=("brevo", "sweego")))
    assert len(res.delivered) == 3 and res.undeliverable == set()
    assert res.sent_by_provider == {"brevo": 2, "sweego": 1}
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0


def test_brevo_and_sweego_both_down_defers_rather_than_condemns(db, brevo_sweego_on):
    with patch("app.mail._call_brevo_batch", return_value=500), \
         patch("app.mail._call_sweego_batch", side_effect=OSError("timeout")):
        res = send_batch(db, _items(3), _cfg(order=("brevo", "sweego")))
    assert res.deferred == 3 and res.undeliverable == set()
    assert db.execute("SELECT COUNT(*) AS n FROM email_failures").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM sent_idempotency").fetchone()["n"] == 0


def test_batch_bumps_daily_counters_for_new_providers(db, brevo_sweego_on):
    with patch("app.mail._call_brevo_batch", return_value=201), \
         patch("app.mail._call_sweego_batch", return_value=200):
        send_batch(db, _items(4), _cfg(order=("brevo", "sweego"),
                                       brevo_daily_quota=3))
    def day_count(p):
        row = db.execute(
            "SELECT n FROM email_send_counts WHERE provider=? AND day=date('now')",
            (p,)).fetchone()
        return row["n"] if row else 0
    assert day_count("brevo") == 3
    assert day_count("sweego") == 1


def test_quota_alert_pool_includes_new_providers_when_configured(db, brevo_sweego_on):
    # 16 sends against a combined cap of 20 (mailjet 5 + brevo 10 + sweego 5)
    # = 80% → the alert fires even with no deferral. Guards the _daily_usage
    # caps dict: a provider missing from it silently drops out of the pool.
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'brevo')",
        [(f"b{i}",) for i in range(16)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(order=("mailjet", "brevo", "sweego"),
                                   mailjet_daily_quota=5, brevo_daily_quota=10,
                                   sweego_daily_quota=5),
                          deferred=0)
    snd.assert_called_once()
    body = snd.call_args.args[3]
    assert "brevo: 16/10" in body


def test_quota_alert_ignores_new_providers_without_keys(db, monkeypatch):
    """No BREVO/SWEEGO API keys → they are not in the send path, so their
    (unused) caps must not join the pool or raise an alarm on their own."""
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    db.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'brevo')",
        [(f"b{i}",) for i in range(99)])
    with patch("app.mail.send") as snd:
        maybe_quota_alert(db, _cfg(order=("mailjet", "brevo", "sweego"),
                                   brevo_daily_quota=100, sweego_daily_quota=100),
                          deferred=0)
    snd.assert_not_called()
