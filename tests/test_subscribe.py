import pytest
from app.web import create_app
from app.db import connect, init_schema

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "5")
    monkeypatch.setenv("MAILJET_API_KEY", "mj"); monkeypatch.setenv("MAILJET_API_SECRET", "mj")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x"); monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("RESEND_API_KEY", "re")
    monkeypatch.setenv("ADMIN_TOKEN", "a"*32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS","24");monkeypatch.setenv("RATE_LIMIT_MINUTES","15")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE","10");monkeypatch.setenv("MAX_PLANS_PER_CITY","10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS","2")
    monkeypatch.setenv("DEVELOPER_EMAIL","dev@x");monkeypatch.setenv("KOFI_URL","https://k")
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    app = create_app(); app.config["TESTING"]=True
    return app.test_client()

def _form(email="alice@example.com"):
    return {
        "lang":"de","city":"leipzig",
        "email": email,
        "appointment_type": "29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
        "all_locations": "1",
        "time_start":"00:00","time_end":"23:59",
        "weekdays": ["1","2","3","4","5"],
        "website":"",
    }

def test_subscribe_success_with_mocked_mail(client):
    from unittest.mock import patch
    with patch("app.web._send_confirmation_email") as send:
        r = client.post("/subscribe", data=_form())
    assert r.status_code in (200, 302)
    send.assert_called_once()

def test_subscribe_mail_failure_does_not_500(client):
    """If the confirmation email fails to send, /subscribe must NOT 500 and
    must NOT pretend success. It surfaces a retryable error and drops the
    orphaned (unconfirmable) pending row. Regression test for the same
    unguarded-mail-send class of bug as /confirm."""
    import os
    from unittest.mock import patch
    from app.db import connect
    # Distinct IP + email so the session-global IP limiter and the DB row
    # query don't collide with other subscribe tests.
    email = "mailfail@example.com"
    with patch("app.web._send_confirmation_email",
               side_effect=RuntimeError("mail provider exploded")):
        r = client.post("/subscribe", data=_form(email=email),
                        headers={"X-Forwarded-For": "9.9.9.9"})
    assert r.status_code == 302, r.data[:300]
    assert "subscribe_error=mail" in r.headers.get("Location", "")
    # The pending row must not linger as an unconfirmable orphan.
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT deleted_at FROM subscriptions WHERE email=?",
                       (email,)).fetchone()
    assert row is not None and row["deleted_at"] is not None

def test_honeypot_silently_drops_and_does_not_email(client):
    from unittest.mock import patch
    f = _form()
    f["website"] = "im-a-spam-bot"
    with patch("app.web._send_confirmation_email") as send:
        r = client.post("/subscribe", data=f)
    assert r.status_code in (200, 302)
    send.assert_not_called()

def test_ip_ratelimit(client):
    from unittest.mock import patch
    with patch("app.web._send_confirmation_email"):
        for _ in range(2):
            r = client.post("/subscribe", data=_form(email="b@x.com"),
                            headers={"X-Forwarded-For":"1.2.3.4"})
            assert r.status_code in (200, 302)
        r = client.post("/subscribe", data=_form(email="c@x.com"),
                        headers={"X-Forwarded-For":"1.2.3.4"})
        assert r.status_code == 429
