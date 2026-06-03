import pytest
from app.web import create_app
from app.db import connect, init_schema

@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x" * 32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "1")
    monkeypatch.setenv("MAILJET_API_KEY", "mj")
    monkeypatch.setenv("MAILJET_API_SECRET", "mj")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x")
    monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("RESEND_API_KEY", "re")
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS", "24")
    monkeypatch.setenv("RATE_LIMIT_MINUTES", "15")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE", "10")
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS", "2")
    monkeypatch.setenv("DEVELOPER_EMAIL", "dev@x")
    monkeypatch.setenv("KOFI_URL", "https://k")
    conn = connect(db_path)
    init_schema(conn)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200

def test_form_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"E-Mail" in r.data
    assert b"website" in r.data  # honeypot field name

def test_form_offers_de_and_en(client):
    r_de = client.get("/?lang=de")
    r_en = client.get("/?lang=en")
    assert r_de.status_code == 200 and r_en.status_code == 200
    assert b"Anmelden" in r_de.data or b"abonnieren" in r_de.data.lower()

def _en_switch_href(html):
    import re
    m = re.search(r'href="([^"]*)"[^>]*hreflang="en"', html)
    assert m, "EN language-switch link not found"
    return m.group(1)

def test_lang_switch_preserves_form_query_params(client):
    """Switching language on the post-subscribe page must keep ?confirmed so
    the banner (and city) survive the switch."""
    en_href = _en_switch_href(client.get("/?confirmed=pending&lang=de").data.decode())
    assert "confirmed=pending" in en_href and "lang=en" in en_href

def test_admin_has_no_language_switcher(client):
    """The admin page is an internal, English-only stats page. The inherited
    DE/EN toggle does nothing there, so it must be hidden."""
    r = client.get("/admin?token=" + "a" * 32)
    assert r.status_code == 200
    assert 'hreflang="en"' not in r.data.decode()  # switcher link absent

def test_form_keeps_language_switcher(client):
    """The public form must still offer the language switcher."""
    assert 'hreflang="en"' in client.get("/").data.decode()

def test_pending_banner_shown_after_subscribe(client):
    """/subscribe redirects to /?confirmed=pending. That page MUST tell the
    user to check their inbox and confirm — otherwise the redirect looks like
    the form just reloaded with no feedback. Regression test."""
    r_de = client.get("/?confirmed=pending&lang=de")
    assert r_de.status_code == 200
    assert "fast geschafft" in r_de.data.decode().lower()
    r_en = client.get("/?confirmed=pending&lang=en")
    assert "almost done" in r_en.data.decode().lower()

def test_no_pending_banner_on_bare_form(client):
    """The pending banner must only appear after subscribing, not on the
    bare form."""
    body = client.get("/").data.decode().lower()
    assert "fast geschafft" not in body
    body_en = client.get("/?lang=en").data.decode().lower()
    assert "almost done" not in body_en

def test_subscribe_error_banner_shown(client):
    """When a confirmation email could not be sent, /?subscribe_error=mail
    shows a localized, retryable error banner."""
    r_de = client.get("/?subscribe_error=mail&lang=de")
    assert r_de.status_code == 200
    assert "erneut" in r_de.data.decode().lower()
    r_en = client.get("/?subscribe_error=mail&lang=en")
    assert "try again" in r_en.data.decode().lower()

def test_no_error_banner_on_bare_form(client):
    body = client.get("/").data.decode().lower()
    assert "leider nicht geklappt" not in body
    body_en = client.get("/?lang=en").data.decode().lower()
    assert "didn't go through" not in body_en
