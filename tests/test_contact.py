from unittest.mock import patch

import pytest

from app.db import connect, init_schema
from app.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x" * 32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    for k, v in {
        "SUBSCRIPTION_TTL_DAYS": "90",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
        "CONTACT_RATELIMIT_PER_IP_PER_HOUR": "2",
        "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m",
        "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x",
        "MAILJET_DAILY_QUOTA": "6000", "RESEND_API_KEY": "r",
        "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
        "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
        "RENEWAL_REMINDER_DAYS_BEFORE": "10", "MAX_PLANS_PER_CITY": "10",
        "PARSER_CANARY_THRESHOLD_HOURS": "2", "DEVELOPER_EMAIL": "d@x",
        "KOFI_URL": "https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path)
    init_schema(conn)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


# GLOBAL_IP_LIMITER is module-level state shared by the whole suite, so every
# test posts from its own IP or an earlier test's hits leak in and 429 this one.
def _post(client, ip, **fields):
    form = {"email": "a@b.com", "message": "Hallo", "projekt": "papamap"}
    form.update(fields)
    return client.post("/kontakt", data=form,
                       headers={"X-Forwarded-For": ip})


def test_get_renders_form_with_project_preselected(client):
    r = client.get("/kontakt?projekt=zapfkompass")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'name="message"' in body
    assert '<option value="zapfkompass" selected' in body


def test_valid_submission_sends_to_developer(client):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.1", message="Ist das Fass leer?")
    assert r.status_code == 200
    assert send.call_count == 1
    _conn, to, subject, body = send.call_args.args
    assert to == "d@x"
    assert "PapaMap" in subject
    assert "Ist das Fass leer?" in body
    assert "a@b.com" in body


def test_honeypot_silently_accepts_without_sending(client):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.2", website="http://spam.example")
    assert r.status_code == 200
    send.assert_not_called()


def test_missing_message_is_rejected(client):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.3", message="   ")
    assert r.status_code == 400
    send.assert_not_called()


def test_reply_to_is_the_sender_not_our_own_mailbox(client):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.9", email="besucher@example.org")
    assert r.status_code == 200
    assert send.call_args.kwargs["reply_to"] == "besucher@example.org"


@pytest.mark.parametrize("bad", [
    "not-an-address",
    "no-tld@example",
    "spaced addr@example.com",
    "a@b.com, victim@example.com",
    # A header-injection shape: harmless as JSON, rejected anyway.
    "a@b.com>\r\nBcc: victim@example.com",
])
def test_invalid_email_is_rejected(client, bad):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.4", email=bad)
    assert r.status_code == 400
    send.assert_not_called()


def test_rate_limit_blocks_after_configured_hits(client):
    with patch("app.web.mail_send"):
        for _ in range(2):  # CONTACT_RATELIMIT_PER_IP_PER_HOUR = 2
            assert _post(client, "10.1.0.5").status_code == 200
        assert _post(client, "10.1.0.5").status_code == 429


def test_delivery_failure_reports_error(client):
    with patch("app.web.mail_send", side_effect=RuntimeError("provider down")):
        r = _post(client, "10.1.0.6")
    assert r.status_code == 502


def test_unknown_project_slug_is_not_echoed_into_mail(client):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.7", projekt="<script>evil</script>")
    assert r.status_code == 200
    _conn, _to, subject, body = send.call_args.args
    assert "script" not in subject
    assert "unbekannt" in body


def test_overlong_message_is_truncated(client):
    with patch("app.web.mail_send") as send:
        r = _post(client, "10.1.0.8", message="x" * 9000)
    assert r.status_code == 200
    _conn, _to, _subject, body = send.call_args.args
    assert body.count("x") == 5000
