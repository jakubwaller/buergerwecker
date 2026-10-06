"""The app's JSON API (app/api.py): registration and the per-device secret,
the catalog, and subscriptions under the website's rules. No network, no
relay: nothing here sends a push."""
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
    "KOFI_URL": "https://k",
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


def _register(client, platform="apns", token="tok-1", language="de"):
    r = client.post("/api/v1/devices", json={"platform": platform,
                                             "token": token, "language": language})
    assert r.status_code == 201, r.data
    body = r.get_json()
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
    assert row["platform"] == "apns" and row["token"] == "tok-1"
    assert row["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(dict(row))
    r = client.get("/api/v1/device", headers=_auth(dev, secret))
    assert r.status_code == 200
    assert r.get_json() == {"device_id": dev, "platform": "apns", "language": "de",
                            "created_at": row["created_at"], "subscriptions": []}


def test_the_same_token_registering_again_is_the_same_device_with_a_new_secret(client):
    dev, old = _register(client)
    assert _subscribe(client, _auth(dev, old)).status_code == 201
    dev2, new = _register(client)
    assert dev2 == dev and new != old
    assert client.get("/api/v1/device", headers=_auth(dev, old)).status_code == 401
    r = client.get("/api/v1/device", headers=_auth(dev, new))
    assert r.status_code == 200
    assert len(r.get_json()["subscriptions"]) == 1   # kept across the re-registration


@pytest.mark.parametrize("body, error", [
    ({"platform": "sms", "token": "t"}, "unknown_platform"),
    ({"platform": "fcm", "token": ""}, "invalid_push_token"),
    ({"platform": "fcm", "token": "has space"}, "invalid_push_token"),
    ({}, "unknown_platform"),
])
def test_register_rejects_a_bad_platform_or_token(client, body, error):
    r = client.post("/api/v1/devices", json=body)
    assert r.status_code == 400 and r.get_json()["error"] == error


def test_register_accepts_a_body_that_is_not_json_as_empty(client):
    r = client.post("/api/v1/devices", data="not json",
                    content_type="text/plain")
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
    retire_device(_db(), dev, "Unregistered")
    r = client.get("/api/v1/subscriptions", headers=_auth(dev, secret))
    assert r.status_code == 410 and r.get_json()["error"] == "device_retired"
    # Registering again revives it, and the new secret works.
    dev2, new = _register(client)
    assert dev2 == dev
    assert client.get("/api/v1/subscriptions", headers=_auth(dev, new)).status_code == 200


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
    assert c.post("/api/v1/devices", json={"platform": "apns", "token": "a"}).status_code == 201
    assert c.post("/api/v1/devices", json={"platform": "apns", "token": "b"}).status_code == 201
    r = c.post("/api/v1/devices", json={"platform": "apns", "token": "c"})
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    # Reads are not counted: the app lists its subscriptions on every launch.
    assert c.get("/api/v1/cities").status_code == 200
    # One budget per address across the form and the API.
    assert c.post("/subscribe", data={"email": "a@example.com", "city": "leipzig",
                                      "appointment_type": LEIPZIG_SVC}).status_code == 429


# ---------------------------------------------------------------------------
# The device

def test_device_update_rotates_the_token_and_changes_the_language(client):
    dev, secret = _register(client)
    db = _db()
    db.execute("UPDATE push_devices SET dead_since=CURRENT_TIMESTAMP WHERE id=?", (dev,))
    r = client.put("/api/v1/device", json={"token": "tok-2", "language": "en"},
                   headers=_auth(dev, secret))
    assert r.status_code == 200 and r.get_json()["language"] == "en"
    row = db.execute("SELECT * FROM push_devices WHERE id=?", (dev,)).fetchone()
    assert row["token"] == "tok-2" and row["language"] == "en"
    assert row["dead_since"] is None     # a new token: the evidence clock starts over
    assert client.put("/api/v1/device", json={"language": "fr"},
                      headers=_auth(dev, secret)).status_code == 400
    assert client.put("/api/v1/device", json={"token": ""},
                      headers=_auth(dev, secret)).status_code == 400


def test_device_update_refuses_a_token_another_row_holds(client):
    dev, secret = _register(client, token="tok-1")
    _register(client, token="tok-2")
    r = client.put("/api/v1/device", json={"token": "tok-2"}, headers=_auth(dev, secret))
    assert r.status_code == 409 and r.get_json()["error"] == "token_in_use"


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
    body = client.get("/api/v1/cities/leipzig/slots").get_json()
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
    body = client.get(f"/api/v1/cities/leipzig/slots?service={LEIPZIG_SVC_2}&lang=en").get_json()
    assert [s["id"] for s in body["services"]] == [LEIPZIG_SVC_2]
    # A city nobody watches has nothing to show, and says so.
    assert client.get("/api/v1/cities/bonn/slots").get_json() == {
        "slug": "bonn", "polled_at": None, "services": []}
    assert client.get("/api/v1/cities/atlantis/slots").status_code == 404


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
    r = client.post(f"/api/v1/subscriptions/{sid}/renew", headers=auth)
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
    client.post(f"/api/v1/subscriptions/{sid}/renew", headers=auth)
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
