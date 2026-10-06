"""CORS on the app's JSON API: exact-origin allowlist, preflight, and the
APP_API_ENABLED gate. No network."""
import pytest

from app.db import connect, init_schema
from app.web import create_app
from tests.test_api import _ENV

ALLOWED = ["https://localhost", "buergerwecker://localhost", "capacitor://localhost"]


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
                                    "https://localhost.evil.example", "null"])
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
