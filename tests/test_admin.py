import pytest
from app.web import create_app
from app.db import connect, init_schema
import os

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path/"t.db"))
    for k,v in {
        "TOKEN_SECRET_PRIMARY":"x"*32,"TOKEN_SECRET_PREVIOUS":"",
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
        "ADMIN_TOKEN":"admin-tok","PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "RENEWAL_REMINDER_DAYS_BEFORE":"10","MAX_PLANS_PER_CITY":"10",
        "PARSER_CANARY_THRESHOLD_HOURS":"2","DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path/"t.db")); init_schema(conn)
    app = create_app(); app.config["TESTING"]=True
    return app.test_client()

def test_admin_requires_token(client):
    r = client.get("/admin")
    assert r.status_code == 401

def test_admin_with_token(client):
    r = client.get("/admin?token=admin-tok")
    assert r.status_code == 200
    assert b"active_subscriptions" in r.data

def test_admin_wrong_token(client):
    r = client.get("/admin?token=nope")
    assert r.status_code == 401

def test_go_route_redirects_on_cache_hit(client):
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    conn.execute(
        "INSERT INTO slots_cache (slot_token, city, upstream_url) "
        "VALUES ('tok1', 'leipzig', 'https://example.eu/book/123')"
    )
    r = client.get("/go/tok1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] == "https://example.eu/book/123"

def test_go_route_returns_410_on_miss(client):
    r = client.get("/go/nonexistent-token", follow_redirects=False)
    assert r.status_code == 410
