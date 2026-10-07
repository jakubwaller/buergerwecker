"""CORS on the app's JSON API: exact-origin allowlist, preflight, and the
APP_API_ENABLED gate. No network."""
import pytest

from app.db import connect, init_schema
from app.web import create_app
_ENV = {
    "TOKEN_SECRET_PRIMARY": "x" * 32, "TOKEN_SECRET_PREVIOUS": "",
    "SUBSCRIPTION_TTL_DAYS": "90", "SENSITIVE_SUBSCRIPTION_TTL_DAYS": "30",
    "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
    "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
    "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m", "MAILJET_FROM_EMAIL": "x@x",
    "MAILJET_FROM_NAME": "x", "MAILJET_DAILY_QUOTA": "6000",
    "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
    "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
    "RENEWAL_REMINDER_DAYS_BEFORE": "10", "MAX_PLANS_PER_CITY": "10",
    "PARSER_CANARY_THRESHOLD_HOURS": "2", "DEVELOPER_EMAIL": "dev@x",
    "KOFI_URL": "https://k",
}

ALLOWED = ["https://localhost", "capacitor://localhost"]


def _client(tmp_path, monkeypatch, enabled):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("APP_API_ENABLED", "1" if enabled else "0")
    init_schema(connect(db_path))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def on(tmp_path, monkeypatch):
    return _client(tmp_path, monkeypatch, True)


@pytest.fixture
def off(tmp_path, monkeypatch):
    return _client(tmp_path, monkeypatch, False)


def _preflight(client, origin, path="/api/v1/subscriptions", method="POST"):
    return client.options(path, headers={
        "Origin": origin, "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": "authorization, content-type"})


@pytest.mark.parametrize("origin", ALLOWED)
def test_allowed_origin_gets_headers(on, origin):
    r = on.get("/api/v1/cities", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == origin
    assert "Origin" in r.headers["Vary"]
    assert "Access-Control-Allow-Credentials" not in r.headers


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost",
                                    "https://localhost.evil.example", "null",
                                    "buergerwecker://localhost"])
def test_disallowed_origin_gets_none(on, origin):
    r = on.get("/api/v1/cities", headers={"Origin": origin})
    assert r.status_code == 200
    assert "Access-Control-Allow-Origin" not in r.headers
    assert "Origin" in r.headers["Vary"]


def test_no_origin_no_headers(on):
    r = on.get("/api/v1/cities")
    assert "Access-Control-Allow-Origin" not in r.headers


@pytest.mark.parametrize("path,method", [
    ("/api/v1/devices", "POST"), ("/api/v1/device", "DELETE"),
    ("/api/v1/subscriptions/3", "PUT"), ("/api/v1/cities/leipzig/slots", "GET")])
def test_preflight(on, path, method):
    r = _preflight(on, "https://localhost", path, method)
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://localhost"
    for m in ("GET", "POST", "PUT", "DELETE"):
        assert m in r.headers["Access-Control-Allow-Methods"]
    assert r.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"
    assert int(r.headers["Access-Control-Max-Age"]) > 0
    assert "Origin" in r.headers["Vary"]


def test_preflight_disallowed_origin(on):
    r = _preflight(on, "https://evil.example")
    assert not any(h.startswith("Access-Control-") for h in r.headers.keys())


def test_gate_off_request_is_404_with_cors(off):
    r = off.get("/api/v1/cities", headers={"Origin": "https://localhost"})
    assert r.status_code == 404
    assert r.get_json() == {"error": "not_available"}
    assert r.headers["Access-Control-Allow-Origin"] == "https://localhost"


def test_gate_off_preflight_still_succeeds(off):
    r = _preflight(off, "https://localhost")
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://localhost"


def test_gate_off_disallowed_origin_gets_none(off):
    r = off.get("/api/v1/cities", headers={"Origin": "https://evil.example"})
    assert r.status_code == 404
    assert "Access-Control-Allow-Origin" not in r.headers


def test_website_routes_have_no_cors(on):
    r = on.get("/healthz", headers={"Origin": "https://localhost"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_unknown_api_path_404_has_cors(on):
    r = on.get("/api/v1/nope", headers={"Origin": "https://localhost"})
    assert r.status_code == 404
    assert r.headers["Access-Control-Allow-Origin"] == "https://localhost"
    assert r.headers.getlist("Access-Control-Allow-Origin") == ["https://localhost"]
    assert "Origin" in r.headers["Vary"]


def test_api_root_404_has_cors(on):
    r = on.get("/api/v1", headers={"Origin": "capacitor://localhost"})
    assert r.headers["Access-Control-Allow-Origin"] == "capacitor://localhost"


def test_wrong_method_405_has_cors(on):
    r = on.post("/api/v1/cities", json={}, headers={"Origin": "https://localhost"})
    assert r.status_code == 405
    assert r.headers.getlist("Access-Control-Allow-Origin") == ["https://localhost"]


def test_preflight_to_unknown_path(on):
    r = _preflight(on, "https://localhost", "/api/v1/nope", "GET")
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "https://localhost"
    assert r.headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"


def test_unknown_path_disallowed_origin_gets_none(on):
    r = on.get("/api/v1/nope", headers={"Origin": "https://evil.example"})
    assert r.status_code == 404
    assert "Access-Control-Allow-Origin" not in r.headers


def test_gate_off_unknown_path_still_404_with_cors(off):
    r = off.get("/api/v1/nope", headers={"Origin": "https://localhost"})
    assert r.status_code == 404
    assert r.headers["Access-Control-Allow-Origin"] == "https://localhost"


@pytest.mark.parametrize("method, path", [
    ("get", "/api/v1/nope"), ("get", "/api/v1"), ("post", "/api/v1/cities"),
    ("delete", "/api/v1/devices"), ("patch", "/api/v1/device"),
    ("get", "/api/v1/subscriptions/x"),
])
def test_gate_off_routing_errors_are_the_gated_json_too(off, method, path):
    # Flask's HTML 404/405 would show the API is there, and which methods
    # each route takes.
    r = getattr(off, method)(path)
    assert r.status_code == 404
    assert r.get_json() == {"error": "not_available"}


def test_gate_on_still_answers_routing_errors(on):
    assert on.get("/api/v1/nope").status_code == 404
    assert on.post("/api/v1/cities", json={}).status_code == 405


def test_website_404_has_no_cors(on):
    r = on.get("/api/v1x", headers={"Origin": "https://localhost"})
    assert r.status_code == 404
    assert "Access-Control-Allow-Origin" not in r.headers
    r = on.get("/nope", headers={"Origin": "https://localhost"})
    assert r.status_code == 404
    assert not any(h.startswith("Access-Control-") for h in r.headers.keys())
    assert "Origin" not in r.headers.get("Vary", "")
