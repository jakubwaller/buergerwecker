"""Delivery-feedback webhooks: parsing, authentication, and what an event does.

The point of these is that the send path cannot see an asynchronous failure. A
provider returns 200, the message is recorded as sent, and only the webhook
ever says the mailbox does not exist or that the recipient pressed "spam".
"""
import base64
import hashlib
import hmac
import json
from datetime import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.db import connect, init_schema
from app.models import Filter
from app.repo import (insert_pending, is_suppressed, suppress_address,
                      suppressed_addresses)
from app.webhooks import (COMPLAINT, DELIVERED, HARD_BOUNCE, IGNORE,
                          SOFT_BOUNCE, UNSUBSCRIBE, apply_events, check_secret,
                          parse_brevo, parse_mailjet, parse_sweego,
                          verify_sweego_signature)

SECRET = "s" * 32


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _sub(conn, email, city="leipzig"):
    f = Filter(appointment_types=["A"], locations="all",
               weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    return insert_pending(conn, email=email, city=city, language="de",
                          filter_=f, ttl_days=90)


def _live(conn, email):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM subscriptions "
        "WHERE email=? AND deleted_at IS NULL", (email,)).fetchone()["n"]


def _kinds(events):
    return [e.kind for e in events]


# --------------------------------------------------------------------------
# Mailjet
# --------------------------------------------------------------------------

def test_mailjet_parses_a_grouped_array():
    # Mailjet batches "all the events of the last second for the same webhook
    # URL" into one array, so the endpoint must handle a list as readily as a
    # bare object.
    events = parse_mailjet([
        {"event": "bounce", "email": "a@example.com", "hard_bounce": True},
        {"event": "spam", "email": "b@example.com"},
    ])
    assert [(e.email, e.kind) for e in events] == [
        ("a@example.com", HARD_BOUNCE), ("b@example.com", COMPLAINT)]


def test_mailjet_single_object_is_accepted_too():
    assert _kinds(parse_mailjet(
        {"event": "unsub", "email": "a@example.com"})) == [UNSUBSCRIBE]


def test_mailjet_soft_bounce_is_not_permanent():
    assert _kinds(parse_mailjet({"event": "bounce", "email": "a@example.com",
                                 "hard_bounce": False})) == [SOFT_BOUNCE]


def test_mailjet_bounce_that_blocklists_the_address_is_permanent():
    # `blocked: true` means Mailjet has put the address on its own blocklist,
    # so every later send to it is a guaranteed failure regardless of what the
    # hard_bounce flag says.
    assert _kinds(parse_mailjet({"event": "bounce", "email": "a@example.com",
                                 "hard_bounce": False,
                                 "blocked": True})) == [HARD_BOUNCE]


def test_mailjet_blocked_only_retires_the_address_when_the_address_is_at_fault():
    # This is the one that matters: a `blocked` for our own content or a
    # provider-side fault says nothing about the recipient, and suppressing on
    # it would retire good subscribers over our mistake.
    addressed = parse_mailjet({"event": "blocked", "email": "a@example.com",
                               "error_related_to": "recipient"})
    ours = parse_mailjet({"event": "blocked", "email": "b@example.com",
                          "error_related_to": "content"})
    system = parse_mailjet({"event": "blocked", "email": "c@example.com",
                            "error_related_to": "system"})
    assert _kinds(addressed) == [HARD_BOUNCE]
    assert _kinds(ours) == [SOFT_BOUNCE]
    assert _kinds(system) == [SOFT_BOUNCE]


def test_mailjet_sent_counts_as_delivered():
    # Mailjet's "sent" is acceptance by the recipient's mail server, not by
    # Mailjet, so it is genuine evidence the mailbox works.
    assert _kinds(parse_mailjet(
        {"event": "sent", "email": "a@example.com"})) == [DELIVERED]


def test_mailjet_opens_and_clicks_are_ignored():
    assert _kinds(parse_mailjet([{"event": "open", "email": "a@example.com"},
                                 {"event": "click", "email": "a@example.com"}])
                  ) == [IGNORE, IGNORE]


def test_mailjet_events_without_an_address_are_dropped():
    assert parse_mailjet([{"event": "bounce"},
                          {"event": "bounce", "email": ""},
                          {"event": "bounce", "email": None},
                          "not-a-dict"]) == []


def test_mailjet_error_text_is_captured_for_the_admin_page():
    ev = parse_mailjet({"event": "bounce", "email": "a@example.com",
                        "hard_bounce": True, "error": "user unknown"})[0]
    assert "user unknown" in ev.detail


# --------------------------------------------------------------------------
# Brevo
# --------------------------------------------------------------------------

@pytest.mark.parametrize("event,kind", [
    ("hard_bounce", HARD_BOUNCE),
    ("invalid_email", HARD_BOUNCE),
    ("blocked", HARD_BOUNCE),
    ("spam", COMPLAINT),
    ("unsubscribed", UNSUBSCRIBE),
    ("soft_bounce", SOFT_BOUNCE),
    ("error", SOFT_BOUNCE),
    ("delivered", DELIVERED),
    ("opened", IGNORE),
    ("click", IGNORE),
    ("deferred", IGNORE),
    ("request", IGNORE),
])
def test_brevo_event_mapping(event, kind):
    assert _kinds(parse_brevo({"event": event,
                               "email": "a@example.com"})) == [kind]


def test_brevo_reason_is_captured():
    ev = parse_brevo({"event": "hard_bounce", "email": "a@example.com",
                      "reason": "unknown user"})[0]
    assert ev.detail == "unknown user" and ev.provider == "brevo"


# --------------------------------------------------------------------------
# Sweego
# --------------------------------------------------------------------------

def test_sweego_reads_the_recipient_field():
    ev = parse_sweego({"event_type": "hard_bounce",
                       "recipient": "a@example.com"})[0]
    assert ev.email == "a@example.com" and ev.kind == HARD_BOUNCE


@pytest.mark.parametrize("event_type,kind", [
    # Sweego's own payload docs spell these inconsistently — `soft-bounce`
    # with a hyphen next to `hard_bounce` with an underscore — so both
    # separators have to work for both events.
    ("soft-bounce", SOFT_BOUNCE),
    ("soft_bounce", SOFT_BOUNCE),
    ("hard_bounce", HARD_BOUNCE),
    ("hard-bounce", HARD_BOUNCE),
    ("complaint", COMPLAINT),
    ("list_unsub", UNSUBSCRIBE),
    ("delivered", DELIVERED),
    ("email_sent", IGNORE),
    ("email_opened", IGNORE),
])
def test_sweego_event_mapping(event_type, kind):
    assert _kinds(parse_sweego({"event_type": event_type,
                                "recipient": "a@example.com"})) == [kind]


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

def test_check_secret_rejects_wrong_and_empty():
    assert check_secret(SECRET, SECRET)
    assert not check_secret("wrong" * 8, SECRET)
    assert not check_secret("", SECRET)
    # An unconfigured expectation must never let everything through.
    assert not check_secret(SECRET, "")
    assert not check_secret("", "")


def _sweego_sign(body: bytes, secret_b64: str, wid="id1", ts="1769696506"):
    key = base64.b64decode(secret_b64)
    signed = f"{wid}.{ts}.".encode() + body
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def test_sweego_signature_round_trip():
    secret = base64.b64encode(b"k" * 32).decode()
    body = b'{"event_type":"hard_bounce","recipient":"a@example.com"}'
    sig = _sweego_sign(body, secret)
    assert verify_sweego_signature(webhook_id="id1", timestamp="1769696506",
                                   signature=sig, body=body, secret=secret)


def test_sweego_signature_rejects_a_tampered_body():
    secret = base64.b64encode(b"k" * 32).decode()
    body = b'{"event_type":"hard_bounce","recipient":"a@example.com"}'
    sig = _sweego_sign(body, secret)
    tampered = body.replace(b"a@example.com", b"victim@example.com")
    assert not verify_sweego_signature(webhook_id="id1",
                                       timestamp="1769696506", signature=sig,
                                       body=tampered, secret=secret)


def test_sweego_signature_is_bound_to_id_and_timestamp():
    # The signed string is `{id}.{timestamp}.{body}`, so replaying a valid
    # signature under a different id or timestamp must fail.
    secret = base64.b64encode(b"k" * 32).decode()
    body = b'{"event_type":"delivered","recipient":"a@example.com"}'
    sig = _sweego_sign(body, secret)
    assert not verify_sweego_signature(webhook_id="other", timestamp="1769696506",
                                       signature=sig, body=body, secret=secret)
    assert not verify_sweego_signature(webhook_id="id1", timestamp="1",
                                       signature=sig, body=body, secret=secret)


def test_sweego_signature_rejects_missing_pieces_and_bad_secret():
    secret = base64.b64encode(b"k" * 32).decode()
    body = b"{}"
    sig = _sweego_sign(body, secret)
    assert not verify_sweego_signature(webhook_id="", timestamp="1769696506",
                                       signature=sig, body=body, secret=secret)
    assert not verify_sweego_signature(webhook_id="id1", timestamp="1769696506",
                                       signature="", body=body, secret=secret)
    assert not verify_sweego_signature(webhook_id="id1", timestamp="1769696506",
                                       signature=sig, body=body, secret="")
    # A secret that is not valid base64 must fail closed, not raise.
    assert not verify_sweego_signature(webhook_id="id1", timestamp="1769696506",
                                       signature=sig, body=body, secret="!!!!")


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------

def test_hard_bounce_suppresses_and_ends_every_subscription_for_that_address(db):
    # One person may hold several subscriptions; a dead mailbox is a verdict on
    # the address, not on one row.
    _sub(db, "gone@example.com", city="leipzig")
    _sub(db, "gone@example.com", city="dresden")
    _sub(db, "fine@example.com")
    res = apply_events(db, parse_brevo({"event": "hard_bounce",
                                        "email": "gone@example.com"}))
    assert res.suppressed == 1 and res.unsubscribed == 2
    assert is_suppressed(db, "gone@example.com")
    assert _live(db, "gone@example.com") == 0
    assert _live(db, "fine@example.com") == 1
    assert not is_suppressed(db, "fine@example.com")


def test_complaint_suppresses_and_ends_the_subscription(db):
    _sub(db, "angry@example.com")
    res = apply_events(db, parse_mailjet({"event": "spam",
                                          "email": "angry@example.com"}))
    assert res.suppressed == 1 and res.unsubscribed == 1
    assert _live(db, "angry@example.com") == 0
    row = db.execute("SELECT reason FROM email_suppressions WHERE email=?",
                     ("angry@example.com",)).fetchone()
    assert row["reason"] == COMPLAINT


def test_unsubscribe_ends_the_subscription_without_suppressing_the_address(db):
    # Unsubscribing is the person's own decision. Suppressing the address would
    # quietly make signing up again impossible.
    _sub(db, "bye@example.com")
    res = apply_events(db, parse_brevo({"event": "unsubscribed",
                                        "email": "bye@example.com"}))
    assert res.unsubscribed == 1 and res.suppressed == 0
    assert _live(db, "bye@example.com") == 0
    assert not is_suppressed(db, "bye@example.com")


def test_soft_bounces_only_suppress_after_a_run(db):
    _sub(db, "full@example.com")
    ev = parse_brevo({"event": "soft_bounce", "email": "full@example.com"})
    for _ in range(2):
        apply_events(db, ev, soft_bounce_threshold=3)
    assert not is_suppressed(db, "full@example.com")
    assert _live(db, "full@example.com") == 1
    res = apply_events(db, ev, soft_bounce_threshold=3)
    assert res.suppressed == 1
    assert is_suppressed(db, "full@example.com")
    assert _live(db, "full@example.com") == 0


def test_soft_bounce_threshold_zero_counts_without_ever_suppressing(db):
    ev = parse_brevo({"event": "soft_bounce", "email": "full@example.com"})
    for _ in range(20):
        apply_events(db, ev, soft_bounce_threshold=0)
    assert not is_suppressed(db, "full@example.com")
    assert db.execute("SELECT soft_bounces FROM email_suppressions WHERE email=?",
                      ("full@example.com",)).fetchone()["soft_bounces"] == 20


def test_a_delivery_clears_a_soft_bounce_run(db):
    ev = parse_brevo({"event": "soft_bounce", "email": "flaky@example.com"})
    apply_events(db, ev, soft_bounce_threshold=3)
    apply_events(db, ev, soft_bounce_threshold=3)
    apply_events(db, parse_brevo({"event": "delivered",
                                  "email": "flaky@example.com"}))
    assert db.execute("SELECT soft_bounces FROM email_suppressions WHERE email=?",
                      ("flaky@example.com",)).fetchone()["soft_bounces"] == 0
    # ...so the counter starts over rather than creeping to the cap over months.
    for _ in range(2):
        apply_events(db, ev, soft_bounce_threshold=3)
    assert not is_suppressed(db, "flaky@example.com")


def test_a_later_delivery_does_not_lift_a_hard_suppression(db):
    # A complaint is not undone by a message reaching the mailbox afterwards,
    # and the provider will keep reporting deliveries for other traffic.
    _sub(db, "angry@example.com")
    apply_events(db, parse_mailjet({"event": "spam",
                                    "email": "angry@example.com"}))
    apply_events(db, parse_brevo({"event": "delivered",
                                  "email": "angry@example.com"}))
    assert is_suppressed(db, "angry@example.com")


def test_repeated_suppression_keeps_the_first_reason(db):
    # Providers retry webhooks and a dead mailbox reports from several of them.
    # Re-suppressing must not rewrite why we stopped or when.
    apply_events(db, parse_brevo({"event": "hard_bounce",
                                  "email": "gone@example.com"}))
    first = db.execute("SELECT reason, suppressed_at FROM email_suppressions "
                       "WHERE email=?", ("gone@example.com",)).fetchone()
    apply_events(db, parse_mailjet({"event": "spam", "email": "gone@example.com"}))
    again = db.execute("SELECT reason, suppressed_at FROM email_suppressions "
                       "WHERE email=?", ("gone@example.com",)).fetchone()
    assert again["reason"] == first["reason"] == HARD_BOUNCE
    assert again["suppressed_at"] == first["suppressed_at"]


def test_ignored_events_change_nothing(db):
    _sub(db, "reader@example.com")
    res = apply_events(db, parse_mailjet({"event": "open",
                                          "email": "reader@example.com"}))
    assert res.ignored == 1 and res.suppressed == 0
    assert _live(db, "reader@example.com") == 1
    assert db.execute("SELECT COUNT(*) AS n FROM email_suppressions"
                      ).fetchone()["n"] == 0


def test_empty_event_list_is_a_no_op(db):
    assert apply_events(db, []).suppressed == 0


# --------------------------------------------------------------------------
# The send path has to honour the suppression list
# --------------------------------------------------------------------------

def test_send_batch_never_mails_a_suppressed_address(db):
    from app.mail import Outgoing, send_batch

    suppress_address(db, "gone@example.com", reason=HARD_BOUNCE,
                     provider="brevo")
    cfg = SimpleNamespace(mailjet_hourly_quota=10, mailjet_daily_quota=1000,
                          brevo_daily_quota=300, sweego_daily_quota=100,
                          email_provider_order=("mailjet",),
                          max_send_failures_per_address=3)
    items = [Outgoing(to="gone@example.com", subject="s", body="b", idem_key="k1"),
             Outgoing(to="ok@example.com", subject="s", body="b", idem_key="k2")]
    with patch("app.mail._call_mailjet_batch", return_value=200) as mb:
        res = send_batch(db, items, cfg)
    assert res.delivered == {"k2"}
    assert res.undeliverable == {"k1"}
    # The suppressed recipient must not even reach the provider payload.
    assert [o.to for o in mb.call_args[0][0]] == ["ok@example.com"]


def test_dead_addresses_is_the_union_of_both_failure_halves(db):
    from app.mail import _dead_addresses

    suppress_address(db, "bounced@example.com", reason=HARD_BOUNCE)
    db.execute("INSERT INTO email_failures (email, failures) VALUES (?, 3)",
               ("rejected@example.com",))
    cfg = SimpleNamespace(max_send_failures_per_address=3)
    assert _dead_addresses(db, cfg) == {"bounced@example.com",
                                        "rejected@example.com"}
    # Disabling the synchronous cap must not disable the suppression list —
    # they are separate mechanisms with separate evidence.
    assert _dead_addresses(db, SimpleNamespace(
        max_send_failures_per_address=0)) == {"bounced@example.com"}


def test_suppressed_addresses_ignores_soft_bounce_watchlist_rows(db):
    from app.repo import record_soft_bounce
    record_soft_bounce(db, "flaky@example.com", threshold=5)
    assert suppressed_addresses(db) == set()


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

def test_bounce_suppressions_die_with_the_subscription_that_justified_them(db):
    # A bounce only claims the mailbox does not exist *today*. Domains get
    # fixed and typos get corrected, so the claim goes stale, and the row is a
    # bare address that must not outlive the privacy policy's 30-day promise.
    from app.housekeeping import _prune_suppressions

    sid = _sub(db, "gone@example.com")
    apply_events(db, parse_brevo({"event": "hard_bounce",
                                  "email": "gone@example.com"}))
    # While the (soft-deleted) subscription still exists, the suppression must
    # survive — otherwise the address is mailable again the same night.
    _prune_suppressions(db)
    assert is_suppressed(db, "gone@example.com")
    db.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
    _prune_suppressions(db)
    assert not is_suppressed(db, "gone@example.com")


def test_complaints_are_never_pruned(db):
    # Someone telling their provider we are spam does not expire, and
    # re-mailing them is the worst thing this service can do to its sending
    # domain.
    from app.housekeeping import _prune_suppressions

    sid = _sub(db, "angry@example.com")
    apply_events(db, parse_mailjet({"event": "spam",
                                    "email": "angry@example.com"}))
    db.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
    _prune_suppressions(db)
    assert is_suppressed(db, "angry@example.com")


def test_a_complaint_for_an_already_purged_subscription_survives(db):
    # Feedback loops report late, so a complaint can arrive for an address that
    # has no subscription row at all. Under the plain NOT EXISTS rule the one
    # suppression that must never be lost was the one lost within 24h.
    from app.housekeeping import _prune_suppressions

    apply_events(db, parse_brevo({"event": "spam", "email": "late@example.com"}))
    _prune_suppressions(db)
    assert is_suppressed(db, "late@example.com")


def test_a_soft_bounce_watchlist_row_is_still_pruned(db):
    # Only complaints are permanent; a half-finished soft-bounce count is a
    # bare address with no subscription behind it.
    from app.housekeeping import _prune_suppressions
    from app.repo import record_soft_bounce

    record_soft_bounce(db, "flaky@example.com", threshold=5)
    _prune_suppressions(db)
    assert db.execute("SELECT COUNT(*) AS n FROM email_suppressions"
                      ).fetchone()["n"] == 0


# --------------------------------------------------------------------------
# The HTTP endpoint
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.web import create_app

    db_path = str(tmp_path / "t.db")
    for k, v in {
        "DB_PATH": db_path, "TOKEN_SECRET_PRIMARY": "x" * 32,
        "TOKEN_SECRET_PREVIOUS": "", "SUBSCRIPTION_TTL_DAYS": "90",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
        "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m",
        "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x",
        "MAILJET_DAILY_QUOTA": "6000", "ADMIN_TOKEN": "a" * 32,
        "PUBLIC_BASE_URL": "https://x", "DEDUP_WINDOW_HOURS": "24",
        "RATE_LIMIT_MINUTES": "15", "RENEWAL_REMINDER_DAYS_BEFORE": "10",
        "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
        "DEVELOPER_EMAIL": "d@x", "KOFI_URL": "https://k",
        "WEBHOOK_SECRET": SECRET,
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path)
    init_schema(conn)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), conn


def _post(c, provider, secret, payload, **kw):
    return c.post(f"/webhooks/{provider}/{secret}",
                  data=json.dumps(payload),
                  content_type="application/json", **kw)


def test_webhook_applies_a_hard_bounce_end_to_end(client):
    c, conn = client
    _sub(conn, "gone@example.com")
    r = _post(c, "brevo", SECRET,
              {"event": "hard_bounce", "email": "gone@example.com"})
    assert r.status_code == 200
    assert is_suppressed(conn, "gone@example.com")
    assert _live(conn, "gone@example.com") == 0


def test_webhook_rejects_a_wrong_secret_and_changes_nothing(client):
    c, conn = client
    _sub(conn, "gone@example.com")
    r = _post(c, "brevo", "w" * 32,
              {"event": "hard_bounce", "email": "gone@example.com"})
    assert r.status_code == 403
    assert not is_suppressed(conn, "gone@example.com")
    assert _live(conn, "gone@example.com") == 1


def test_webhook_404s_an_unknown_provider(client):
    c, _ = client
    assert _post(c, "sendgrid", SECRET, {}).status_code == 404


def test_webhook_says_so_when_it_is_not_configured(tmp_path, monkeypatch,
                                                   client):
    # A silent 404 here is indistinguishable from a typo'd URL in a provider
    # dashboard, which is exactly the confusion that leaves a feedback loop
    # switched off for weeks.
    c, _ = client
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    from app.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    r = app.test_client().post(f"/webhooks/brevo/{SECRET}", json={})
    assert r.status_code == 503


def test_webhook_stamps_when_a_provider_last_reported(client):
    c, conn = client
    _post(c, "mailjet", SECRET, {"event": "open", "email": "a@example.com"})
    row = conn.execute("SELECT value FROM meta WHERE key=?",
                       ("last_webhook_at_mailjet",)).fetchone()
    assert row and row["value"]


def test_unreadable_payload_is_counted_not_rejected(client):
    # Returning an error would make the provider retry a body that can never
    # parse, and eventually disable the endpoint. The count is what surfaces a
    # parser gone stale against a format change.
    c, conn = client
    r = c.post(f"/webhooks/brevo/{SECRET}", data=b"<html>nope</html>",
               content_type="application/json")
    assert r.status_code == 200
    assert conn.execute("SELECT value FROM meta WHERE key=?",
                        ("webhook_errors_brevo",)).fetchone()["value"] == "1"


def test_sweego_signature_is_enforced_when_a_signing_secret_is_set(client,
                                                                   monkeypatch):
    c, conn = client
    signing = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setenv("SWEEGO_WEBHOOK_SECRET", signing)
    from app.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    cl = app.test_client()
    _sub(conn, "gone@example.com")

    body = json.dumps({"event_type": "hard_bounce",
                       "recipient": "gone@example.com"}).encode()
    unsigned = cl.post(f"/webhooks/sweego/{SECRET}", data=body,
                       content_type="application/json")
    assert unsigned.status_code == 403
    assert _live(conn, "gone@example.com") == 1

    headers = {"webhook-id": "id1", "webhook-timestamp": "1769696506",
               "webhook-signature": _sweego_sign(body, signing)}
    signed = cl.post(f"/webhooks/sweego/{SECRET}", data=body, headers=headers,
                     content_type="application/json")
    assert signed.status_code == 200
    assert is_suppressed(conn, "gone@example.com")


def test_sweego_signature_is_verified_against_the_raw_body(client, monkeypatch):
    # Flask's JSON round-trip changes whitespace and key order, so a
    # re-serialised body would not match. Sign a body with non-canonical
    # spacing and it must still verify.
    c, conn = client
    signing = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setenv("SWEEGO_WEBHOOK_SECRET", signing)
    from app.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    body = b'{ "recipient" : "gone@example.com" ,  "event_type" : "complaint" }'
    headers = {"webhook-id": "id1", "webhook-timestamp": "1769696506",
               "webhook-signature": _sweego_sign(body, signing)}
    r = app.test_client().post(f"/webhooks/sweego/{SECRET}", data=body,
                               headers=headers,
                               content_type="application/json")
    assert r.status_code == 200
    assert is_suppressed(conn, "gone@example.com")


# --------------------------------------------------------------------------
# Surfacing: /admin and the ops-summary anomalies
#
# A feedback loop nobody looks at is only marginally better than none. These
# guard the two places the numbers appear.
# --------------------------------------------------------------------------

def test_admin_page_shows_the_deliverability_section(client):
    c, conn = client
    _sub(conn, "gone@example.com")
    _post(c, "brevo", SECRET, {"event": "hard_bounce",
                               "email": "gone@example.com"})
    r = c.get("/admin?token=" + "a" * 32)
    assert r.status_code == 200
    body = r.data.decode()
    assert "Deliverability" in body
    assert "complaint rate" in body and "hard bounce rate" in body
    # The address itself must never be rendered on the page.
    assert "gone@example.com" not in body


def test_stats_reports_rates_against_what_was_actually_sent(db):
    from app.admin import _deliverability

    db.execute("INSERT INTO email_send_counts (provider, day, n) "
               "VALUES ('mailjet', date('now'), 1000)")
    apply_events(db, parse_mailjet({"event": "spam", "email": "a@example.com"}))
    apply_events(db, parse_brevo({"event": "hard_bounce",
                                  "email": "b@example.com"}))
    apply_events(db, parse_brevo({"event": "hard_bounce",
                                  "email": "c@example.com"}))
    d = _deliverability(db, SimpleNamespace(webhook_secret=SECRET,
                                            email_provider_order=("mailjet",)))
    assert d["sent_30d"] == 1000
    assert d["complaint_rate"] == pytest.approx(0.1)
    assert d["bounce_rate"] == pytest.approx(0.2)
    assert d["suppressed"] == 3


def test_anomaly_fires_on_a_complaint_rate_above_the_gmail_line():
    from app.admin import summary_anomalies
    from datetime import datetime

    lines = summary_anomalies({"deliverability": {
        "configured": True, "sent_30d": 10_000, "complaint_30d": 40,
        "complaint_rate": 0.4, "hard_bounce_30d": 10, "bounce_rate": 0.1,
        "providers_silent": []}}, now=datetime(2026, 8, 26, 12))
    assert any("spam-complaint rate 0.40%" in ln for ln in lines)
    assert not any("hard-bounce rate" in ln for ln in lines)


def test_a_tiny_sample_does_not_raise_a_reputation_alarm():
    # One complaint out of 50 mails is 2%, which is arithmetic, not a
    # reputation emergency. Crying wolf here trains the reader to skip the line.
    from app.admin import summary_anomalies
    from datetime import datetime

    lines = summary_anomalies({"deliverability": {
        "configured": True, "sent_30d": 50, "complaint_30d": 1,
        "complaint_rate": 2.0, "hard_bounce_30d": 0, "bounce_rate": 0.0,
        "providers_silent": []}}, now=datetime(2026, 8, 26, 12))
    assert not any("complaint" in ln for ln in lines)


def test_anomaly_fires_when_a_provider_stops_reporting():
    # 0.00% with a dead webhook and 0.00% with a healthy one look identical in
    # the numbers. This line is the difference.
    from app.admin import summary_anomalies
    from datetime import datetime

    lines = summary_anomalies({"deliverability": {
        "configured": True, "sent_30d": 10_000, "complaint_30d": 0,
        "complaint_rate": 0.0, "hard_bounce_30d": 0, "bounce_rate": 0.0,
        "providers_silent": ["brevo"]}}, now=datetime(2026, 8, 26, 12))
    assert any("no delivery feedback from brevo" in ln for ln in lines)


def test_anomaly_fires_when_webhooks_are_not_configured_at_all():
    from app.admin import summary_anomalies
    from datetime import datetime

    lines = summary_anomalies(
        {"deliverability": {"configured": False, "providers_silent": []}},
        now=datetime(2026, 8, 26, 12))
    assert any("not configured" in ln for ln in lines)


def test_a_provider_that_reported_recently_is_not_flagged_silent(db):
    from app.admin import _deliverability

    db.execute("INSERT INTO meta (key, value) "
               "VALUES ('last_webhook_at_mailjet', CURRENT_TIMESTAMP)")
    d = _deliverability(db, SimpleNamespace(webhook_secret=SECRET,
                                            email_provider_order=("mailjet",)))
    assert d["providers_silent"] == []
    assert d["providers"][0]["silent"] is False


def test_an_idle_provider_leg_is_not_reported_as_a_broken_webhook(db):
    # Sweego sits at the end of the fallback chain and often sends nothing for
    # days. "No feedback" from a leg that sent no mail is not evidence of
    # anything, and flagging it daily teaches the reader to skip the section.
    from app.admin import _deliverability

    cfg = SimpleNamespace(webhook_secret=SECRET,
                          email_provider_order=("mailjet", "sweego"))
    db.execute("INSERT INTO sent_idempotency (idem_key, provider, sent_at) "
               "VALUES ('k1', 'mailjet', CURRENT_TIMESTAMP)")
    d = _deliverability(db, cfg)
    by_name = {p["name"]: p for p in d["providers"]}
    # Mailjet sent and reported nothing back: that is the broken-webhook shape.
    assert by_name["mailjet"]["silent"] is True
    assert d["providers_silent"] == ["mailjet"]
    # Sweego sent nothing, so its silence says nothing.
    assert by_name["sweego"]["silent"] is False
