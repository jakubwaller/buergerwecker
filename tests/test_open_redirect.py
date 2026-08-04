"""A city slug reaches a Location header, so it is untrusted input.

`redirect("/" + city)` with city="/evil.example.com" emits
`Location: //evil.example.com` — a protocol-relative URL every browser follows
off-site. On a site whose whole pitch is that it is safe to trust with an Amt
appointment, an open redirect is a phishing kit.
"""
import pytest
from app.web import create_app
from app.db import connect, init_schema
from app.catalog import load_catalog, CatalogError

# "/x" is the one that actually escapes: "/" + "/x" == "//x". The rest cover
# the usual browser-normalisation tricks around it.
PAYLOADS = [
    "/evil.example.com",
    "//evil.example.com",
    "/\\evil.example.com",
    "\\\\evil.example.com",
    "///evil.example.com",
    "/%2Fevil.example.com",
    "https://evil.example.com",
    "..",
    "../..",
    "../../etc",
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "50")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "50")
    monkeypatch.setenv("MAILJET_API_KEY", "mj"); monkeypatch.setenv("MAILJET_API_SECRET", "mj")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x"); monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("RESEND_API_KEY", "re")
    monkeypatch.setenv("ADMIN_TOKEN", "a"*32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://buergerwecker.de")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS","24");monkeypatch.setenv("RATE_LIMIT_MINUTES","15")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE","10");monkeypatch.setenv("MAX_PLANS_PER_CITY","10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS","2")
    monkeypatch.setenv("DEVELOPER_EMAIL","dev@x");monkeypatch.setenv("KOFI_URL","https://k")
    init_schema(connect(str(tmp_path / "t.db")))
    app = create_app(); app.config["TESTING"] = True
    return app.test_client()


def _offsite(location: str | None) -> bool:
    """Would a browser leave buergerwecker.de for this Location?"""
    if not location:
        return False
    normalised = location.replace("\\", "/")
    return normalised.startswith("//") or "://" in normalised


@pytest.mark.parametrize("payload", PAYLOADS)
def test_legacy_city_query_cannot_redirect_off_site(client, payload):
    r = client.get("/", query_string={"city": payload})
    assert not _offsite(r.headers.get("Location")), r.headers.get("Location")
    assert r.status_code == 404


@pytest.mark.parametrize("payload", PAYLOADS)
def test_subscribe_cannot_redirect_off_site(client, payload):
    # GLOBAL_IP_LIMITER is a singleton for the whole test session, so each
    # POST needs an IP nobody else claims — 203.0.113.x is already spoken for
    # by the rate-limit tests.
    from unittest.mock import patch
    form = {
        "lang": "de", "city": payload, "email": "alice@example.com",
        "appointment_type": "29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
        "all_locations": "1", "time_start": "00:00", "time_end": "23:59",
        "weekdays": ["1"], "website": "",
    }
    with patch("app.web._send_confirmation_email", return_value=True):
        r = client.post("/subscribe", data=form,
                        headers={"X-Forwarded-For": f"198.51.100.{100 + PAYLOADS.index(payload)}"})
    assert not _offsite(r.headers.get("Location")), r.headers.get("Location")
    assert r.status_code == 400


def test_unknown_city_is_not_stored_as_a_subscription(client):
    """It could never be polled, so it is a row nobody will ever notify."""
    from unittest.mock import patch
    import os
    form = {
        "lang": "de", "city": "not-a-city", "email": "bob@example.com",
        "appointment_type": "29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
        "all_locations": "1", "time_start": "00:00", "time_end": "23:59",
        "weekdays": ["1"], "website": "",
    }
    with patch("app.web._send_confirmation_email", return_value=True):
        client.post("/subscribe", data=form,
                    headers={"X-Forwarded-For": "198.51.100.150"})
    conn = connect(os.environ["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0


@pytest.mark.parametrize("slug", ["../catalog", "..", "leipzig/../leipzig",
                                  "Leipzig", "leipzig ", ""])
def test_load_catalog_rejects_anything_that_is_not_a_slug(slug):
    """Path traversal at the source: CATALOG_ROOT / city with an unchecked
    city walks out of the catalog directory, and lru_cache then keys on it."""
    with pytest.raises(CatalogError):
        load_catalog(slug)


def test_load_catalog_still_loads_real_tenants():
    assert load_catalog("leipzig").city == "leipzig"
    assert load_catalog("muenster-standesamt").city == "muenster-standesamt"
