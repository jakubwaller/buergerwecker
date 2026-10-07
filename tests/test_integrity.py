"""Play Integrity attestation of Android registrations (app/integrity.py and
its enforcement in app/api.py). No network: every request to Google leaves
through `app.push._post`, which these tests replace."""
import hashlib
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import push
from app.db import connect, init_schema
from app.integrity import PACKAGE_NAME, verify_play_integrity
from app.ratelimit import GLOBAL_IP_LIMITER
from app.web import create_app

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RSA_PEM = _KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
ACCOUNT = json.dumps({
    "project_id": "bw-test", "client_email": "sa@example.com",
    "token_uri": "https://oauth2.example.com/token", "private_key": RSA_PEM})

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
    "MAX_UNVERIFIED_DEVICES_PER_IP_PER_HOUR": "999",
    "MAX_UNVERIFIED_DEVICES_PER_IP6_48_PER_HOUR": "999",
    "MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN": "999",
    "PLAY_INTEGRITY_REQUIRED": "1",
    "FCM_SERVICE_ACCOUNT_JSON": ACCOUNT,
}


JWE = "aaa.bbb.ccc.ddd.eee"      # shaped like a compact JWE; never decoded here


def fcm_tok(name):
    digest = hashlib.sha256(name.encode()).hexdigest()
    return f"{digest[:22]}:APA91b{hashlib.sha512(name.encode()).hexdigest()}"


def apns_tok(name):
    return hashlib.sha256(name.encode()).hexdigest()


def verdict(token, **over):
    """A passing `tokenPayloadExternal` for `token`; `over` replaces whole
    sections to break one check at a time."""
    payload = {
        "requestDetails": {
            "requestPackageName": PACKAGE_NAME,
            "requestHash": hashlib.sha256(token.encode()).hexdigest(),
            "timestampMillis": str(int(time.time() * 1000))},
        "appIntegrity": {"appRecognitionVerdict": "PLAY_RECOGNIZED",
                         "packageName": PACKAGE_NAME},
        "deviceIntegrity": {"deviceRecognitionVerdict": ["MEETS_DEVICE_INTEGRITY"]},
        "accountDetails": {"appLicensingVerdict": "UNEVALUATED"},
    }
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(payload.get(key), dict):
            payload[key] = {**payload[key], **val}
        else:
            payload[key] = val
    return payload


class FakeGoogle:
    """Stands in for `push._post`: the OAuth exchange and decodeIntegrityToken.
    `decode` is the (status, body) of the decode call, or an exception to raise."""

    def __init__(self, decode=None, token_status=200):
        self.decode = decode
        self.token_status = token_status
        self.decode_calls: list[dict] = []

    def __call__(self, platform, url, *, headers=None, json=None, data=None):
        if data is not None and "assertion" in data:
            return httpx.Response(self.token_status,
                                  json={"access_token": "ya29.test", "expires_in": 3600})
        if "playintegrity" not in url:       # the verification push to FCM
            return httpx.Response(200, json={"name": "projects/bw-test/messages/1"})
        self.decode_calls.append(dict(url=url, headers=headers, json=json))
        if isinstance(self.decode, Exception):
            raise self.decode
        status, body = self.decode
        return httpx.Response(status, json=body)


def passing(token):
    return FakeGoogle((200, {"tokenPayloadExternal": verdict(token)}))


@pytest.fixture(autouse=True)
def _fresh():
    push._fcm_token.clear()
    push._clients.clear()
    yield
    push._fcm_token.clear()
    push._clients.clear()


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def make(**env):
        db_path = str(tmp_path / "t.db")
        monkeypatch.setenv("DB_PATH", db_path)
        for k, v in {**_ENV, **env}.items():
            monkeypatch.setenv(k, v)
        init_schema(connect(db_path))
        GLOBAL_IP_LIMITER._events.clear()
        app = create_app()
        app.config["TESTING"] = True
        return app.test_client()
    return make


@pytest.fixture
def client(make_client):
    return make_client()


def _post(client, token, platform="fcm", **extra):
    return client.post("/api/v1/devices",
                       json={"platform": platform, "token": token, **extra})


def _devices():
    return connect(os.environ["DB_PATH"]).execute(
        "SELECT COUNT(*) FROM push_devices").fetchone()[0]


# --- verify_play_integrity itself ------------------------------------------

def _cfg(**over):
    base = dict(play_integrity_service_account_json=ACCOUNT)
    base.update(over)
    return SimpleNamespace(**base)


def _verify(google, token="T" * 100, integrity=JWE):
    with patch("app.push._post", google):
        return verify_play_integrity(_cfg(), integrity, token)


def test_a_passing_verdict_returns_none_and_calls_google_as_documented():
    t = fcm_tok("a")
    google = passing(t)
    assert _verify(google, t) is None
    call = google.decode_calls[0]
    assert call["url"] == ("https://playintegrity.googleapis.com/v1/"
                           "de.buergerwecker.app:decodeIntegrityToken")
    assert call["json"] == {"integrity_token": JWE}
    assert call["headers"]["authorization"] == "Bearer ya29.test"


@pytest.mark.parametrize("token", [None, "", "   ", 5, ["x"], {"a": 1}])
def test_a_missing_or_non_string_token_is_missing(token):
    google = passing("x")
    with patch("app.push._post", google):
        assert verify_play_integrity(_cfg(), token, "x") == "integrity_missing"
    assert google.decode_calls == []


@pytest.mark.parametrize("name,override", [
    ("wrong package in the request", {"requestDetails": {"requestPackageName": "com.evil"}}),
    ("not play recognised", {"appIntegrity": {"appRecognitionVerdict": "UNRECOGNIZED_VERSION"}}),
    ("wrong package in the app verdict", {"appIntegrity": {"packageName": "com.evil"}}),
    ("no device integrity", {"deviceIntegrity": {"deviceRecognitionVerdict": ["MEETS_BASIC_INTEGRITY"]}}),
    ("empty device verdict", {"deviceIntegrity": {"deviceRecognitionVerdict": []}}),
    ("no app integrity at all", {"appIntegrity": None}),
])
def test_each_verdict_check_fails_on_its_own(name, override):
    t = fcm_tok("a")
    google = FakeGoogle((200, {"tokenPayloadExternal": verdict(t, **override)}))
    assert _verify(google, t) == "integrity_failed", name


def test_a_request_hash_for_another_token_fails():
    google = passing(fcm_tok("other"))
    assert _verify(google, fcm_tok("a")) == "integrity_failed"


def test_a_stale_timestamp_fails_and_a_slightly_old_one_passes():
    t = fcm_tok("a")
    for age_s, expected in ((11 * 60, "integrity_failed"), (9 * 60, None)):
        ts = str(int((time.time() - age_s) * 1000))
        google = FakeGoogle((200, {"tokenPayloadExternal": verdict(
            t, requestDetails={"timestampMillis": ts})}))
        assert _verify(google, t) == expected


def test_a_future_timestamp_fails_beyond_a_minute_of_skew():
    t = fcm_tok("a")
    for ahead_s, expected in ((5 * 60, "integrity_failed"), (20, None)):
        ts = str(int((time.time() + ahead_s) * 1000))
        google = FakeGoogle((200, {"tokenPayloadExternal": verdict(
            t, requestDetails={"timestampMillis": ts})}))
        assert _verify(google, t) == expected


def test_the_licensing_verdict_is_not_required():
    t = fcm_tok("a")
    google = FakeGoogle((200, {"tokenPayloadExternal": verdict(
        t, accountDetails={"appLicensingVerdict": "UNLICENSED"})}))
    assert _verify(google, t) is None


@pytest.mark.parametrize("body", [{}, {"tokenPayloadExternal": "x"},
                                  {"tokenPayloadExternal": {"requestDetails": 3}}])
def test_a_malformed_decode_answer_is_unavailable_or_failed_never_a_pass(body):
    assert _verify(FakeGoogle((200, body))) in ("integrity_unavailable", "integrity_failed")


def test_google_400_for_a_bad_token_is_failed():
    assert _verify(FakeGoogle((400, {"error": {}}))) == "integrity_failed"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_other_google_statuses_are_unavailable(status):
    assert _verify(FakeGoogle((status, {}))) == "integrity_unavailable"


def test_a_timeout_and_a_connection_error_are_unavailable():
    assert _verify(FakeGoogle(httpx.ReadTimeout("slow"))) == "integrity_unavailable"
    assert _verify(FakeGoogle(httpx.ConnectError("down"))) == "integrity_unavailable"


def test_a_failed_oauth_exchange_is_unavailable():
    google = FakeGoogle((200, {}), token_status=401)
    assert _verify(google) == "integrity_unavailable"
    assert google.decode_calls == []


def test_no_credentials_is_unavailable_without_calling_google():
    google = passing("x")
    with patch("app.push._post", google):
        assert verify_play_integrity(_cfg(play_integrity_service_account_json=""),
                                     JWE, "x") == "integrity_unavailable"
    assert google.decode_calls == []


def test_unparsable_credentials_are_unavailable():
    with patch("app.push._post", passing("x")):
        assert verify_play_integrity(
            _cfg(play_integrity_service_account_json="{not json"),
            JWE, "x") == "integrity_unavailable"


# --- config ----------------------------------------------------------------

def test_the_fcm_account_is_the_fallback_and_required_defaults_on(monkeypatch):
    from app.config import load_config
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PLAY_INTEGRITY_REQUIRED")
    cfg = load_config()
    assert cfg.play_integrity_required is True
    assert cfg.play_integrity_service_account_json == ACCOUNT
    monkeypatch.setenv("PLAY_INTEGRITY_SERVICE_ACCOUNT_JSON", '{"own": 1}')
    assert load_config().play_integrity_service_account_json == '{"own": 1}'
    monkeypatch.setenv("PLAY_INTEGRITY_REQUIRED", "0")
    assert load_config().play_integrity_required is False


# --- POST /devices ---------------------------------------------------------

def test_a_passing_verdict_registers(client):
    t = fcm_tok("a")
    with patch("app.push._post", passing(t)) as google:
        r = _post(client, t, integrity_token=JWE)
    assert r.status_code == 201
    assert _devices() == 1
    assert len(google.decode_calls) == 1


@pytest.mark.parametrize("decode,status,key", [
    ((200, {"tokenPayloadExternal": {}}), 403, "integrity_failed"),
    ((400, {}), 403, "integrity_failed"),
    ((500, {}), 503, "integrity_unavailable"),
    (httpx.ReadTimeout("slow"), 503, "integrity_unavailable"),
])
def test_a_refused_verdict_registers_nothing(client, decode, status, key):
    with patch("app.push._post", FakeGoogle(decode)):
        r = _post(client, fcm_tok("a"), integrity_token=JWE, language="en")
    assert r.status_code == status
    assert r.get_json()["error"] == key
    assert r.get_json()["message"]
    assert _devices() == 0


def test_a_missing_token_is_400_and_never_calls_google(client):
    google = passing("x")
    with patch("app.push._post", google):
        r = _post(client, fcm_tok("a"))
    assert r.status_code == 400 and r.get_json()["error"] == "integrity_missing"
    assert google.decode_calls == [] and _devices() == 0


def test_no_credentials_fails_closed_over_http(make_client):
    client = make_client(FCM_SERVICE_ACCOUNT_JSON="")
    r = _post(client, fcm_tok("a"), integrity_token=JWE)
    assert r.status_code == 503 and r.get_json()["error"] == "integrity_unavailable"
    assert _devices() == 0


def test_required_zero_skips_the_check(make_client):
    client = make_client(PLAY_INTEGRITY_REQUIRED="0")
    google = passing("x")
    with patch("app.push._post", google):
        r = _post(client, fcm_tok("a"))
    assert r.status_code == 201 and google.decode_calls == []


def test_apns_registration_is_not_checked(client):
    google = passing("x")
    with patch("app.push._post", google):
        r = _post(client, apns_tok("a"), platform="apns")
    assert r.status_code == 201 and google.decode_calls == []


def test_a_known_token_registering_again_is_not_checked(client):
    t = fcm_tok("a")
    with patch("app.push._post", passing(t)):
        assert _post(client, t, integrity_token=JWE).status_code == 201
    google = passing(t)
    with patch("app.push._post", google):
        r = _post(client, t)
    assert r.status_code == 201 and google.decode_calls == []
    assert _devices() == 1


def test_no_google_call_when_the_gate_refuses(make_client):
    client = make_client(MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN="1")
    t = fcm_tok("a")
    with patch("app.push._post", passing(t)):
        assert _post(client, t, integrity_token=JWE).status_code == 201
    google = passing(fcm_tok("b"))
    with patch("app.push._post", google):
        r = _post(client, fcm_tok("b"), integrity_token=JWE)
    assert r.status_code == 429 and google.decode_calls == []


@pytest.mark.parametrize("payload", [
    {"platform": "fcm", "token": "has space"},
    {"platform": "fcm", "token": "a" * 63},
    {"platform": "fcm", "token": 7},
    {"platform": "windows", "token": "x" * 100},
])
def test_no_google_call_for_an_invalid_request(client, payload):
    google = passing("x")
    with patch("app.push._post", google):
        r = client.post("/api/v1/devices", json={**payload, "integrity_token": JWE})
    assert r.status_code == 400 and google.decode_calls == []


def test_a_verdict_for_one_token_cannot_register_another(client):
    google = passing(fcm_tok("minted-for-this-one"))
    with patch("app.push._post", google):
        r = _post(client, fcm_tok("scripted"), integrity_token=JWE)
    assert r.status_code == 403 and _devices() == 0


# --- PUT /device -----------------------------------------------------------

def _registered(client, name="a"):
    t = fcm_tok(name)
    with patch("app.push._post", passing(t)):
        body = _post(client, t, integrity_token=JWE).get_json()
    return {"Authorization": f"Bearer {body['device_id']}.{body['secret']}"}


def test_a_new_fcm_token_on_put_is_checked_against_the_new_token(client):
    auth = _registered(client)
    new = fcm_tok("rotated")
    google = passing(new)
    with patch("app.push._post", google):
        r = client.put("/api/v1/device", headers=auth,
                       json={"token": new, "integrity_token": JWE})
    assert r.status_code == 200 and len(google.decode_calls) == 1


def test_put_with_a_verdict_for_the_old_token_is_refused(client):
    auth = _registered(client)
    with patch("app.push._post", passing(fcm_tok("a"))):
        r = client.put("/api/v1/device", headers=auth,
                       json={"token": fcm_tok("scripted"), "integrity_token": JWE})
    assert r.status_code == 403
    row = connect(os.environ["DB_PATH"]).execute("SELECT token FROM push_devices").fetchone()
    assert row["token"] == fcm_tok("a")


def test_put_with_a_new_token_and_no_integrity_token_is_400(client):
    auth = _registered(client)
    google = passing("x")
    with patch("app.push._post", google):
        r = client.put("/api/v1/device", headers=auth, json={"token": fcm_tok("b")})
    assert r.status_code == 400 and r.get_json()["error"] == "integrity_missing"
    assert google.decode_calls == []


def test_a_language_only_put_and_the_same_token_are_not_checked(client):
    auth = _registered(client)
    google = passing("x")
    with patch("app.push._post", google):
        assert client.put("/api/v1/device", headers=auth,
                          json={"language": "en"}).status_code == 200
        assert client.put("/api/v1/device", headers=auth,
                          json={"token": fcm_tok("a")}).status_code == 200
    assert google.decode_calls == []


def test_no_google_call_on_put_for_an_invalid_token_or_a_refused_gate(make_client):
    client = make_client(MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN="1")
    auth = _registered(client)               # uses the one allowed gate pass
    google = passing("x")
    with patch("app.push._post", google):
        bad = client.put("/api/v1/device", headers=auth,
                         json={"token": "has space", "integrity_token": JWE})
        gated = client.put("/api/v1/device", headers=auth,
                           json={"token": fcm_tok("b"), "integrity_token": JWE})
    assert bad.status_code == 400
    assert gated.status_code == 429
    assert google.decode_calls == []


def test_put_on_an_apns_device_is_not_checked(client):
    r = _post(client, apns_tok("a"), platform="apns")
    body = r.get_json()
    auth = {"Authorization": f"Bearer {body['device_id']}.{body['secret']}"}
    google = passing("x")
    with patch("app.push._post", google):
        r = client.put("/api/v1/device", headers=auth, json={"token": apns_tok("b")})
    assert r.status_code == 200 and google.decode_calls == []


def test_a_registration_body_with_a_large_integrity_token_is_accepted(client):
    t = fcm_tok("a")
    with patch("app.push._post", passing(t)):
        r = _post(client, t, integrity_token="j" * 20000 + ".b.c.d.e")
    assert r.status_code == 201


# --- credentials that are not what they should be ---------------------------

@pytest.mark.parametrize("creds", [
    json.dumps({"project_id": "p", "client_email": "sa@example.com",
                "token_uri": "https://oauth2.example.com/token",
                "private_key": "not a key"}),
    "[1, 2]", "7", "null", '"text"', "{}",
])
def test_malformed_service_accounts_are_unavailable_never_a_500(creds):
    google = passing("x")
    with patch("app.push._post", google):
        assert verify_play_integrity(_cfg(play_integrity_service_account_json=creds),
                                     JWE, "x") == "integrity_unavailable"
    assert google.decode_calls == []


def test_a_malformed_service_account_is_a_503_over_http(make_client):
    client = make_client(FCM_SERVICE_ACCOUNT_JSON="[1, 2]")
    r = _post(client, fcm_tok("a"), integrity_token=JWE)
    assert r.status_code == 503 and r.get_json()["error"] == "integrity_unavailable"


# --- failed checks per network: the daily Google quota is not for junk -----

V6 = "2001:db8:1:%x::1"


def _fail_google():
    return FakeGoogle((400, {}))


def _from(client, name, addr="203.0.113.7", token=JWE):
    return client.post("/api/v1/devices", headers={"X-Forwarded-For": addr},
                       json={"platform": "fcm", "token": fcm_tok(name),
                             "integrity_token": token})


def test_a_network_over_its_failures_gets_429_and_no_google_call(client):
    google = _fail_google()
    with patch("app.push._post", google):
        for i in range(3):
            assert _from(client, f"f{i}").status_code == 403
        r = _from(client, "f3")
        other = _from(client, "f4", addr="203.0.113.8")
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    assert 1 <= r.get_json()["retry_after"] <= 600
    assert len(google.decode_calls) == 4      # three + the other network's one
    assert other.status_code == 403


def test_an_ipv6_48_is_bounded_across_its_64s(client):
    google = _fail_google()
    with patch("app.push._post", google):
        for i in range(30):
            assert _from(client, f"f{i}", addr=V6 % i).status_code == 403
        r = _from(client, "last", addr=V6 % 99)
    assert r.status_code == 429 and len(google.decode_calls) == 30


def test_the_failures_expire_after_ten_minutes(client):
    google = _fail_google()
    with patch("app.push._post", google):
        for i in range(3):
            _from(client, f"f{i}")
        assert _from(client, "blocked").status_code == 429
        connect(os.environ["DB_PATH"]).execute(
            "UPDATE rate_events SET at=datetime('now', '-11 minutes')")
        assert _from(client, "later").status_code == 403
    assert len(google.decode_calls) == 4


def test_a_token_that_is_not_a_jwe_fails_without_a_call_and_counts(client):
    google = passing("x")
    with patch("app.push._post", google):
        for i, junk in enumerate(["x", "a.b.c", "a.b.c.d.e.f", "a b.c.d.e.f", "a..c.d."]):
            r = _from(client, f"j{i}", token=junk)
            assert r.status_code == (403 if i < 3 else 429), (junk, r.status_code)
    assert google.decode_calls == []


def test_unavailable_missing_and_passing_checks_do_not_count(client):
    with patch("app.push._post", FakeGoogle((500, {}))):
        for i in range(5):
            assert _from(client, f"u{i}").status_code == 503
    with patch("app.push._post", passing(fcm_tok("ok"))):
        assert _from(client, "ok").status_code == 201
    for i in range(5):
        assert _post(client, fcm_tok(f"m{i}")).status_code == 400
    google = _fail_google()
    with patch("app.push._post", google):
        assert _from(client, "f0").status_code == 403    # still room: nothing above counted
    assert len(google.decode_calls) == 1


def test_the_poller_prunes_the_failure_buckets():
    from app.ratelimit import RATE_EVENT_LIFETIMES
    assert RATE_EVENT_LIFETIMES["intfail"] == RATE_EVENT_LIFETIMES["intfail48"] == 600
