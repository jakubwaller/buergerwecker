import sqlite3
from unittest.mock import patch, MagicMock
import pytest
from app.db import connect, init_schema
from app.mail import send, MailFailed, _idem_key

@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn

@pytest.fixture
def resend_configured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")

def _ok():
    r = MagicMock()
    r.status_code = 200
    return r

def _resp(code):
    r = MagicMock()
    r.status_code = code
    return r

def test_send_uses_mailjet_when_ok(db):
    with patch("app.mail._call_mailjet", return_value=_ok()) as mj, \
         patch("app.mail._call_resend") as re_:
        send(db, "alice@example.com", "subj", "body", idem_key="k1")
    mj.assert_called_once()
    re_.assert_not_called()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k1'").fetchone()
    assert row["provider"] == "mailjet"

def test_failover_to_resend_on_mailjet_5xx(db, resend_configured):
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_resend", return_value=_ok()) as re_:
        send(db, "alice@example.com", "subj", "body", idem_key="k2")
    re_.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k2'").fetchone()
    assert row["provider"] == "resend"

def test_failover_to_resend_on_mailjet_401_account_block(db, resend_configured):
    """A Mailjet 401 (e.g. account temporarily blocked) must fail over to Resend,
    not hard-fail. Auth/account errors are exactly when failover matters most."""
    with patch("app.mail._call_mailjet", return_value=_resp(401)), \
         patch("app.mail._call_resend", return_value=_ok()) as re_:
        send(db, "alice@example.com", "subj", "body", idem_key="k401")
    re_.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k401'").fetchone()
    assert row["provider"] == "resend"

def test_failover_to_resend_on_mailjet_403(db, resend_configured):
    with patch("app.mail._call_mailjet", return_value=_resp(403)), \
         patch("app.mail._call_resend", return_value=_ok()) as re_:
        send(db, "alice@example.com", "subj", "body", idem_key="k403")
    re_.assert_called_once()
    row = db.execute("SELECT provider FROM sent_idempotency WHERE idem_key='k403'").fetchone()
    assert row["provider"] == "resend"

def test_no_fallback_on_401_without_resend(db, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(401)), \
         patch("app.mail._call_resend") as re_:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k401nofb")
    re_.assert_not_called()

def test_idempotency_skips_second_send(db):
    with patch("app.mail._call_mailjet", return_value=_ok()) as mj:
        send(db, "alice@example.com", "subj", "body", idem_key="k3")
        send(db, "alice@example.com", "subj", "body", idem_key="k3")
    assert mj.call_count == 1  # second call short-circuited by idempotency

def test_raises_when_both_providers_fail(db, resend_configured):
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_resend", return_value=_resp(503)):
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k4")
    row = db.execute("SELECT * FROM sent_idempotency WHERE idem_key='k4'").fetchone()
    assert row is None  # claim rolled back on full failure → retry possible

def test_no_fallback_when_resend_not_configured(db, monkeypatch):
    """When RESEND_API_KEY is unset, Mailjet 5xx must NOT fall through to Resend."""
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(503)), \
         patch("app.mail._call_resend") as re_:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k_no_fb")
    re_.assert_not_called()

def test_no_fallback_on_mailjet_429_without_resend(db, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with patch("app.mail._call_mailjet", return_value=_resp(429)), \
         patch("app.mail._call_resend") as re_:
        with pytest.raises(MailFailed):
            send(db, "alice@example.com", "subj", "body", idem_key="k_no_fb_429")
    re_.assert_not_called()

def test_pending_row_blocks_second_call_after_crash(db):
    """If the process died mid-send leaving provider='pending', the next call must skip."""
    db.execute(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'pending')",
        ("k5",),
    )
    with patch("app.mail._call_mailjet") as mj, \
         patch("app.mail._call_resend") as re_:
        send(db, "alice@example.com", "subj", "body", idem_key="k5")
    mj.assert_not_called()
    re_.assert_not_called()
