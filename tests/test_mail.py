import sqlite3
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.mail import (send, MailFailed, _idem_key, _call_mailjet,
                      _call_brevo, _call_sweego)

@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn

@pytest.fixture
def brevo_configured(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-test")

@pytest.fixture
def sweego_configured(monkeypatch):
    monkeypatch.setenv("SWEEGO_API_KEY", "sw_test")

@pytest.fixture
def full_chain_order(monkeypatch):
    """The production order. The chain honors EMAIL_PROVIDER_ORDER, so tests
    expecting Brevo/Sweego in the transactional chain must name them."""
    monkeypatch.setenv("EMAIL_PROVIDER_ORDER", "mailjet,brevo,sweego")

def _ok():
    r = MagicMock()
    r.status_code = 200
    return r

def _resp(code):
    r = MagicMock()
    r.status_code = code
    return r

def test_send_uses_mailjet_when_ok(db, brevo_configured):
    with patch("app.mail._call_mailjet", return_value=_ok()) as mj, \
         patch("app.mail._call_brevo") as bv:
        send(db, "alice@example.com", "subj", "body", idem_key="k1")
    mj.assert_called_once()
    bv.assert_not_called()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k1'").fetchone()
    assert row["provider"] == "mailjet"

def test_failover_to_brevo_on_mailjet_5xx(db, brevo_configured):
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo", return_value=_ok()) as bv:
        send(db, "alice@example.com", "subj", "body", idem_key="k2")
    bv.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k2'").fetchone()
    assert row["provider"] == "brevo"

def test_failover_to_brevo_on_mailjet_401_account_block(db, brevo_configured):
    """A Mailjet 401 (e.g. account temporarily blocked) must fail over to the
    fallback, not hard-fail. Auth/account errors are exactly when failover
    matters most."""
    with patch("app.mail._call_mailjet", return_value=_resp(401)), \
         patch("app.mail._call_brevo", return_value=_ok()) as bv:
        send(db, "alice@example.com", "subj", "body", idem_key="k401")
    bv.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k401'").fetchone()
    assert row["provider"] == "brevo"

def test_failover_to_brevo_on_mailjet_403(db, brevo_configured):
    with patch("app.mail._call_mailjet", return_value=_resp(403)), \
         patch("app.mail._call_brevo", return_value=_ok()) as bv:
        send(db, "alice@example.com", "subj", "body", idem_key="k403")
    bv.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k403'").fetchone()
    assert row["provider"] == "brevo"

def test_no_fallback_on_401_without_fallback_keys(db, monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(401)), \
         patch("app.mail._call_brevo") as bv:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k401nofb")
    bv.assert_not_called()

def test_idempotency_skips_second_send(db):
    with patch("app.mail._call_mailjet", return_value=_ok()) as mj:
        send(db, "alice@example.com", "subj", "body", idem_key="k3")
        send(db, "alice@example.com", "subj", "body", idem_key="k3")
    assert mj.call_count == 1  # second call short-circuited by idempotency

def test_raises_when_both_providers_fail(db, brevo_configured):
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo", return_value=_resp(503)):
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k4")
    row = db.execute("SELECT * FROM sent_idempotency WHERE idem_key='k4'").fetchone()
    assert row is None  # claim rolled back on full failure → retry possible

def test_no_fallback_when_no_optional_provider_configured(db, monkeypatch):
    """With no fallback key set, Mailjet 5xx must NOT fall through anywhere."""
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo") as bv:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k_no_fb")
    bv.assert_not_called()

def test_no_fallback_on_mailjet_429_without_fallback_keys(db, monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(429)), \
         patch("app.mail._call_brevo") as bv:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k_no_fb_429")
    bv.assert_not_called()

@pytest.fixture
def mailjet_env(monkeypatch):
    monkeypatch.setenv("MAILJET_API_KEY", "k")
    monkeypatch.setenv("MAILJET_API_SECRET", "s")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "hallo@buergerwecker.de")
    monkeypatch.setenv("MAILJET_FROM_NAME", "Bürgerwecker")

def _capture():
    """Patch-ready post that records the url, headers and json= payload and
    returns 200."""
    seen = {}
    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        seen["json"] = kwargs.get("json")
        return _ok()
    return seen, fake_post

def test_mailjet_sets_reply_to_when_configured(monkeypatch, mailjet_env):
    monkeypatch.setenv("REPLY_TO_EMAIL", "termine@jakubwaller.eu")
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_mailjet("alice@example.com", "subj", "body")
    msg = seen["json"]["Messages"][0]
    assert msg["ReplyTo"] == {"Email": "termine@jakubwaller.eu"}

def test_mailjet_omits_reply_to_when_unset(monkeypatch, mailjet_env):
    monkeypatch.delenv("REPLY_TO_EMAIL", raising=False)
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_mailjet("alice@example.com", "subj", "body")
    assert "ReplyTo" not in seen["json"]["Messages"][0]

def test_explicit_reply_to_overrides_the_env_default(monkeypatch, mailjet_env):
    """Contact-form mail points replies at the visitor, not our own mailbox."""
    monkeypatch.setenv("REPLY_TO_EMAIL", "buergerwecker@jakubwaller.eu")
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_mailjet("alice@example.com", "subj", "body", None, "gast@example.org")
    assert seen["json"]["Messages"][0]["ReplyTo"] == {"Email": "gast@example.org"}


# ---------- Brevo & Sweego in the transactional failover chain ----------

def test_failover_prefers_brevo_over_sweego(db, brevo_configured, sweego_configured,
                                            full_chain_order):
    """A Mailjet failure goes to the next provider in the order; the one after
    only sees mail when everything before it has failed."""
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo", return_value=_ok()) as bv, \
         patch("app.mail._call_sweego") as sw:
        send(db, "alice@example.com", "subj", "body", idem_key="kb1")
    bv.assert_called_once()
    sw.assert_not_called()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='kb1'").fetchone()
    assert row["provider"] == "brevo"

def test_failover_walks_the_whole_chain(db, brevo_configured,
                                        sweego_configured, full_chain_order):
    with patch("app.mail._call_mailjet", return_value=_resp(500)), \
         patch("app.mail._call_brevo", return_value=_resp(402)), \
         patch("app.mail._call_sweego", return_value=_ok()) as sw:
        send(db, "alice@example.com", "subj", "body", idem_key="kc1")
    sw.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='kc1'").fetchone()
    assert row["provider"] == "sweego"

def test_raises_when_the_whole_chain_fails(db, brevo_configured, sweego_configured,
                                           full_chain_order):
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo", return_value=_resp(500)), \
         patch("app.mail._call_sweego", return_value=_resp(500)):
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="kd1")
    # Claim rolled back on full failure → retry possible.
    assert db.execute("SELECT * FROM sent_idempotency WHERE idem_key='kd1'").fetchone() is None

def test_brevo_and_sweego_not_in_the_chain_without_keys(db, full_chain_order,
                                                        monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo") as bv, \
         patch("app.mail._call_sweego") as sw:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="ke1")
    bv.assert_not_called()
    sw.assert_not_called()

def test_a_key_outside_the_provider_order_is_not_a_live_fallback(db, monkeypatch,
                                                                 brevo_configured):
    """The smoke-test state: BREVO_API_KEY configured for a manual test while
    EMAIL_PROVIDER_ORDER does not name brevo. The order gates the
    transactional chain too — an unproven provider must not quietly become
    a live fallback for confirmations."""
    monkeypatch.setenv("EMAIL_PROVIDER_ORDER", "mailjet")
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo") as bv:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="ko1")
    bv.assert_not_called()

def test_dropping_a_provider_from_the_order_removes_it_from_the_chain(
        db, monkeypatch, brevo_configured, sweego_configured):
    """Dropping a provider from the order must stop transactional fallbacks
    too, not just digests — this is how a provider is retired (it is how
    Resend left, 2026-08): otherwise it keeps seeing occasional mail until
    its key is deleted."""
    monkeypatch.setenv("EMAIL_PROVIDER_ORDER", "mailjet,brevo")
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo", return_value=_ok()), \
         patch("app.mail._call_sweego") as sw:
        send(db, "alice@example.com", "subj", "body", idem_key="ko2")
    sw.assert_not_called()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='ko2'").fetchone()
    assert row["provider"] == "brevo"

def test_order_without_a_usable_provider_falls_back_to_mailjet(db, monkeypatch):
    """A bad order must not leave transactional mail with no provider at all —
    Mailjet is required config, so it is the last resort."""
    monkeypatch.setenv("EMAIL_PROVIDER_ORDER", "brevo")
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_ok()) as mj:
        send(db, "alice@example.com", "subj", "body", idem_key="ko3")
    mj.assert_called_once()

def test_pending_row_blocks_second_call_after_crash(db):
    """If the process died mid-send leaving provider='pending', the next call must skip."""
    db.execute(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'pending')",
        ("k5",),
    )
    with patch("app.mail._call_mailjet") as mj, \
         patch("app.mail._call_brevo") as bv:
        send(db, "alice@example.com", "subj", "body", idem_key="k5")
    mj.assert_not_called()
    bv.assert_not_called()

def test_unsub_headers_only_with_real_url(monkeypatch, mailjet_env):
    """RFC 8058 headers must carry the per-recipient unsubscribe URL when given,
    and be ABSENT otherwise — a placeholder/dead URL is worse than no header."""
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_mailjet("a@x.com", "s", "b", "https://x/unsubscribe/tok123")
    h = seen["json"]["Messages"][0]["Headers"]
    assert h["List-Unsubscribe"] == "<https://x/unsubscribe/tok123>"
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    with patch("app.mail.requests.post", side_effect=fake):
        _call_mailjet("a@x.com", "s", "b")   # no unsub semantics (e.g. confirmation)
    assert "Headers" not in seen["json"]["Messages"][0]

# ---------- Brevo & Sweego payload shapes ----------

def test_brevo_payload_shape(monkeypatch, mailjet_env, brevo_configured):
    monkeypatch.setenv("REPLY_TO_EMAIL", "termine@jakubwaller.eu")
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_brevo("alice@example.com", "subj", "body",
                    "https://x/unsubscribe/tok123")
    assert seen["url"] == "https://api.brevo.com/v3/smtp/email"
    assert seen["headers"]["api-key"] == "xkeysib-test"
    p = seen["json"]
    assert p["sender"] == {"email": "hallo@buergerwecker.de",
                           "name": "Bürgerwecker"}
    assert p["to"] == [{"email": "alice@example.com"}]
    assert p["subject"] == "subj" and p["textContent"] == "body"
    assert p["replyTo"] == {"email": "termine@jakubwaller.eu"}
    assert p["headers"]["List-Unsubscribe"] == "<https://x/unsubscribe/tok123>"
    assert p["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

def test_brevo_omits_reply_to_and_headers_when_unset(monkeypatch, mailjet_env,
                                                     brevo_configured):
    monkeypatch.delenv("REPLY_TO_EMAIL", raising=False)
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_brevo("alice@example.com", "subj", "body")
    assert "replyTo" not in seen["json"]
    assert "headers" not in seen["json"]

def test_sweego_payload_shape(monkeypatch, mailjet_env, sweego_configured):
    monkeypatch.setenv("REPLY_TO_EMAIL", "termine@jakubwaller.eu")
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_sweego("alice@example.com", "subj", "body",
                     "https://x/unsubscribe/tok123")
    assert seen["url"] == "https://api.sweego.io/send"
    assert seen["headers"]["Api-Key"] == "sw_test"
    p = seen["json"]
    assert p["channel"] == "email" and p["provider"] == "sweego"
    assert p["campaign-type"] == "transac"
    assert p["from"] == {"email": "hallo@buergerwecker.de",
                         "name": "Bürgerwecker"}
    assert p["recipients"] == [{"email": "alice@example.com"}]
    assert p["subject"] == "subj" and p["message-txt"] == "body"
    assert p["reply-to"] == {"email": "termine@jakubwaller.eu"}
    # Sweego refuses the URL-only form the other providers send: it demands
    # <mailto:...>,<url> with the one-click Post header.
    assert p["headers"]["List-Unsubscribe"] == (
        "<mailto:termine@jakubwaller.eu?subject=abmelden>,"
        "<https://x/unsubscribe/tok123>")
    assert p["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

def test_sweego_omits_reply_to_and_headers_when_unset(monkeypatch, mailjet_env,
                                                      sweego_configured):
    monkeypatch.delenv("REPLY_TO_EMAIL", raising=False)
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_sweego("alice@example.com", "subj", "body")
    assert "reply-to" not in seen["json"]
    assert "headers" not in seen["json"]

def test_sweego_drops_unsub_headers_without_a_reply_to_mailbox(
        monkeypatch, mailjet_env, sweego_configured):
    # An unsub URL but no monitored mailbox: Sweego would 422 the URL-only
    # header, so none are sent at all.
    monkeypatch.delenv("REPLY_TO_EMAIL", raising=False)
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_sweego("alice@example.com", "subj", "body",
                     "https://x/unsubscribe/tok123")
    assert "headers" not in seen["json"]

def test_explicit_reply_to_overrides_env_for_new_providers(monkeypatch, mailjet_env,
                                                           brevo_configured,
                                                           sweego_configured):
    monkeypatch.setenv("REPLY_TO_EMAIL", "buergerwecker@jakubwaller.eu")
    seen, fake = _capture()
    with patch("app.mail.requests.post", side_effect=fake):
        _call_brevo("alice@example.com", "s", "b", None, "gast@example.org")
    assert seen["json"]["replyTo"] == {"email": "gast@example.org"}
    with patch("app.mail.requests.post", side_effect=fake):
        _call_sweego("alice@example.com", "s", "b", None, "gast@example.org")
    assert seen["json"]["reply-to"] == {"email": "gast@example.org"}

# ---------- durable per-day send counters (admin quota view) ----------

def _day_count(db, provider):
    row = db.execute(
        "SELECT n FROM email_send_counts WHERE provider=? AND day=date('now')",
        (provider,)).fetchone()
    return row["n"] if row else 0

def test_send_bumps_daily_counter(db):
    with patch("app.mail._call_mailjet", return_value=_ok()):
        send(db, "a@x.com", "s", "b", idem_key="cnt1")
        send(db, "a@x.com", "s", "b", idem_key="cnt2")
        send(db, "a@x.com", "s", "b", idem_key="cnt2")  # idempotent repeat
    assert _day_count(db, "mailjet") == 2

def test_send_counter_follows_the_chain_provider(db, brevo_configured,
                                                 full_chain_order):
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_brevo", return_value=_ok()):
        send(db, "a@x.com", "s", "b", idem_key="cnt5")
    assert _day_count(db, "brevo") == 1
    assert _day_count(db, "mailjet") == 0

def test_failed_send_does_not_bump_counter(db, monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(500)):
        with pytest.raises(MailFailed):
            send(db, "a@x.com", "s", "b", idem_key="cnt4")
    assert _day_count(db, "mailjet") == 0
