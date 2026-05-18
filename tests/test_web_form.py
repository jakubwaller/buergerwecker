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
