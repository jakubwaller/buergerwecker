"""The app's JSON API (app/api.py): registration and the per-device secret,
the catalog, and subscriptions under the website's rules. No network, no
relay: nothing here sends a push (the devices are verified straight in the
database; see tests/test_verification.py)."""
import hashlib
import json
import re
import time
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
    "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "600",
    "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
    "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m", "MAILJET_FROM_EMAIL": "x@x",
    "MAILJET_FROM_NAME": "x", "MAILJET_DAILY_QUOTA": "6000",
    "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
    "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
    "RENEWAL_REMINDER_DAYS_BEFORE": "10", "MAX_PLANS_PER_CITY": "10",
    "PARSER_CANARY_THRESHOLD_HOURS": "2", "DEVELOPER_EMAIL": "dev@x",
    "KOFI_URL": "https://k", "APP_API_ENABLED": "1",
    # Attestation has its own tests (test_integrity.py); these register devices.
    "PLAY_INTEGRITY_REQUIRED": "0",
    # Every test client is one address; the tests of the limits lower them.
    "MAX_UNVERIFIED_DEVICES_PER_IP_PER_HOUR": "999",
    "MAX_UNVERIFIED_DEVICES_PER_IP6_48_PER_HOUR": "999",
    "MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN": "999",
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


def _reg(c, name, addr=None):
    headers = {"X-Forwarded-For": addr} if addr else {}
    return c.post("/api/v1/devices", json={"platform": "apns", "token": tok(name)},
                  headers=headers)


def _device(c, name, addr):
    """A verified device registered from `addr`."""
    r = _reg(c, name, addr)
    assert r.status_code == 201, r.data
    body = r.get_json()
    _db().execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP WHERE id=?",
                  (body["device_id"],))
    return body["device_id"], body["secret"]


def _left_to_the_sweep(capsys):
    """The devices whose code a request left to the sweep, from the log."""
    return [int(d) for d in re.findall(
        r"api: device (\d+): its network is over", capsys.readouterr().out)]


def _ten_minutes_pass():
    _db().execute("UPDATE rate_events SET at=datetime(at, '-601 seconds')")


def test_token_requests_per_network_hold_across_four_workers_and_restarts(client, monkeypatch):
    """The bound on what one network can make the API do with push tokens
    is in the database: four gunicorn workers (each with its own per-process
    limiter) and a restart in between still let only the allowance through."""
    monkeypatch.setenv("MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN", "3")
    workers = [create_app().test_client() for _ in range(4)]
    answers = []
    for i in range(20):
        GLOBAL_IP_LIMITER._events.clear()            # nothing per process to lean on
        answers.append(_reg(workers[i % 4], f"w{i}", "192.0.2.40"))
    assert [a.status_code for a in answers].count(201) == 3
    refused = answers[-1]
    assert refused.status_code == 429 and refused.get_json()["error"] == "rate_limited"
    assert 0 < refused.get_json()["retry_after"] <= 600
    restarted = create_app().test_client()
    assert _reg(restarted, "after-restart", "192.0.2.40").status_code == 429
    # Reads are not counted: the app lists its subscriptions on every launch.
    assert restarted.get("/api/v1/cities").status_code == 200
    # Another network has its own count, and the table holds no address.
    assert _reg(restarted, "elsewhere", "198.51.100.40").status_code == 201
    buckets = [b for (b,) in _db().execute("SELECT bucket FROM rate_events")]
    assert not any("192.0.2" in b or "198.51" in b for b in buckets)
    # Ten minutes on, the network registers again: a stranger who filled the
    # count holds real phones off for that long, no longer.
    _ten_minutes_pass()
    assert _reg(restarted, "later", "192.0.2.40").status_code == 201


def test_a_devices_own_writes_count_per_device_not_per_network(client, monkeypatch):
    """Behind one carrier NAT, a stranger who used up the network's count
    kept everyone's code posts and alerts at 429 for an hour."""
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "3")     # writes per device
    monkeypatch.setenv("MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN", "1")
    c = create_app().test_client()
    mine, secret = _device(c, "mine", "192.0.2.80")
    nat = {"X-Forwarded-For": "192.0.2.80"}
    assert _reg(c, "stranger", "192.0.2.80").status_code == 429       # the NAT's count is spent
    auth = {**_auth(mine, secret), **nat}
    for _ in range(3):
        assert _subscribe(c, auth).status_code == 201                  # the device's own three
    assert _subscribe(c, auth).status_code == 429
    other, osecret = _device(c, "other", "192.0.2.81")
    assert _subscribe(c, {**_auth(other, osecret), **nat}).status_code == 201
    # Erasure never waits on a count.
    assert c.delete("/api/v1/device", headers=auth).status_code == 204
    keys = set(GLOBAL_IP_LIMITER._events)
    assert f"apidev:{mine}:main" in keys and not any(k.startswith("api:") for k in keys)


def test_token_requests_are_counted_by_ipv6_64_and_48(client, monkeypatch):
    monkeypatch.setenv("MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN", "1")    # 1 per /64, 10 per /48
    c = create_app().test_client()
    assert _reg(c, "v6-a", "2001:db8:1:2::1").status_code == 201
    assert _reg(c, "v6-b", "2001:db8:1:2:ffff::9").status_code == 429  # same /64
    assert _reg(c, "v6-c", "2001:db8:1:3::1").status_code == 201      # next /64
    codes = [_reg(c, f"v6-{i}", f"2001:db8:1:{i + 10:x}::1").status_code for i in range(10)]
    assert codes == [201] * 8 + [429] * 2                              # the /48's ten
    assert _reg(c, "v6-z", "2001:db8:2:1::1").status_code == 201      # another /48
    # IPv4 stays the full address, and an IPv4-mapped one counts as it.
    assert _reg(c, "v4-a", "192.0.2.1").status_code == 201
    assert _reg(c, "v4-b", "::ffff:192.0.2.1").status_code == 429
    assert _reg(c, "v4-c", "192.0.2.2").status_code == 201


def test_addresses_that_embed_an_ipv4_address_count_as_it(client, monkeypatch, capsys):
    """A 6to4 address carries its IPv4 address, and 2002:<v4>::/48 is 65,536
    /64s for whoever holds that one IPv4: counted by the /64, every one was a
    fresh count. Teredo names its client's IPv4 the same way."""
    monkeypatch.setenv("MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN", "1")
    monkeypatch.setenv("MAX_UNVERIFIED_DEVICES_PER_IP_PER_HOUR", "2")
    c = create_app().test_client()
    assert _reg(c, "e1", "192.0.2.1").status_code == 201
    assert _reg(c, "e2", "2002:c000:201:1::1").status_code == 429          # 6to4 of it
    assert _reg(c, "e3", "2001:0:4136:e378:8000:63bf:3fff:fdfe").status_code == 429  # Teredo
    assert _reg(c, "e4", "2002:c000:202::1").status_code == 201            # 6to4 of .2
    # The unverified-token count is shared the same way.
    capsys.readouterr()
    _ten_minutes_pass()
    assert _reg(c, "e5", "2002:c000:201:3::1").status_code == 201
    _ten_minutes_pass()
    r = _reg(c, "e6", "2002:c000:201:4::1")
    assert r.status_code == 201
    assert _left_to_the_sweep(capsys) == [r.get_json()["device_id"]]


def test_unverified_devices_per_network_hold_across_workers_and_never_refuse(
        client, monkeypatch, capsys):
    monkeypatch.setenv("MAX_UNVERIFIED_DEVICES_PER_IP_PER_HOUR", "2")
    c = create_app().test_client()
    nat = "192.0.2.10"
    n1 = _reg(c, "n1", nat).get_json()
    n2 = _reg(c, "n2", nat).get_json()
    # The per-process limiter forgets (another worker, a restart); the
    # database does not.
    GLOBAL_IP_LIMITER._events.clear()
    n3 = _reg(c, "n3", nat)
    assert n3.status_code == 201                       # never a refusal
    assert _left_to_the_sweep(capsys) == [n3.get_json()["device_id"]]
    # A device that verifies drops out of the count...
    _db().execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP WHERE id IN (?, ?)",
                  (n1["device_id"], n3.get_json()["device_id"]))
    n4 = _reg(c, "n4", nat).get_json()
    assert _left_to_the_sweep(capsys) == []
    # ...one deleted before it verified does not.
    assert c.delete("/api/v1/device", headers=_auth(n2["device_id"], n2["secret"])
                    ).status_code == 204
    n5 = _reg(c, "n5", nat).get_json()
    assert _left_to_the_sweep(capsys) == [n5["device_id"]]
    # A token that already has a row is no new token and is not counted, but
    # while the network is over, its push waits for the sweep all the same.
    def counted():
        return _db().execute("SELECT COUNT(*) FROM rate_events "
                             "WHERE device_id IS NOT NULL").fetchone()[0]
    before = counted()
    _reg(c, "n4", nat)
    assert _left_to_the_sweep(capsys) == [n4["device_id"]] and counted() == before
    # Another network has its own count, and the table holds no address.
    _reg(c, "n6", "198.51.100.7")
    assert _left_to_the_sweep(capsys) == []
    buckets = [r[0] for r in _db().execute("SELECT bucket FROM rate_events")]
    assert buckets and not any("192.0.2" in b or "198.51" in b for b in buckets)
    # The hour passes.
    _db().execute("UPDATE rate_events SET at=datetime('now','-61 minutes')")
    _reg(c, "n7", nat)
    assert _left_to_the_sweep(capsys) == []


def test_unverified_devices_are_also_counted_per_ipv6_48(client, monkeypatch, capsys):
    """A /56 or /48 delegation is 256 to 65,536 /64s, each with its own
    count: the /48 has a coarser one of its own."""
    monkeypatch.setenv("MAX_UNVERIFIED_DEVICES_PER_IP_PER_HOUR", "1")
    monkeypatch.setenv("MAX_UNVERIFIED_DEVICES_PER_IP6_48_PER_HOUR", "3")
    c = create_app().test_client()
    ids = [_reg(c, f"s{i}", f"2001:db8:5:{i}::1").get_json()["device_id"] for i in range(5)]
    assert _left_to_the_sweep(capsys) == ids[3:]       # fresh /64s, the /48 is full
    _reg(c, "s6", "2001:db8:6:1::1")                    # another /48
    _reg(c, "s7", "192.0.2.9")                          # IPv4 has none
    _reg(c, "s8", "2002:c000:20a::1")                   # 6to4: IPv4 too
    assert _left_to_the_sweep(capsys) == []
    buckets = [b for (b,) in _db().execute("SELECT bucket FROM rate_events")]
    assert not any("2001" in b or "192.0" in b for b in buckets)


@pytest.mark.parametrize("limit", ["0", "1"])
def test_the_ipv6_48_count_can_be_switched_off(client, monkeypatch, capsys, limit):
    monkeypatch.setenv("MAX_UNVERIFIED_DEVICES_PER_IP6_48_PER_HOUR", limit)
    c = create_app().test_client()
    ids = [_reg(c, f"o{i}", f"2001:db8:9:{i}::1").get_json()["device_id"] for i in range(3)]
    assert _left_to_the_sweep(capsys) == ([] if limit == "0" else ids[1:])


def test_deleting_a_device_is_counted_per_device_and_never_refused(client, monkeypatch):
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "1")
    c = create_app().test_client()
    dev, secret = _device(c, "del-1", "192.0.2.30")
    auth = _auth(dev, secret)
    assert _subscribe(c, auth).status_code == 201       # the device's one write this hour
    assert _subscribe(c, auth).status_code == 429
    assert c.delete("/api/v1/device", headers=auth).status_code == 204


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
    # A cap of 2 leaves the app a share of one service of its own.
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "2")
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
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "2")     # the app's share: one
    c = create_app().test_client()
    dev, secret = _register(c)
    auth = _auth(dev, secret)
    sid = _subscribe(c, auth).get_json()["id"]
    # Replacing the only plan with another is fine: the old one goes away.
    r = c.put(f"/api/v1/subscriptions/{sid}", headers=auth,
              json={"appointment_type": LEIPZIG_SVC_2})
    assert r.status_code == 200
    # A second device cannot add a second app-held service next to it.
    dev2, secret2 = _register(c, token="other")
    r = _subscribe(c, _auth(dev2, secret2))
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# The app's share of a city (security review 2026-10-07). Devices are free to
# mint, so every limit here is a database count, never a count of devices.

def _leipzig_services(n):
    from app.catalog import load_catalog
    return sorted(load_catalog("leipzig").appointment_types.values())[:n]


def _web_signup(client, svc, email, *, confirmed=True):
    """A website sign-up, through the form; confirmed in the database
    (the confirmation click is not what is under test)."""
    with patch("app.web._send_confirmation_email", return_value=True):
        r = client.post("/subscribe", data={
            "lang": "de", "city": "leipzig", "email": email,
            "appointment_type": svc, "all_locations": "1",
            "time_start": "00:00", "time_end": "23:59", "weekdays": ["1"],
            "website": ""})
    if r.status_code == 302 and confirmed:
        _db().execute("UPDATE subscriptions SET confirmed_at=CURRENT_TIMESTAMP "
                      "WHERE email=?", (email,))
    return r


def _app_client(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_devices_cannot_lock_the_website_out_of_a_city(client, monkeypatch):
    """The measured attack: verified devices took every service the cap
    allowed, and the website answered waitlist_full for any other. Now the
    app holds at most half the cap on its own, and mail is judged against
    mail-held services alone."""
    c = _app_client(monkeypatch, MAX_PLANS_PER_CITY="4")   # app share: 2
    svcs = _leipzig_services(7)
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    assert _subscribe(c, _auth(a, sa), appointment_type=svcs[0]).status_code == 201
    assert _subscribe(c, _auth(b, sb), appointment_type=svcs[1]).status_code == 201
    r = _subscribe(c, _auth(b, sb), appointment_type=svcs[2])
    assert r.status_code == 503 and r.get_json()["error"] == "waitlist_full"
    # Joining a service the app already holds costs no plan.
    assert _subscribe(c, _auth(b, sb), appointment_type=svcs[0]).status_code == 201
    # The website keeps its whole cap: four services of its own next to the
    # app's two, and only mail's own fifth is refused.
    for i, svc in enumerate(svcs[2:6]):
        assert _web_signup(c, svc, f"m{i}@example.com").status_code == 302, svc
    assert _web_signup(c, svcs[6], "m9@example.com").status_code == 503
    # A service the app holds is never refused to a mail subscriber.
    assert _web_signup(c, svcs[1], "m10@example.com").status_code == 302
    # With the city past its cap, the app gets no new plan, but may still
    # join a polled service.
    r = _subscribe(c, _auth(a, sa), appointment_type=svcs[6])
    assert r.status_code == 503
    assert _subscribe(c, _auth(a, sa), appointment_type=svcs[3]).status_code == 201


def test_a_device_watches_at_most_three_services_per_city(client, monkeypatch):
    from app.api import MAX_SERVICES_PER_DEVICE_PER_CITY
    assert MAX_SERVICES_PER_DEVICE_PER_CITY == 3
    # The three-subscriptions-a-city limit would answer first; lifted here so
    # the service count is what is under test.
    monkeypatch.setattr("app.api.MAX_SUBSCRIPTIONS_PER_DEVICE_PER_CITY", 10)
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    svcs = _leipzig_services(4)
    ids = [_subscribe(client, auth, appointment_type=s).get_json()["id"]
           for s in svcs[:3]]
    r = _subscribe(client, auth, appointment_type=svcs[3])
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "too_many_services" and body["limit"] == 3
    assert "höchstens 3" in body["message"]
    # Another subscription to one of its three is not a fourth service, and
    # another city is another count.
    assert _subscribe(client, auth, appointment_type=svcs[0],
                      locations=[LEIPZIG_LOC]).status_code == 201
    assert _subscribe(client, auth, city="muenster-standesamt",
                      appointment_type="2434").status_code == 201
    # An edit into a fourth service is refused; within its own, fine.
    r = client.put(f"/api/v1/subscriptions/{ids[0]}", headers=auth,
                   json={"appointment_type": svcs[3]})
    assert r.status_code == 409 and r.get_json()["error"] == "too_many_services"
    r = client.put(f"/api/v1/subscriptions/{ids[2]}", headers=auth,
                   json={"appointment_type": svcs[2], "weekdays": [2]})
    assert r.status_code == 200
    # Another device is not this device's count.
    other, so = _register(client, token="other")
    assert _subscribe(client, _auth(other, so), appointment_type=svcs[3]).status_code == 201
    # Deleting the last subscription to a service frees its place.
    assert client.delete(f"/api/v1/subscriptions/{ids[1]}", headers=auth).status_code == 204
    assert _subscribe(client, auth, appointment_type=svcs[3]).status_code == 201


def test_the_city_ceiling_turns_the_app_away_and_only_the_app(client, monkeypatch):
    c = _app_client(monkeypatch, MAX_APP_SUBSCRIPTIONS_PER_CITY="2")
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    first = _subscribe(c, _auth(a, sa)).get_json()["id"]
    assert _subscribe(c, _auth(a, sa)).status_code == 201
    r = _subscribe(c, _auth(b, sb))
    assert r.status_code == 503 and r.get_json()["error"] == "waitlist_full"
    # Per city: another one is free.
    assert _subscribe(c, _auth(b, sb), city="muenster-standesamt",
                      appointment_type="2434").status_code == 201
    # The website never sees the app's count.
    assert _web_signup(c, LEIPZIG_SVC, "m@example.com").status_code == 302
    # A paused subscription (its device changed token and has not verified
    # again) still counts: it resumes the moment the device verifies.
    db = _db()
    db.execute("UPDATE push_devices SET verified_at=NULL WHERE id=?", (a,))
    db.execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP WHERE id=?", (b,))
    assert _subscribe(c, _auth(b, sb)).status_code == 503
    # An expired one does not (it comes back only through /renew).
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') "
               "WHERE id=?", (first,))
    assert _subscribe(c, _auth(b, sb)).status_code == 201


@pytest.mark.parametrize("ceiling, second", [("1", 503), ("0", 201)])
def test_zero_turns_the_city_ceiling_off(client, monkeypatch, ceiling, second):
    c = _app_client(monkeypatch, MAX_APP_SUBSCRIPTIONS_PER_CITY=ceiling)
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    assert _subscribe(c, _auth(a, sa)).status_code == 201
    assert _subscribe(c, _auth(b, sb)).status_code == second


def test_a_device_holds_at_most_three_subscriptions_per_city(client):
    """The city ceiling is shared by every device. At ten a device, ten free
    devices filled a city's hundred places and every other app user got
    waitlist_full there; at three a city takes thirty-four."""
    from app.api import MAX_SUBSCRIPTIONS_PER_DEVICE_PER_CITY
    assert MAX_SUBSCRIPTIONS_PER_DEVICE_PER_CITY == 3
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    ids = [_subscribe(client, auth, weekdays=[d]).get_json()["id"] for d in (1, 2, 3)]
    r = _subscribe(client, auth, weekdays=[4])
    assert r.status_code == 409
    body = r.get_json()
    assert body["error"] == "too_many_in_city" and body["limit"] == 3
    assert "höchstens 3" in body["message"]
    # Another city is another count; an edit adds nothing; a renewal is not
    # counted against itself, an expired one included.
    assert _subscribe(client, auth, city="muenster-standesamt",
                      appointment_type="2434").status_code == 201
    assert client.put(f"/api/v1/subscriptions/{ids[0]}", headers=auth,
                      json={"appointment_type": LEIPZIG_SVC, "weekdays": [5]}).status_code == 200
    _db().execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') "
                  "WHERE id=?", (ids[1],))
    assert client.post(f"/api/v1/subscriptions/{ids[1]}/renew", json={},
                       headers=auth).status_code == 200
    # An expired one still holds its place (it is renewable); a deleted one
    # frees it.
    assert _subscribe(client, auth, weekdays=[4]).status_code == 409
    assert client.delete(f"/api/v1/subscriptions/{ids[2]}", headers=auth).status_code == 204
    assert _subscribe(client, auth, weekdays=[4]).status_code == 201


def test_renew_is_judged_like_a_sign_up(client, monkeypatch):
    """One renew per term kept a place the app could no longer take. A new
    term is judged like a sign-up now, leaving the subscription itself out;
    refused, it keeps the term it has."""
    c = _app_client(monkeypatch, MAX_APP_SUBSCRIPTIONS_PER_CITY="1")
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    sid = _subscribe(c, _auth(a, sa)).get_json()["id"]
    # Alone in the city it renews: it is not counted against itself.
    assert c.post(f"/api/v1/subscriptions/{sid}/renew", json={},
                  headers=_auth(a, sa)).status_code == 200
    db = _db()
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') "
               "WHERE id=?", (sid,))
    assert _subscribe(c, _auth(b, sb)).status_code == 201     # took the place
    before = db.execute("SELECT expires_at FROM subscriptions WHERE id=?",
                        (sid,)).fetchone()[0]
    r = c.post(f"/api/v1/subscriptions/{sid}/renew", json={}, headers=_auth(a, sa))
    assert r.status_code == 503 and r.get_json()["error"] == "waitlist_full"
    assert db.execute("SELECT expires_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] == before


def test_app_held_services_past_the_share_are_not_renewed(client, monkeypatch):
    """A service turns app-held when its last mail subscriber leaves, past
    the share the app could take itself. Renewals on app-held services are
    refused until the share is kept again; mail notices nothing."""
    c = _app_client(monkeypatch, MAX_PLANS_PER_CITY="2")   # app share: 1
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    assert _web_signup(c, LEIPZIG_SVC, "m@example.com").status_code == 302
    held = _subscribe(c, _auth(a, sa), appointment_type=LEIPZIG_SVC_2).get_json()["id"]
    # Joining the mail-held service is always fine for the app.
    joined = _subscribe(c, _auth(b, sb), appointment_type=LEIPZIG_SVC).get_json()["id"]
    for sid, (dev, sec) in ((held, (a, sa)), (joined, (b, sb))):
        assert c.post(f"/api/v1/subscriptions/{sid}/renew", json={},
                      headers=_auth(dev, sec)).status_code == 200
    _db().execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP "
                  "WHERE email='m@example.com'")
    # Two app-held services against a share of one: neither renews.
    for sid, (dev, sec) in ((held, (a, sa)), (joined, (b, sb))):
        r = c.post(f"/api/v1/subscriptions/{sid}/renew", json={}, headers=_auth(dev, sec))
        assert r.status_code == 503, sid
    # The website still has its whole cap.
    assert _web_signup(c, _leipzig_services(15)[-1], "n@example.com").status_code == 302


def test_a_city_above_its_cap_still_lets_its_subscribers_edit_and_renew(client, monkeypatch):
    """The app took its half first, mail then filled its whole cap: six
    services polled against a cap of four, as designed. An edit or renewal
    leaves the subscription itself out, and its own service then looked
    unpolled and new, so 5 + 1 > 4 refused the app user who came first."""
    c = _app_client(monkeypatch, MAX_PLANS_PER_CITY="4")     # app share: 2
    svcs = _leipzig_services(6)
    a, sa = _register(c, token="a")
    sid = _subscribe(c, _auth(a, sa), appointment_type=svcs[0]).get_json()["id"]
    assert _subscribe(c, _auth(a, sa), appointment_type=svcs[1]).status_code == 201
    for i, svc in enumerate(svcs[2:]):
        assert _web_signup(c, svc, f"m{i}@example.com").status_code == 302
    body = {"city": "leipzig", "appointment_type": svcs[0], "locations": "all",
            "weekdays": [1, 2], "time_start": "08:00", "time_end": "12:00"}
    r = c.put(f"/api/v1/subscriptions/{sid}", headers=_auth(a, sa), json=body)
    assert r.status_code == 200, r.get_json()
    assert c.post(f"/api/v1/subscriptions/{sid}/renew", json={},
                  headers=_auth(a, sa)).status_code == 200
    # Moving to a service nobody polls is still judged like a sign-up.
    r = c.put(f"/api/v1/subscriptions/{sid}", headers=_auth(a, sa),
              json={**body, "appointment_type": _leipzig_services(7)[-1]})
    assert r.status_code == 503


def test_conversions_do_not_let_a_city_grow_past_cap_and_a_half(client, monkeypatch):
    """Mail fills its cap, devices join every service, mail leaves: the
    services are app-held now, past the app's half, and they count against
    mail until they drain. Before, mail could fill a whole new cap every
    round."""
    from app.repo import city_services
    c = _app_client(monkeypatch, MAX_PLANS_PER_CITY="4")     # app share: 2
    svcs = _leipzig_services(9)
    for i, svc in enumerate(svcs[:4]):
        assert _web_signup(c, svc, f"m{i}@example.com").status_code == 302
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    for (dev, sec), svc in zip([(a, sa)] * 3 + [(b, sb)], svcs[:4]):
        assert _subscribe(c, _auth(dev, sec), appointment_type=svc).status_code == 201
    _db().execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE email<>''")
    # Four app-held against a share of two: mail has 4 - 2 places left.
    assert _web_signup(c, svcs[4], "n1@example.com").status_code == 302
    assert _web_signup(c, svcs[5], "n2@example.com").status_code == 302
    assert _web_signup(c, svcs[6], "n3@example.com").status_code == 503
    mail, app = city_services(_db(), "leipzig")
    assert len(mail | app) == 6                               # cap + cap // 2


def test_two_workers_racing_for_the_last_place_get_201_and_503(client, monkeypatch):
    """Check-then-insert runs under BEGIN IMMEDIATE: the second request waits
    for the first and then counts its row. Under a plain BEGIN both passed
    the check and the loser failed with "database is locked", a 500."""
    import threading
    from app import api
    c = _app_client(monkeypatch, MAX_APP_SUBSCRIPTIONS_PER_CITY="1")
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    gate = threading.Barrier(2, timeout=1)
    real = api.app_subscriptions_in_city

    def both_at_the_check(*args, **kw):
        try:
            gate.wait()          # both requests between check and insert
        except threading.BrokenBarrierError:
            pass                 # serialised: the other one already finished
        return real(*args, **kw)

    monkeypatch.setattr(api, "app_subscriptions_in_city", both_at_the_check)
    results: list = []

    def subscribe(dev, sec):
        try:
            results.append(_subscribe(c.application.test_client(), _auth(dev, sec)).status_code)
        except Exception as exc:          # TESTING propagates a 500's cause
            results.append(repr(exc))

    threads = [threading.Thread(target=subscribe, args=x) for x in ((a, sa), (b, sb))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results, key=str) == [201, 503]
    assert _db().execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 1


def test_an_edit_cannot_move_a_subscription_to_another_city(client):
    """The city is the one the subscription was made in; a `city` in the
    body is not read, so no edit escapes the checks of the city it lands in."""
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sid = _subscribe(client, auth).get_json()["id"]
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"city": "muenster-standesamt", "appointment_type": "2434"})
    assert r.status_code == 400 and r.get_json()["error"] == "unknown_type"
    r = client.put(f"/api/v1/subscriptions/{sid}", headers=auth,
                   json={"city": "muenster-standesamt", "appointment_type": LEIPZIG_SVC_2})
    assert r.status_code == 200 and r.get_json()["city"] == "leipzig"


def test_a_paused_devices_service_still_holds_the_app_share(client, monkeypatch):
    """A device that changed its token pauses its subscriptions, and they
    resume the moment it verifies, without a check: so they keep counting,
    or devices could take turns pausing to stack the share."""
    c = _app_client(monkeypatch, MAX_PLANS_PER_CITY="2")     # app share: 1
    a, sa = _register(c, token="a")
    b, sb = _register(c, token="b")
    assert _subscribe(c, _auth(a, sa), appointment_type=LEIPZIG_SVC).status_code == 201
    _db().execute("UPDATE push_devices SET verified_at=NULL WHERE id=?", (a,))
    r = _subscribe(c, _auth(b, sb), appointment_type=LEIPZIG_SVC_2)
    assert r.status_code == 503 and r.get_json()["error"] == "waitlist_full"
    # Joining the service it holds is fine: it adds no plan.
    assert _subscribe(c, _auth(b, sb), appointment_type=LEIPZIG_SVC).status_code == 201


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
