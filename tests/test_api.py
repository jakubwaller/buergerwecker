"""The app's JSON API (app/api.py): registration and the per-device secret,
the catalog, and subscriptions under the website's rules. No network, no
relay: nothing here sends a push (the devices are verified straight in the
database; see tests/test_verification.py)."""
import hashlib
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.db import connect, init_schema
from app.ratelimit import GLOBAL_IP_LIMITER
from app.repo import active_subscriptions, retire_device
from app.web import create_app

LEIPZIG_SVC = "7ea65748-8333-4269-bf65-8453f9f27cf4"      # Abholung Ausweisdokumente
LEIPZIG_SVC_2 = "9bd4aa45-24d8-4733-bbf2-def52e05f684"    # Abmeldung Wohnsitz
LEIPZIG_LOC = "dafd1124-6d47-4c5e-97d1-9311527c3241"      # Bürgerbüro Leutzsch
SBGG_SVC = "2471"                                         # Münster Standesamt, Art. 9

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
    "KOFI_URL": "https://k", "APP_API_ENABLED": "1",
    # Every test client is one address; the tests of the limit lower it.
    "MAX_NEW_DEVICES_PER_IP_PER_DAY": "999",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path)
    init_schema(conn)
    # The limiter is a process-wide singleton; every test starts clean.
    GLOBAL_IP_LIMITER._events.clear()
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _db():
    import os
    return connect(os.environ["DB_PATH"])


def tok(name="tok-1", platform="apns"):
    """A synthetic push token of the platform's shape, named for the test:
    64 hex characters for APNs, an FCM-style `<id>:APA91b…` for FCM."""
    digest = hashlib.sha256(name.encode()).hexdigest()
    if platform == "apns":
        return digest
    return f"{digest[:22]}:APA91b{hashlib.sha512(name.encode()).hexdigest()}"


def _register(client, platform="apns", token="tok-1", language="de",
              verified=True):
    """Register a device; `token` is a name for `tok()`. Verified by default
    (straight in the database, as if the code had been posted back) so the
    tests of everything else need no push; tests/test_verification.py covers
    the verification itself."""
    r = client.post("/api/v1/devices", json={"platform": platform,
                                             "token": tok(token, platform),
                                             "language": language})
    assert r.status_code == 201, r.data
    body = r.get_json()
    if verified:
        conn = _db()
        conn.execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP "
                     "WHERE id=?", (body["device_id"],))
    return body["device_id"], body["secret"]


def _auth(device_id, secret):
    return {"Authorization": f"Bearer {device_id}.{secret}"}


def _subscribe(client, auth, **over):
    body = {"city": "leipzig", "appointment_type": LEIPZIG_SVC,
            "locations": "all", "weekdays": [1, 2, 3, 4, 5],
            "time_start": "08:00", "time_end": "12:00"}
    body.update(over)
    return client.post("/api/v1/subscriptions", json=body, headers=auth)


# ---------------------------------------------------------------------------
# Registration and authentication

def test_register_stores_the_hashed_secret_and_shows_it_once(client):
    dev, secret = _register(client)
    row = _db().execute("SELECT * FROM push_devices WHERE id=?", (dev,)).fetchone()
    assert row["platform"] == "apns" and row["token"] == tok()
    assert row["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(dict(row))
    r = client.get("/api/v1/device", headers=_auth(dev, secret))
    assert r.status_code == 200
    assert r.get_json() == {"device_id": dev, "platform": "apns", "language": "de",
                            "created_at": row["created_at"], "verified": True,
                            "subscriptions": []}


def test_the_same_token_registering_again_is_the_same_device_with_a_pending_secret(client):
    dev, old = _register(client)
    assert _subscribe(client, _auth(dev, old)).status_code == 201
    dev2, new = _register(client)
    assert dev2 == dev and new != old
    # The install that works is not broken; the new one waits for its code.
    r = client.get("/api/v1/device", headers=_auth(dev, old))
    assert r.status_code == 200 and len(r.get_json()["subscriptions"]) == 1
    # ... and learns nothing about the device it is waiting for.
    r = client.get("/api/v1/device", headers=_auth(dev, new))
    assert r.status_code == 200 and r.get_json() == {"verified": False}


_HEX64 = "0123456789abcdef" * 4


@pytest.mark.parametrize("body, error", [
    ({"platform": "sms", "token": "t"}, "unknown_platform"),
    ({"platform": "fcm", "token": ""}, "invalid_push_token"),
    ({"platform": "fcm", "token": "has space"}, "invalid_push_token"),
    ({"platform": "fcm", "token": 12345}, "invalid_push_token"),
    ({"platform": "fcm", "token": "a" * 63}, "invalid_push_token"),
    ({"platform": "fcm", "token": "a" * 4097}, "invalid_push_token"),
    ({"platform": "fcm", "token": "a" * 100 + "/x"}, "invalid_push_token"),
    ({"platform": "fcm", "token": "a" * 100 + "é"}, "invalid_push_token"),
    ({"platform": "apns", "token": "tok-1"}, "invalid_push_token"),
    ({"platform": "apns", "token": _HEX64[:-1]}, "invalid_push_token"),
    ({"platform": "apns", "token": _HEX64 * 4}, "invalid_push_token"),   # 256
    ({}, "unknown_platform"),
])
def test_register_rejects_a_bad_platform_or_token(client, body, error):
    r = client.post("/api/v1/devices", json=body)
    assert r.status_code == 400 and r.get_json()["error"] == error


@pytest.mark.parametrize("alias", [
    # Each of these reached APNs as /3/device/<T>: httpx drops the fragment
    # and the query and resolves dot segments, so one phone verified a row
    # per alias.
    _HEX64 + "#1", _HEX64 + "?x", "x/../" + _HEX64, "./" + _HEX64,
    _HEX64 + "/", _HEX64 + "%23", _HEX64[:32] + "\n" + _HEX64[32:], _HEX64 + "\x00",
    _HEX64 + "K",            # KELVIN SIGN lowercases to an ASCII "k"
])
def test_register_refuses_every_alias_of_an_apns_token(client, alias):
    r = client.post("/api/v1/devices", json={"platform": "apns", "token": alias})
    assert r.status_code == 400 and r.get_json()["error"] == "invalid_push_token"
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


def test_a_token_that_is_not_utf8_is_a_400_not_a_500(client):
    for platform in ("apns", "fcm"):
        r = client.post("/api/v1/devices", data=json.dumps(
            {"platform": platform, "token": "ab\ud800" * 40}),
            content_type="application/json")
        assert r.status_code == 400, platform
        assert r.get_json()["error"] == "invalid_push_token"


def test_apns_tokens_are_stored_lowercase_so_one_token_is_one_row(client):
    upper = tok("case").upper()
    r = client.post("/api/v1/devices", json={"platform": "apns", "token": upper})
    assert r.status_code == 201
    again = client.post("/api/v1/devices", json={"platform": "apns",
                                                 "token": tok("case")})
    assert again.get_json()["device_id"] == r.get_json()["device_id"]
    row = _db().execute("SELECT token FROM push_devices").fetchone()
    assert row["token"] == tok("case")


def test_real_shaped_fcm_tokens_pass(client):
    # The current shape: a 22-character installation id, ':' and an
    # APA91b... body, about 163 characters in all; and an older one without
    # the id. Synthetic, same alphabet and length.
    current = "cAbC1dEf2GhI3jKl4MnO5p" + ":APA91b" + "Xy-_" * 34
    legacy = "APA91b" + "Qz9_-a" * 25
    for t in (current, legacy):
        r = client.post("/api/v1/devices", json={"platform": "fcm", "token": t})
        assert r.status_code == 201, (len(t), r.data)


def test_register_refuses_a_body_that_is_not_json(client):
    # A text/plain post is a "simple" request that a browser sends cross-site
    # without a CORS preflight; requiring JSON forces the preflight.
    for data, ctype in (("not json", "text/plain"),
                        (json.dumps({"platform": "apns", "token": tok()}), "text/plain"),
                        ("platform=apns", "application/x-www-form-urlencoded")):
        r = client.post("/api/v1/devices", data=data, content_type=ctype)
        assert r.status_code == 415, ctype
        assert r.get_json()["error"] == "unsupported_media_type"
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


def test_every_route_with_a_body_requires_json(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    for method, path in (("put", "/api/v1/device"),
                         ("post", "/api/v1/device/verify"),
                         ("post", "/api/v1/device/verify/resend"),
                         ("post", "/api/v1/subscriptions"),
                         ("put", "/api/v1/subscriptions/1"),
                         ("post", "/api/v1/subscriptions/1/renew")):
        r = getattr(client, method)(path, data="{}", content_type="text/plain",
                                    headers=auth)
        assert r.status_code == 415, (method, path)
        assert r.get_json()["error"] == "unsupported_media_type"


def test_a_json_body_that_is_not_an_object_is_empty(client):
    r = client.post("/api/v1/devices", data="[1, 2]", content_type="application/json")
    assert r.status_code == 400 and r.get_json()["error"] == "unknown_platform"


@pytest.mark.parametrize("header", [
    None, "Bearer", "Bearer 1", "Bearer x.y", "Bearer 1.", "Basic 1.abc",
    "Bearer ².x", "Bearer 999999999999999999999999.x",
])
def test_authenticated_routes_reject_a_missing_or_malformed_bearer(client, header):
    headers = {"Authorization": header} if header else {}
    r = client.get("/api/v1/subscriptions", headers=headers)
    assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}


def test_wrong_secret_or_unknown_device_is_unauthorized(client):
    dev, secret = _register(client)
    assert client.get("/api/v1/device", headers=_auth(dev, "nope")).status_code == 401
    assert client.get("/api/v1/device", headers=_auth(dev + 1, secret)).status_code == 401


def test_a_retired_device_is_told_to_register_afresh(client):
    dev, secret = _register(client)
    retire_device(_db(), dev, "Unregistered", token=tok())
    r = client.get("/api/v1/subscriptions", headers=_auth(dev, secret))
    assert r.status_code == 410 and r.get_json()["error"] == "device_retired"
    # Registering again revives it; the verified old secret works again.
    dev2, new = _register(client)
    assert dev2 == dev
    assert client.get("/api/v1/subscriptions", headers=_auth(dev, secret)).status_code == 200
    assert client.get("/api/v1/device", headers=_auth(dev, new)).status_code == 200


def test_every_authenticated_call_restarts_the_purge_clock(client):
    dev, secret = _register(client)
    db = _db()
    db.execute("UPDATE push_devices SET last_seen_at=datetime('now','-40 days') WHERE id=?",
               (dev,))
    client.get("/api/v1/subscriptions", headers=_auth(dev, secret))
    seen = db.execute("SELECT last_seen_at FROM push_devices WHERE id=?", (dev,)).fetchone()[0]
    assert datetime.fromisoformat(seen) > datetime.utcnow() - timedelta(minutes=1)


def test_registration_is_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    c = create_app().test_client()
    assert c.post("/api/v1/devices", json={"platform": "apns", "token": tok("a")}).status_code == 201
    assert c.post("/api/v1/devices", json={"platform": "apns", "token": tok("b")}).status_code == 201
    r = c.post("/api/v1/devices", json={"platform": "apns", "token": tok("c")})
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    # Reads are not counted: the app lists its subscriptions on every launch.
    assert c.get("/api/v1/cities").status_code == 200
    # The API has its own bucket: app traffic behind a carrier NAT does not
    # use up the sign-up form's budget for everyone there.
    keys = set(GLOBAL_IP_LIMITER._events)
    assert keys and all(k.startswith("api:") for k in keys)


def test_ipv6_clients_are_counted_by_their_slash_64(client, monkeypatch):
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    c = create_app().test_client()

    def reg(name, addr):
        return c.post("/api/v1/devices", json={"platform": "apns", "token": tok(name)},
                      headers={"X-Forwarded-For": addr})
    assert reg("v6-a", "2001:db8:1:2::1").status_code == 201
    assert reg("v6-b", "2001:db8:1:2:ffff::9").status_code == 201
    assert reg("v6-c", "2001:db8:1:2:abcd::5").status_code == 429     # same /64
    assert reg("v6-d", "2001:db8:1:3::1").status_code == 201         # next /64
    # IPv4 stays the full address, and an IPv4-mapped one counts as it.
    assert reg("v4-a", "192.0.2.1").status_code == 201
    assert reg("v4-b", "::ffff:192.0.2.1").status_code == 201
    assert reg("v4-c", "192.0.2.1").status_code == 429
    assert reg("v4-d", "192.0.2.2").status_code == 201
    assert "api:2001:db8:1:2::/64" in GLOBAL_IP_LIMITER._events


def test_new_devices_per_network_per_day_hold_across_workers(client, monkeypatch):
    monkeypatch.setenv("MAX_NEW_DEVICES_PER_IP_PER_DAY", "2")
    c = create_app().test_client()

    def reg(name, addr="192.0.2.10"):
        return c.post("/api/v1/devices", json={"platform": "apns", "token": tok(name)},
                      headers={"X-Forwarded-For": addr})
    first = reg("n1")
    assert first.status_code == 201
    assert reg("n2").status_code == 201
    # The per-process limiter forgets (another worker, a restart); the
    # database does not.
    GLOBAL_IP_LIMITER._events.clear()
    over = reg("n3")
    assert over.status_code == 429 and over.get_json()["error"] == "rate_limited"
    assert 0 < over.get_json()["retry_after"] <= 86400
    # Deleting a device does not give its place back.
    dev, secret = first.get_json()["device_id"], first.get_json()["secret"]
    assert c.delete("/api/v1/device", headers=_auth(dev, secret)).status_code == 204
    assert reg("n4").status_code == 429
    # A token that already has a row is not a new device: re-registering works.
    assert reg("n2").status_code == 201
    # Another network has its own count, and the table holds no address.
    assert reg("n5", "198.51.100.7").status_code == 201
    buckets = [r[0] for r in _db().execute("SELECT bucket FROM rate_events")]
    assert len(buckets) == 3 and not any("192.0.2" in b or "198.51" in b for b in buckets)
    # The window frees after a day.
    _db().execute("UPDATE rate_events SET at=datetime('now','-25 hours')")
    assert reg("n6").status_code == 201


def test_deleting_a_device_counts_against_the_network_but_is_never_refused(client, monkeypatch):
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    c = create_app().test_client()
    dev, secret = _register(c, token="del-1")
    assert c.delete("/api/v1/device", headers=_auth(dev, secret)).status_code == 204
    # Register (1) and delete (2) used the budget up.
    r = c.post("/api/v1/devices", json={"platform": "apns", "token": tok("del-2")})
    assert r.status_code == 429
    GLOBAL_IP_LIMITER._events.clear()
    dev, secret = _register(c, token="del-3")
    _register(c, token="del-4")
    assert c.delete("/api/v1/device", headers=_auth(dev, secret)).status_code == 204


# ---------------------------------------------------------------------------
# The device

def test_device_update_rotates_the_token_and_changes_the_language(client):
    dev, secret = _register(client)
    db = _db()
    db.execute("UPDATE push_devices SET dead_since=CURRENT_TIMESTAMP WHERE id=?", (dev,))
    assert client.put("/api/v1/device", json={"language": "fr"},
                      headers=_auth(dev, secret)).status_code == 400
    for bad in ("", "tok-2", tok("tok-2") + "#1", tok("fcm", "fcm"), 7):
        assert client.put("/api/v1/device", json={"token": bad},
                          headers=_auth(dev, secret)).status_code == 400, bad
    r = client.put("/api/v1/device", json={"token": tok("tok-2").upper(), "language": "en"},
                   headers=_auth(dev, secret))
    assert r.status_code == 200 and r.get_json()["language"] == "en"
    assert r.get_json()["verified"] is False       # a new token must verify again
    row = db.execute("SELECT * FROM push_devices WHERE id=?", (dev,)).fetchone()
    assert row["token"] == tok("tok-2") and row["language"] == "en"
    assert row["dead_since"] is None     # a new token: the evidence clock starts over


def test_device_update_refuses_a_token_another_row_holds(client):
    dev, secret = _register(client, token="tok-1")
    _register(client, token="tok-2")
    r = client.put("/api/v1/device", json={"token": tok("tok-2")},
                   headers=_auth(dev, secret))
    assert r.status_code == 409 and r.get_json()["error"] == "token_in_use"


def test_owner_writes_stop_once_the_secret_was_replaced_in_between(client):
    """The route authenticated the old secret; a pending one promoted before
    the write (the phone changed hands) makes the write a no-op and a 401."""
    from app.repo import delete_device, update_device
    dev, secret = _register(client)
    old_hash = hashlib.sha256(secret.encode()).hexdigest()
    db = _db()
    db.execute("UPDATE push_devices SET secret_hash=? WHERE id=?", ("f" * 64, dev))
    assert update_device(db, dev, secret_hash=old_hash, language="en") == "unauthorized"
    assert update_device(db, dev, secret_hash=old_hash, token=tok("x")) == "unauthorized"
    assert delete_device(db, dev, old_hash) is False
    row = db.execute("SELECT token, language FROM push_devices WHERE id=?", (dev,)).fetchone()
    assert row["token"] == tok() and row["language"] == "de"
    assert update_device(db, dev, secret_hash="f" * 64, language="en") == "ok"
    assert delete_device(db, dev, "f" * 64) is True


def test_delete_device_takes_its_subscriptions_along_and_ends_the_secret(client):
    dev, secret = _register(client)
    sid = _subscribe(client, _auth(dev, secret)).get_json()["id"]
    db = _db()
    db.execute("INSERT INTO seen_slots (subscription_id, slot_hash) VALUES (?, 'h')", (sid,))
    r = client.delete("/api/v1/device", headers=_auth(dev, secret))
    assert r.status_code == 204
    assert db.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM seen_slots").fetchone()[0] == 0
    assert client.get("/api/v1/device", headers=_auth(dev, secret)).status_code == 401


# ---------------------------------------------------------------------------
# Catalog

def test_cities_lists_every_tenant_with_its_city_name(client):
    body = client.get("/api/v1/cities").get_json()
    by_slug = {c["slug"]: c for c in body["cities"]}
    assert by_slug["leipzig"]["city"] == "Leipzig"
    assert by_slug["leipzig"]["office"] == "Bürgerbüro"
    assert by_slug["leipzig"]["has_sensitive"] is False
    assert by_slug["muenster-standesamt"]["has_sensitive"] is True
    en = client.get("/api/v1/cities?lang=en").get_json()
    assert {c["slug"]: c for c in en["cities"]}["leipzig"]["office"] == "Citizens' office"


def test_city_returns_the_form_the_website_shows(client):
    body = client.get("/api/v1/cities/leipzig").get_json()
    assert body["city"] == "Leipzig" and body["ttl_days"] == 90
    assert body["sensitive_ttl_days"] == 30
    assert body["booking_url"] == "https://x/go/leipzig"
    services = {s["id"]: s for s in body["services"]}
    assert services[LEIPZIG_SVC]["name"] == "Abholung Ausweisdokumente"
    assert services[LEIPZIG_SVC]["sensitive"] is False
    assert any(loc["id"] == LEIPZIG_LOC for loc in body["locations"])
    en = client.get("/api/v1/cities/leipzig?lang=en").get_json()
    assert en["booking_url"] == "https://x/go/leipzig?lang=en"
    sbgg = client.get("/api/v1/cities/muenster-standesamt").get_json()
    assert {s["id"]: s for s in sbgg["services"]}[SBGG_SVC]["sensitive"] is True


def test_city_slots_is_the_last_poll_per_watched_service(client):
    from app.models import PollPlan
    from app.snapshots import record_snapshots
    from app.models import Slot
    from datetime import datetime as dt
    db = _db()
    plan = PollPlan(city="leipzig", appointment_type=LEIPZIG_SVC, locations="all")
    other = PollPlan(city="leipzig", appointment_type=LEIPZIG_SVC_2, locations="all")
    record_snapshots(db, [plan, other], {"leipzig"}, [plan, other], {
        plan.key(): [Slot("2026-06-10", "10:30", LEIPZIG_LOC, LEIPZIG_SVC, "t"),
                     Slot("2026-06-09", "08:00", "loc-unknown", LEIPZIG_SVC, "t")],
        other.key(): []}, now=dt(2026, 6, 8, 12, 0))
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    assert _subscribe(client, auth).status_code == 201
    assert _subscribe(client, auth, city="bonn",
                      appointment_type=_bonn_service()).status_code == 201
    r = client.get("/api/v1/cities/leipzig/slots", headers=auth)
    assert r.headers["Cache-Control"] == "private, no-store"
    body = r.get_json()
    assert body["slug"] == "leipzig" and body["polled_at"] == "2026-06-08T12:00:00Z"
    assert [s["name"] for s in body["services"]] == ["Abholung Ausweisdokumente",
                                                     "Abmeldung Wohnsitz"]
    first = body["services"][0]
    assert first["id"] == LEIPZIG_SVC and first["n_total"] == 2
    assert first["polled_at"] == "2026-06-08T12:00:00Z"
    assert first["earliest"] == {"date": "2026-06-09", "time": "08:00",
                                 "location": "loc-unknown",
                                 "location_name": "loc-unknown"}
    assert first["slots"][1] == {"date": "2026-06-10", "time": "10:30",
                                 "location": LEIPZIG_LOC,
                                 "location_name": "Bürgerbüro Leutzsch"}
    second = body["services"][1]
    assert second["slots"] == [] and second["earliest"] is None and second["n_total"] == 0
    # One service only, and English labels.
    body = client.get(f"/api/v1/cities/leipzig/slots?service={LEIPZIG_SVC_2}&lang=en",
                      headers=auth).get_json()
    assert [s["id"] for s in body["services"]] == [LEIPZIG_SVC_2]
    # A city nobody's poll has reached yet has nothing to show, and says so.
    assert client.get("/api/v1/cities/bonn/slots", headers=auth).get_json() == {
        "slug": "bonn", "polled_at": None, "services": []}
    assert client.get("/api/v1/cities/atlantis/slots", headers=auth).status_code == 404


def _bonn_service():
    from app.catalog import load_catalog
    return next(u for u in load_catalog("bonn").appointment_types.values()
                if not load_catalog("bonn").is_sensitive(u))


def _snapshot(city, service, slot_loc="loc-x"):
    from app.models import PollPlan, Slot
    from app.snapshots import record_snapshots
    plan = PollPlan(city=city, appointment_type=service, locations="all")
    record_snapshots(_db(), [plan], {city}, [plan], {
        plan.key(): [Slot("2026-06-10", "10:30", slot_loc, service, "t")]})


def test_city_slots_is_for_a_verified_device_that_watches_the_city(client):
    _snapshot("leipzig", LEIPZIG_SVC)
    url = "/api/v1/cities/leipzig/slots"
    r = client.get(url)
    assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}
    assert r.headers["Cache-Control"] == "no-store"
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    # Verified, but watching nothing here.
    r = client.get(url, headers=auth)
    assert r.status_code == 403 and r.get_json()["error"] == "not_subscribed"
    assert r.get_json()["message"].startswith("Du beobachtest")
    en = client.get(url + "?lang=en", headers=auth).get_json()
    assert en["message"].startswith("You aren't watching")
    sid = _subscribe(client, auth).get_json()["id"]
    assert client.get(url, headers=auth).status_code == 200
    # An expired subscription is paused, and watches nothing.
    _db().execute("UPDATE subscriptions SET expires_at=datetime('now','-1 hour') "
                  "WHERE id=?", (sid,))
    assert client.get(url, headers=auth).get_json()["error"] == "not_subscribed"
    _db().execute("UPDATE subscriptions SET expires_at=datetime('now','+1 day') "
                  "WHERE id=?", (sid,))
    assert client.delete(f"/api/v1/subscriptions/{sid}", headers=auth).status_code == 204
    assert client.get(url, headers=auth).get_json()["error"] == "not_subscribed"
    # Unverified (a token change), a pending credential, a retired device.
    other, osecret = _register(client, token="other")
    _subscribe(client, _auth(other, osecret))
    _db().execute("UPDATE push_devices SET verified_at=NULL WHERE id=?", (other,))
    r = client.get(url, headers=_auth(other, osecret))
    assert r.status_code == 403 and r.get_json()["error"] == "device_unverified"
    _db().execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP WHERE id=?",
                  (other,))
    _, pending = _register(client, token="other", verified=False)
    r = client.get(url, headers=_auth(other, pending))
    assert r.status_code == 403 and r.get_json()["error"] == "device_unverified"
    retire_device(_db(), other, "Unregistered", token=tok("other"))
    r = client.get(url, headers=_auth(other, osecret))
    assert r.status_code == 410 and r.get_json()["error"] == "device_retired"


def test_city_slots_shows_a_special_category_service_only_to_its_own_watchers(client):
    city = "muenster-standesamt"
    _snapshot(city, SBGG_SVC)
    _snapshot(city, "2434")
    url = f"/api/v1/cities/{city}/slots"
    ordinary, so = _register(client, token="ordinary")
    assert _subscribe(client, _auth(ordinary, so), city=city,
                      appointment_type="2434").status_code == 201
    body = client.get(url, headers=_auth(ordinary, so)).get_json()
    assert [s["id"] for s in body["services"]] == ["2434"]
    # Asking for it by id does not help.
    body = client.get(url + f"?service={SBGG_SVC}", headers=_auth(ordinary, so)).get_json()
    assert body["services"] == [] and body["polled_at"] is None
    watcher, sw = _register(client, token="watcher")
    assert _subscribe(client, _auth(watcher, sw), city=city, appointment_type=SBGG_SVC,
                      consent_special=True).status_code == 201
    body = client.get(url, headers=_auth(watcher, sw)).get_json()
    assert {s["id"] for s in body["services"]} == {SBGG_SVC, "2434"}


def test_city_slots_reads_are_capped_per_device_across_workers(client):
    _snapshot("leipzig", LEIPZIG_SVC)
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    _subscribe(client, auth)
    from app.api import MAX_SLOT_READS_PER_DEVICE_PER_HOUR as cap
    _db().executemany("INSERT INTO rate_events (bucket) VALUES (?)",
                      [(f"slots:{dev}",)] * (cap - 1))
    assert client.get("/api/v1/cities/leipzig/slots", headers=auth).status_code == 200
    r = client.get("/api/v1/cities/leipzig/slots", headers=auth)
    assert r.status_code == 429 and 0 < r.get_json()["retry_after"] <= 3600
    # Another device has its own count, and the write limit is not touched.
    other, osecret = _register(client, token="other")
    _subscribe(client, _auth(other, osecret))
    assert client.get("/api/v1/cities/leipzig/slots",
                      headers=_auth(other, osecret)).status_code == 200


def test_authenticated_answers_are_never_stored_and_the_catalog_may_be(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    for r in (client.get("/api/v1/device", headers=auth),
              client.get("/api/v1/subscriptions", headers=auth),
              _subscribe(client, auth),
              client.get("/api/v1/subscriptions", headers=_auth(dev, "wrong")),
              client.post("/api/v1/devices", json={"platform": "apns", "token": tok("n")})):
        assert r.headers["Cache-Control"] == "no-store", r.request.path
    assert "Cache-Control" not in client.get("/api/v1/cities").headers
    assert "Cache-Control" not in client.get("/api/v1/cities/leipzig").headers


def test_unknown_city_is_not_found(client):
    for slug in ("atlantis", "..", "Leipzig"):
        r = client.get(f"/api/v1/cities/{slug}")
        assert r.status_code == 404, slug
        assert r.get_json()["error"] == "unknown_city"


# ---------------------------------------------------------------------------
# Subscriptions

def test_subscribe_is_live_at_once_and_reads_back(client):
    dev, secret = _register(client)
    r = _subscribe(client, _auth(dev, secret), locations=[LEIPZIG_LOC],
                   max_days_ahead=14)
    assert r.status_code == 201, r.data
    body = r.get_json()
    assert body["city"] == "leipzig" and body["appointment_type"] == LEIPZIG_SVC
    assert body["locations"] == [LEIPZIG_LOC] and body["weekdays"] == [1, 2, 3, 4, 5]
    assert body["time_start"] == "08:00" and body["time_end"] == "12:00"
    assert body["max_days_ahead"] == 14 and body["active"] is True
    assert body["consent_special"] is False and body["language"] == "de"
    row = _db().execute("SELECT * FROM subscriptions WHERE id=?", (body["id"],)).fetchone()
    assert row["email"] == "" and row["device_id"] == dev
    assert row["confirmed_at"] is not None     # no double opt-in
    assert [s.id for s in active_subscriptions(_db())] == [body["id"]]
    listed = client.get("/api/v1/subscriptions", headers=_auth(dev, secret)).get_json()
    assert listed == {"subscriptions": [body]}
    assert client.get(f"/api/v1/subscriptions/{body['id']}",
                      headers=_auth(dev, secret)).get_json() == body


def test_subscribe_defaults_every_day_every_office_all_day(client):
    dev, secret = _register(client)
    r = client.post("/api/v1/subscriptions",
                    json={"city": "leipzig", "appointment_type": LEIPZIG_SVC},
                    headers=_auth(dev, secret))
    assert r.status_code == 201
    body = r.get_json()
    assert body["locations"] == "all" and body["weekdays"] == [1, 2, 3, 4, 5, 6, 7]
    assert body["time_start"] == "00:00" and body["time_end"] == "23:59"
    assert body["max_days_ahead"] is None


@pytest.mark.parametrize("over, status, error", [
    ({"city": "atlantis"}, 400, "unknown_city"),
    ({"city": ""}, 400, "unknown_city"),
    ({"appointment_type": ""}, 400, "missing_type"),
    ({"appointment_type": "svc-nope"}, 400, "unknown_type"),
    ({"locations": ["loc-nope"]}, 400, "unknown_location"),
    ({"locations": "some"}, 400, "unknown_location"),
    ({"time_start": "25:00"}, 400, "invalid_time"),
    ({"time_start": "13:00", "time_end": "09:00"}, 400, "invalid_time"),
])
def test_subscribe_validates_like_the_form(client, over, status, error):
    dev, secret = _register(client)
    r = _subscribe(client, _auth(dev, secret), **over)
    assert r.status_code == status
    body = r.get_json()
    assert body["error"] == error
    assert body["message"]      # the website's sentence, in the device language
    assert active_subscriptions(_db()) == []


def test_subscribe_stores_each_office_and_weekday_once(client):
    # Every office entry goes upstream in the poller's request for the plan,
    # every cycle: one valid id repeated was a multi-MB POST to the city.
    dev, secret = _register(client)
    r = _subscribe(client, _auth(dev, secret), locations=[LEIPZIG_LOC] * 200,
                   weekdays=[3, 1, 3, "1", 7] * 100)     # still under MAX_BODY_BYTES
    assert r.status_code == 201, r.data
    body = r.get_json()
    assert body["locations"] == [LEIPZIG_LOC] and body["weekdays"] == [1, 3, 7]


@pytest.mark.parametrize("over, field, stored", [
    ({"weekdays": ["²", 2]}, "weekdays", [2]),
    ({"max_days_ahead": "²"}, "max_days_ahead", None),
    ({"max_days_ahead": "9" * 5000}, "max_days_ahead", None),
])
def test_subscribe_ignores_digits_int_cannot_read(client, over, field, stored):
    # str.isdigit() takes "²", and int() refuses it, or a 5,000-digit
    # string: both were a 500.
    dev, secret = _register(client)
    r = _subscribe(client, _auth(dev, secret), **over)
    assert r.status_code == 201, r.data
    assert r.get_json()[field] == stored


@pytest.mark.parametrize("raw, stored", [
    (14, 14), (9999, 9999), (10000, None), (10 ** 30, None), (0, None), (-3, None),
    (True, None),
])
def test_max_days_ahead_as_a_number_has_the_same_ceiling_as_a_string(client, raw, stored):
    dev, secret = _register(client)
    r = _subscribe(client, _auth(dev, secret), max_days_ahead=raw)
    assert r.status_code == 201, r.data
    assert r.get_json()["max_days_ahead"] == stored


def test_a_body_over_the_limit_is_refused(client):
    from app.api import MAX_BODY_BYTES
    r = client.post("/api/v1/devices", data=b"{" + b" " * MAX_BODY_BYTES + b"}",
                    content_type="application/json")
    assert r.status_code == 413 and r.get_json()["error"] == "too_large"
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


def test_error_message_follows_the_device_language(client):
    dev, secret = _register(client, language="en")
    r = _subscribe(client, _auth(dev, secret), appointment_type="svc-nope")
    assert "isn't offered" in r.get_json()["message"]


def test_a_special_category_service_needs_the_separate_consent(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    r = _subscribe(client, auth, city="muenster-standesamt", appointment_type=SBGG_SVC)
    assert r.status_code == 400 and r.get_json()["error"] == "consent_required"
    r = _subscribe(client, auth, city="muenster-standesamt", appointment_type=SBGG_SVC,
                   consent_special="yes")    # only a JSON true counts
    assert r.status_code == 400
    r = _subscribe(client, auth, city="muenster-standesamt", appointment_type=SBGG_SVC,
                   consent_special=True)
    assert r.status_code == 201
    body = r.get_json()
    assert body["consent_special"] is True
    # The shorter term, and the consent stamped as evidence.
    row = _db().execute("SELECT consent_special_at, expires_at FROM subscriptions "
                        "WHERE id=?", (body["id"],)).fetchone()
    assert row["consent_special_at"] is not None
    assert datetime.fromisoformat(row["expires_at"]) < datetime.utcnow() + timedelta(days=31)


def test_subscribe_counts_toward_the_city_plan_cap(client, monkeypatch):
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "1")
    c = create_app().test_client()
    dev, secret = _register(c)
    assert _subscribe(c, _auth(dev, secret)).status_code == 201
    r = _subscribe(c, _auth(dev, secret), appointment_type=LEIPZIG_SVC_2)
    assert r.status_code == 503 and r.get_json()["error"] == "waitlist_full"


def test_a_device_holds_a_bounded_number_of_subscriptions(client):
    dev, secret = _register(client)
    with patch("app.api.MAX_SUBSCRIPTIONS_PER_DEVICE", 2):
        assert _subscribe(client, _auth(dev, secret)).status_code == 201
        assert _subscribe(client, _auth(dev, secret)).status_code == 201
        r = _subscribe(client, _auth(dev, secret))
    assert r.status_code == 409
    assert r.get_json() == {"error": "too_many_subscriptions", "limit": 2}
    # A deleted one frees its place.
    sid = client.get("/api/v1/subscriptions", headers=_auth(dev, secret)).get_json()["subscriptions"][0]["id"]
    assert client.delete(f"/api/v1/subscriptions/{sid}", headers=_auth(dev, secret)).status_code == 204
    with patch("app.api.MAX_SUBSCRIPTIONS_PER_DEVICE", 2):
        assert _subscribe(client, _auth(dev, secret)).status_code == 201


def test_another_devices_subscription_is_not_found(client):
    a, sa = _register(client, token="a")
    b, sb = _register(client, token="b")
    sid = _subscribe(client, _auth(a, sa)).get_json()["id"]
    assert client.get("/api/v1/subscriptions", headers=_auth(b, sb)).get_json() == {"subscriptions": []}
    for method, path in (("get", f"/api/v1/subscriptions/{sid}"),
                         ("put", f"/api/v1/subscriptions/{sid}"),
                         ("delete", f"/api/v1/subscriptions/{sid}"),
                         ("post", f"/api/v1/subscriptions/{sid}/renew")):
        r = getattr(client, method)(path, json={}, headers=_auth(b, sb))
        assert r.status_code == 404, (method, path)
        assert r.get_json()["error"] == "not_found"
    assert active_subscriptions(_db())[0].id == sid     # untouched


def test_edit_replaces_the_filter_and_resets_the_cadence_state(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sid = _subscribe(client, auth).get_json()["id"]
    db = _db()
    db.execute("UPDATE subscriptions SET last_match_count=30, consecutive_digests=4 "
               "WHERE id=?", (sid,))
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"appointment_type": LEIPZIG_SVC_2, "locations": [LEIPZIG_LOC],
                         "weekdays": [6], "time_start": "09:00", "time_end": "10:00",
                         "max_days_ahead": 3, "language": "en"})
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["appointment_type"] == LEIPZIG_SVC_2 and body["locations"] == [LEIPZIG_LOC]
    assert body["weekdays"] == [6] and body["max_days_ahead"] == 3
    assert body["language"] == "en"
    row = db.execute("SELECT last_match_count, consecutive_digests FROM subscriptions "
                     "WHERE id=?", (sid,)).fetchone()
    assert row["last_match_count"] is None and row["consecutive_digests"] == 0
    # The same validation as a sign-up.
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"appointment_type": "svc-nope"})
    assert r.status_code == 400 and r.get_json()["error"] == "unknown_type"


def test_edit_into_and_out_of_a_special_category_service(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sid = _subscribe(client, auth, city="muenster-standesamt",
                     appointment_type="2434").get_json()["id"]
    db = _db()
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"appointment_type": SBGG_SVC})
    assert r.status_code == 400 and r.get_json()["error"] == "consent_required"
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"appointment_type": SBGG_SVC, "consent_special": True})
    assert r.status_code == 200 and r.get_json()["consent_special"] is True
    row = db.execute("SELECT consent_special_at, expires_at FROM subscriptions WHERE id=?",
                     (sid,)).fetchone()
    assert row["consent_special_at"] is not None
    assert datetime.fromisoformat(row["expires_at"]) < datetime.utcnow() + timedelta(days=31)
    # Back to an ordinary service: the consent covered that one selection.
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"appointment_type": "2434"})
    assert r.status_code == 200 and r.get_json()["consent_special"] is False
    assert db.execute("SELECT consent_special_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] is None


def test_edit_respects_the_plan_cap_minus_its_own_plan(client, monkeypatch):
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "1")
    c = create_app().test_client()
    dev, secret = _register(c)
    auth = _auth(dev, secret)
    sid = _subscribe(c, auth).get_json()["id"]
    # Replacing the only plan with another is fine: the old one goes away.
    r = c.put(f"/api/v1/subscriptions/{sid}", headers=auth,
              json={"appointment_type": LEIPZIG_SVC_2})
    assert r.status_code == 200
    # A second device cannot add a third plan next to it.
    dev2, secret2 = _register(c, token="other")
    r = _subscribe(c, _auth(dev2, secret2))
    assert r.status_code == 503


def test_delete_is_a_soft_delete_the_app_no_longer_sees(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sid = _subscribe(client, auth).get_json()["id"]
    assert client.delete(f"/api/v1/subscriptions/{sid}", headers=auth).status_code == 204
    assert client.get("/api/v1/subscriptions", headers=auth).get_json() == {"subscriptions": []}
    assert client.delete(f"/api/v1/subscriptions/{sid}", headers=auth).status_code == 404
    row = _db().execute("SELECT deleted_at FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["deleted_at"] is not None


def test_renew_starts_a_new_term_and_clears_the_checkin_latch(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sid = _subscribe(client, auth).get_json()["id"]
    db = _db()
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','-2 days'), "
               "reminder_sent_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
    # Expired is paused, not gone: still listed, inactive, renewable.
    listed = client.get("/api/v1/subscriptions", headers=auth).get_json()["subscriptions"]
    assert listed[0]["active"] is False
    r = client.post(f"/api/v1/subscriptions/{sid}/renew", json={}, headers=auth)
    assert r.status_code == 200 and r.get_json()["active"] is True
    row = db.execute("SELECT expires_at, reminder_sent_at FROM subscriptions WHERE id=?",
                     (sid,)).fetchone()
    assert row["reminder_sent_at"] is None
    assert datetime.fromisoformat(row["expires_at"]) > datetime.utcnow() + timedelta(days=89)


def test_renew_keeps_the_shorter_special_category_term(client):
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sid = _subscribe(client, auth, city="muenster-standesamt", appointment_type=SBGG_SVC,
                     consent_special=True).get_json()["id"]
    client.post(f"/api/v1/subscriptions/{sid}/renew", json={}, headers=auth)
    row = _db().execute("SELECT expires_at FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert datetime.fromisoformat(row["expires_at"]) < datetime.utcnow() + timedelta(days=31)


def test_the_website_renew_link_shares_the_helper(client):
    """The /renew route and the API renew the same way: a new term, the
    check-in latch cleared."""
    from app.repo import confirm, insert_pending
    from app.models import Filter
    from datetime import time
    from app.tokens import sign
    db = _db()
    sid = insert_pending(db, email="m@example.com", city="leipzig", language="de",
                         filter_=Filter(["A"], "all", [1], time(0, 0), time(23, 59)),
                         ttl_days=1)
    confirm(db, sid)
    db.execute("UPDATE subscriptions SET reminder_sent_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
    tok = sign(sid, "renew", primary="x" * 32, previous="")
    assert client.get(f"/renew/{tok}").status_code == 200
    row = db.execute("SELECT expires_at, reminder_sent_at FROM subscriptions WHERE id=?",
                     (sid,)).fetchone()
    assert row["reminder_sent_at"] is None
    assert datetime.fromisoformat(row["expires_at"]) > datetime.utcnow() + timedelta(days=89)


def test_api_closed_when_gate_unset(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("APP_API_ENABLED", raising=False)
    init_schema(connect(db_path))
    GLOBAL_IP_LIMITER._events.clear()
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    for r in (c.post("/api/v1/devices", json={"platform": "ios", "token": "t"}),
              c.get("/api/v1/cities"),
              c.get("/api/v1/subscriptions")):
        assert r.status_code == 404
        assert r.get_json() == {"error": "not_available"}
    assert c.get("/healthz").status_code == 200
    assert c.get("/leipzig").status_code == 200
