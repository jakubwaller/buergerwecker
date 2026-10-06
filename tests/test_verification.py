"""Device verification by push (app/api.py, app/push.send_verifications,
app/repo.py, housekeeping's purge of unverified devices). The relay is faked
at `app.push._post`; nothing here touches the network."""
import hashlib
from datetime import datetime, time
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import load_config
from app.db import connect, init_schema
from app.housekeeping import _prune_push_devices
from app.models import Filter, PollPlan, Slot
from app.push import send_verifications
from app.repo import verify_push_wait, active_subscriptions, insert_push_subscription, soft_delete
from app.snapshots import record_snapshots
from test_api import (LEIPZIG_SVC, LEIPZIG_SVC_2, LEIPZIG_LOC, _auth, _db,
                      _register, _subscribe, client)  # noqa: F401  (fixtures)

_EC_PEM = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
_APNS_ENV = {"APNS_TEAM_ID": "TEAM123456", "APNS_KEY_ID": "KEY1234567",
             "APNS_KEY_P8": _EC_PEM, "APNS_TOPIC": "app.example.test"}


class Relay:
    """Stands in for `app.push._post`; answers every request with `status`."""

    def __init__(self, status=200):
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, platform, url, *, headers=None, json=None, data=None):
        self.calls.append({"url": url, "json": json})
        return httpx.Response(self.status, json={})

    def codes(self):
        return [c["json"]["code"] for c in self.calls]


def _enable_push(monkeypatch):
    for k, v in _APNS_ENV.items():
        monkeypatch.setenv(k, v)


def _post_code(client, auth, code):
    return client.post("/api/v1/device/verify", json={"code": code}, headers=auth)


def _row(dev):
    return _db().execute("SELECT * FROM push_devices WHERE id=?", (dev,)).fetchone()


def _age_deliveries(dev, minutes=2):
    """Make the device's delivered verification pushes `minutes` older: the
    one-a-minute and five-a-day limits are measured from these rows."""
    # The key changes too: a real delivery that old has a different minute.
    _db().execute("UPDATE sent_idempotency SET sent_at=datetime(sent_at, ?), "
                  "idem_key=idem_key || '-' || abs(random()) WHERE idem_key LIKE ?",
                  (f"-{minutes} minutes", f"verify|{dev}|%"))
    _db().execute("UPDATE push_devices SET verify_code_at="
                  "datetime(verify_code_at, ?) WHERE id=?", (f"-{minutes} minutes", dev))


def _backdate(dev, column, modifier):
    _db().execute(f"UPDATE push_devices SET {column}=datetime('now', ?) WHERE id=?",
                  (modifier, dev))
    if column == "verify_requested_at":
        # The stored code is a claim for a minute (set_verify_code): a request
        # that old has an old code stamp too.
        _db().execute("UPDATE push_devices SET verify_code_at=datetime('now', ?) "
                      "WHERE id=? AND verify_code_at IS NOT NULL", (modifier, dev))


@pytest.fixture
def relay(monkeypatch, client):
    _enable_push(monkeypatch)
    # The app reads its config at create_app(), so rebuild it with credentials.
    from app.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    r = Relay()
    with patch("app.push._post", r):
        yield app.test_client(), r


# ---------------------------------------------------------------------------
# The lock

def test_a_new_device_is_unverified_and_locked_out_of_everything_but_three_routes(client):
    dev, secret = _register(client, verified=False)
    auth = _auth(dev, secret)
    assert client.get("/api/v1/device", headers=auth).get_json()["verified"] is False
    for r in (_subscribe(client, auth),
              client.get("/api/v1/subscriptions", headers=auth),
              client.get("/api/v1/subscriptions/1", headers=auth),
              client.put("/api/v1/subscriptions/1", json={}, headers=auth),
              client.delete("/api/v1/subscriptions/1", headers=auth),
              client.post("/api/v1/subscriptions/1/renew", headers=auth),
              ):
        body = r.get_json()
        assert r.status_code == 403 and body["error"] == "device_unverified"
        assert body["message"] == ("Dieses Gerät ist noch nicht bestätigt. "
                                   "Warte auf die Test-Benachrichtigung.")
    assert client.get("/api/v1/cities").status_code == 200
    assert client.get("/api/v1/cities/leipzig").status_code == 200
    assert client.get("/api/v1/cities/leipzig/slots").status_code == 200
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 1


def test_the_locked_message_is_english_for_an_english_device(client):
    dev, secret = _register(client, language="en", verified=False)
    r = _subscribe(client, _auth(dev, secret))
    assert r.get_json()["message"] == ("This device is not verified yet. "
                                       "Wait for the test notification.")


def test_register_answers_verified_false(client):
    r = client.post("/api/v1/devices", json={"platform": "apns", "token": "t-1"})
    assert r.status_code == 201 and r.get_json()["verified"] is False


# ---------------------------------------------------------------------------
# The code

def test_registering_pushes_a_code_and_posting_it_unlocks_the_device(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    auth = _auth(dev, secret)
    assert len(r.calls) == 1
    payload = r.calls[0]["json"]
    assert payload["type"] == "verify" and payload["code"]
    assert payload["aps"]["alert"] == {
        "title": "Bürgerwecker ist bereit",
        "body": "Benachrichtigungen funktionieren. Du kannst jetzt Termine beobachten."}
    row = _row(dev)
    assert row["verify_sent_at"] is not None
    # Only the hash is stored, and nothing in the idempotency table has it.
    code = payload["code"]
    assert row["verify_code_hash"] == hashlib.sha256(code.encode()).hexdigest()
    keys = [k[0] for k in _db().execute("SELECT idem_key FROM sent_idempotency")]
    import re
    assert len(keys) == 1 and re.fullmatch(rf"verify\|{dev}\|\d{{12}}", keys[0])
    assert code not in "".join(keys)

    assert _subscribe(client, auth).status_code == 403
    ok = _post_code(client, auth, code)
    assert ok.status_code == 200 and ok.get_json() == {"verified": True}
    assert _subscribe(client, auth).status_code == 201
    assert client.get("/api/v1/device", headers=auth).get_json()["verified"] is True
    assert _post_code(client, auth, code).get_json() == {"verified": True}
    assert _post_code(client, auth, "anything").status_code == 200


def test_the_english_push_is_english(relay):
    client, r = relay
    _register(client, language="en", verified=False)
    assert r.calls[0]["json"]["aps"]["alert"]["title"] == "Bürgerwecker is ready"


def test_a_wrong_missing_or_expired_code_is_invalid(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    auth = _auth(dev, secret)
    for body in ({"code": "wrong"}, {}, {"code": 5}, {"code": ""}):
        resp = client.post("/api/v1/device/verify", json=body, headers=auth)
        assert resp.status_code == 400 and resp.get_json()["error"] == "invalid_code"
    assert resp.get_json()["message"] == "Der Code ist ungültig oder abgelaufen."
    _backdate(dev, "verify_code_at", "-25 hours")
    assert _post_code(client, auth, r.codes()[0]).status_code == 400
    assert _row(dev)["verified_at"] is None


def test_a_resend_inside_a_minute_is_rate_limited_after_it_replaces_the_code(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    auth = _auth(dev, secret)
    again = client.post("/api/v1/device/verify/resend", headers=auth)
    body = again.get_json()
    assert again.status_code == 429 and body["error"] == "rate_limited"
    assert 0 < body["retry_after"] <= 60
    assert len(r.calls) == 1
    _age_deliveries(dev)
    resent = client.post("/api/v1/device/verify/resend", headers=auth)
    assert resent.status_code == 202 and resent.get_json() == {"verified": False}
    assert len(r.calls) == 2
    old, new = r.codes()
    assert old != new
    assert _post_code(client, auth, old).status_code == 400
    assert _post_code(client, auth, new).status_code == 200
    again = client.post("/api/v1/device/verify/resend", headers=auth)
    assert again.status_code == 200 and again.get_json() == {"verified": True}


def test_registering_the_same_token_again_locks_management_until_verified(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    assert _post_code(client, _auth(dev, secret), r.codes()[0]).status_code == 200
    sub = _subscribe(client, _auth(dev, secret)).get_json()["id"]
    _age_deliveries(dev)
    dev2, new_secret = _register(client, verified=False)
    assert dev2 == dev
    auth = _auth(dev, new_secret)
    old = _auth(dev, secret)
    seen = client.get("/api/v1/device", headers=auth).get_json()
    assert seen["verified"] is False
    assert seen["subscriptions"] == []                 # not for an unverified holder
    assert len(active_subscriptions(_db())) == 1       # still running
    # The old install keeps full access; the new secret is locked.
    assert len(client.get("/api/v1/device", headers=old).get_json()["subscriptions"]) == 1
    assert _subscribe(client, old).status_code == 201
    assert _subscribe(client, auth).status_code == 403
    put = client.put(f"/api/v1/subscriptions/{sub}", json={}, headers=auth)
    assert put.status_code == 403 and put.get_json()["error"] == "device_unverified"
    assert len(r.calls) == 2
    code = r.codes()[1]
    # The old install cannot verify on behalf of the new one.
    assert _post_code(client, old, code).status_code == 200
    assert _row(dev)["pending_secret_hash"] is not None
    assert client.get("/api/v1/device", headers=auth).get_json()["verified"] is False
    assert _post_code(client, auth, code).status_code == 200
    assert client.get(f"/api/v1/subscriptions/{sub}", headers=auth).status_code == 200
    back = client.get("/api/v1/device", headers=auth).get_json()
    assert sub in [x["id"] for x in back["subscriptions"]]
    assert client.get("/api/v1/device", headers=old).status_code == 401   # promoted


def test_a_pending_secret_older_than_a_day_is_unauthorized(relay):
    client, r = relay
    dev, secret = _register(client, verified=True)
    _, new = _register(client, verified=False)
    assert client.get("/api/v1/device", headers=_auth(dev, new)).status_code == 200
    _backdate(dev, "pending_since", "-25 hours")
    assert client.get("/api/v1/device", headers=_auth(dev, new)).status_code == 401
    assert client.get("/api/v1/device", headers=_auth(dev, secret)).status_code == 200
    # A resend re-stamps the request but does not extend the pending secret.
    _age_deliveries(dev)
    assert client.post("/api/v1/device/verify/resend",
                       headers=_auth(dev, secret)).status_code == 200  # verified: no-op
    _db().execute("UPDATE push_devices SET verify_requested_at=datetime('now') "
                  "WHERE id=?", (dev,))
    assert client.get("/api/v1/device", headers=_auth(dev, new)).status_code == 401


def test_a_code_does_not_live_on_because_the_request_was_restamped(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    _backdate(dev, "verify_code_at", "-25 hours")
    _age_deliveries(dev)
    _register(client, verified=False)                  # re-stamps verify_requested_at
    assert _row(dev)["verify_requested_at"] > "2000"
    assert _post_code(client, _auth(dev, secret), r.codes()[0]).status_code == 400


def test_a_token_change_unverifies_and_pauses_subscriptions_until_the_new_token_verifies(relay):
    client, r = relay
    dev, secret = _register(client)
    auth = _auth(dev, secret)
    sub = _subscribe(client, auth).get_json()["id"]
    assert [s.id for s in active_subscriptions(_db())] == [sub]
    assert client.put("/api/v1/device", json={"language": "en"},
                      headers=auth).get_json()["verified"] is True   # language only
    _age_deliveries(dev)
    put = client.put("/api/v1/device", json={"token": "tok-new"}, headers=auth)
    assert put.status_code == 200 and put.get_json()["verified"] is False
    assert put.get_json()["subscriptions"] == []
    assert active_subscriptions(_db()) == []           # paused: not polled, not counted
    assert len(r.calls) == 2 and r.calls[1]["url"].endswith("/tok-new")
    assert client.get("/api/v1/subscriptions", headers=auth).status_code == 403
    assert _post_code(client, auth, r.codes()[1]).status_code == 200
    assert [s.id for s in active_subscriptions(_db())] == [sub]


def test_swapping_the_token_for_junk_does_not_mint_verified_devices(relay):
    client, r = relay
    dev, secret = _register(client, token="T")
    auth = _auth(dev, secret)
    _subscribe(client, auth)
    client.put("/api/v1/device", json={"token": "junk"}, headers=auth)
    assert active_subscriptions(_db()) == []
    dev2, secret2 = _register(client, token="T", verified=False)
    assert dev2 != dev
    assert _subscribe(client, _auth(dev2, secret2)).status_code == 403
    assert active_subscriptions(_db()) == []           # nothing counts toward a cap


def test_registering_again_inside_a_minute_sends_nothing_and_never_touches_the_secret(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    code = r.codes()[0]
    before = _row(dev)
    _, new_secret = _register(client, verified=False)
    after = _row(dev)
    assert len(r.calls) == 1 and new_secret != secret
    assert after["secret_hash"] == before["secret_hash"]       # never rotated
    assert after["pending_secret_hash"] == hashlib.sha256(new_secret.encode()).hexdigest()
    assert after["verify_sent_at"] is None                     # the sender decides
    assert after["verify_code_hash"] == before["verify_code_hash"]
    # The old (locked) secret still works, the new one is pending.
    assert client.get("/api/v1/device", headers=_auth(dev, secret)).status_code == 200
    assert _subscribe(client, _auth(dev, secret)).status_code == 403
    assert _subscribe(client, _auth(dev, new_secret)).status_code == 403
    # The delivered code promotes the pending secret; the old one dies.
    assert _post_code(client, _auth(dev, new_secret), code).status_code == 200
    assert client.get("/api/v1/device", headers=_auth(dev, secret)).status_code == 401
    assert _subscribe(client, _auth(dev, new_secret)).status_code == 201
    _age_deliveries(dev)
    _register(client, verified=False)
    assert len(r.calls) == 2


def test_a_registration_cannot_take_over_a_row_that_holds_subscriptions(relay):
    client, r = relay
    dev, secret = _register(client, token="T")
    own = _auth(dev, secret)
    sub = _subscribe(client, own).get_json()["id"]
    _age_deliveries(dev)
    client.put("/api/v1/device", json={"token": "T2"}, headers=own)   # unverified now
    _age_deliveries(dev)
    # A stranger who knows the new token registers it.
    dev2, stranger = _register(client, token="T2", verified=False)
    assert dev2 == dev
    pend = _auth(dev, stranger)
    for resp in (client.put("/api/v1/device", json={"token": "x"}, headers=pend),
                 client.delete("/api/v1/device", headers=pend)):
        assert resp.status_code == 403
    assert client.get("/api/v1/device", headers=pend).get_json()["subscriptions"] == []
    # The owner's secret still works and verifies with the code on the new token.
    assert client.get("/api/v1/device", headers=own).status_code == 200
    assert _post_code(client, own, r.codes()[-1]).status_code == 200
    assert _row(dev)["pending_secret_hash"] is None
    assert [s["id"] for s in client.get("/api/v1/subscriptions",
                                        headers=own).get_json()["subscriptions"]] == [sub]
    assert client.get("/api/v1/device", headers=pend).status_code == 401


def test_the_minute_claim_stops_a_second_sender(client, monkeypatch):
    dev, _ = _register(client, verified=False)
    _backdate(dev, "verify_requested_at", "-1 minutes")
    _enable_push(monkeypatch)
    conn = _db()
    with patch("app.push._verify_minute", return_value="202601010000"):
        conn.execute("INSERT INTO sent_idempotency (idem_key, provider) "
                     "VALUES (?, 'pending')", (f"verify|{dev}|202601010000",))
        with patch("app.push._post", Relay()) as r:
            assert send_verifications(conn, load_config()) == 0
        assert r.calls == []
        conn.execute("DELETE FROM sent_idempotency")
        _backdate(dev, "verify_code_at", "-61 seconds")
        with patch("app.push._post", Relay()) as r:
            assert send_verifications(conn, load_config()) == 1
        assert len(r.calls) == 1
    assert [k[0] for k in conn.execute("SELECT idem_key FROM sent_idempotency")] \
        == [f"verify|{dev}|202601010000"]


def test_a_pending_secret_replaced_in_between_is_never_promoted_by_the_old_code(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    assert _post_code(client, _auth(dev, secret), r.codes()[0]).status_code == 200
    _age_deliveries(dev)
    _, a = _register(client, verified=False)           # pending A, code delivered
    assert len(r.calls) == 2
    _, b = _register(client, verified=False)           # pending B replaces A, no new push
    assert len(r.calls) == 2
    code = r.codes()[1]
    assert _post_code(client, _auth(dev, a), code).status_code == 401   # A is gone
    assert _row(dev)["secret_hash"] == hashlib.sha256(secret.encode()).hexdigest()
    # The race itself: the verify transaction promotes only the hash it
    # authenticated with.
    from app.repo import verify_device
    assert verify_device(_db(), dev, code,
                         pending_hash=hashlib.sha256(a.encode()).hexdigest()) is False
    assert _row(dev)["verified_at"] is not None and _row(dev)["pending_secret_hash"]
    assert _post_code(client, _auth(dev, b), code).status_code == 200
    assert client.get("/api/v1/device", headers=_auth(dev, b)).get_json()["verified"] is True


def test_an_unverified_owner_may_rotate_or_delete_but_a_pending_credential_may_not(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    own = _auth(dev, secret)
    put = client.put("/api/v1/device", json={"token": "tok-2"}, headers=own)
    assert put.status_code == 200 and put.get_json()["verified"] is False
    assert client.delete("/api/v1/device", headers=own).status_code == 204
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0

    dev, secret = _register(client, token="tok-3", verified=True)
    _age_deliveries(dev)
    _, pending = _register(client, token="tok-3", verified=False)
    pend = _auth(dev, pending)
    assert client.get("/api/v1/device", headers=pend).status_code == 200
    for resp in (client.put("/api/v1/device", json={"token": "x"}, headers=pend),
                 client.delete("/api/v1/device", headers=pend)):
        assert resp.status_code == 403 and resp.get_json()["error"] == "device_unverified"
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 1


def test_a_token_change_inside_a_minute_delivers_exactly_one_code(relay, monkeypatch):
    client, r = relay
    dev, secret = _register(client, verified=True)
    assert len(r.calls) == 1
    put = client.put("/api/v1/device", json={"token": "tok-new"},
                     headers=_auth(dev, secret))
    assert put.status_code == 200
    assert len(r.calls) == 1                           # delivered under a minute ago
    row = _row(dev)
    assert row["verify_sent_at"] is None and row["verify_code_hash"] is None
    cfg = load_config()
    conn = _db()
    assert send_verifications(conn, cfg) == 0          # sweep: grace and minute
    _age_deliveries(dev)
    _db().execute("UPDATE push_devices SET verify_requested_at="
                  "datetime('now','-1 minutes') WHERE id=?", (dev,))
    assert send_verifications(conn, cfg) == 1          # the sweep goes first
    assert send_verifications(conn, cfg, device_ids=[dev]) == 0   # never a second
    assert send_verifications(conn, cfg) == 0
    assert len(r.calls) == 2 and r.calls[1]["url"].endswith("/tok-new")


def test_the_dashboard_does_not_count_paused_subscriptions(relay):
    from app.admin import stats as collect_stats
    client, r = relay
    dev, secret = _register(client)
    _subscribe(client, _auth(dev, secret))
    assert collect_stats(_db())["active_subscriptions"] == 1
    client.put("/api/v1/device", json={"token": "other"}, headers=_auth(dev, secret))
    stats = collect_stats(_db())
    assert stats["active_subscriptions"] == 0 and stats["active_subscribers"] == 0
    assert stats["active_subscriptions_by_city"] == {}


def test_a_sixth_verification_push_in_a_day_is_refused_everywhere(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    auth = _auth(dev, secret)
    for _ in range(4):
        _age_deliveries(dev)
        assert client.post("/api/v1/device/verify/resend",
                           headers=auth).status_code == 202
    assert len(r.calls) == 5
    _age_deliveries(dev)
    over = client.post("/api/v1/device/verify/resend", headers=auth)
    assert over.status_code == 429 and over.get_json()["retry_after"] > 0
    _register(client, verified=False)                  # normal answer, no push
    cfg = load_config()
    assert send_verifications(_db(), cfg) == 0         # the sweep respects it too
    assert len(r.calls) == 5
    # The window frees once the oldest push is over a day old.
    _db().execute("UPDATE sent_idempotency SET sent_at=datetime('now','-25 hours') "
                  "WHERE idem_key LIKE ?", (f"verify|{dev}|%",))
    assert verify_push_wait(_db(), dev) == 0


# ---------------------------------------------------------------------------
# The sender, without credentials and with a deferring relay

def test_without_credentials_nothing_is_sent_and_the_poller_sweep_delivers(client, monkeypatch):
    with patch("app.push._post", Relay()) as none:
        dev, secret = _register(client, verified=False)
    assert none.calls == [] and _row(dev)["verify_sent_at"] is None
    _backdate(dev, "verify_requested_at", "-1 minutes")
    _enable_push(monkeypatch)
    cfg = load_config()
    conn = _db()
    r = Relay()
    with patch("app.push._post", r):
        assert send_verifications(conn, cfg) == 1
        assert len(r.calls) == 1 and _row(dev)["verify_sent_at"] is not None
        assert send_verifications(conn, cfg) == 0
        assert len(r.calls) == 1
    assert _post_code(client, _auth(dev, secret), r.codes()[0]).status_code == 200


def test_a_deferring_relay_leaves_the_device_waiting(client, monkeypatch):
    dev, _ = _register(client, verified=False)
    _backdate(dev, "verify_requested_at", "-1 minutes")
    _enable_push(monkeypatch)
    conn = _db()
    with patch("app.push._post", Relay(status=503)):
        assert send_verifications(conn, load_config()) == 0
    row = _row(dev)
    assert row["verify_sent_at"] is None
    # The claim was released, so the next pass can try again.
    assert conn.execute("SELECT COUNT(*) FROM sent_idempotency").fetchone()[0] == 0
    # The stored code holds the claim for a minute; then a new one goes out.
    _backdate(dev, "verify_code_at", "-61 seconds")
    with patch("app.push._post", Relay()) as ok:
        assert send_verifications(conn, load_config()) == 1
    assert len(ok.calls) == 1


def test_the_stored_code_is_the_claim_one_sender_per_device_per_minute(client, monkeypatch):
    dev, _ = _register(client, verified=False)
    _backdate(dev, "verify_requested_at", "-1 minutes")
    _enable_push(monkeypatch)
    conn = _db()
    with patch("app.push._post", Relay(status=503)):
        assert send_verifications(conn, load_config()) == 0      # stores a code
    first = _row(dev)["verify_code_hash"]
    with patch("app.push._post", Relay()) as second:
        assert send_verifications(conn, load_config()) == 0      # claim held
    assert second.calls == [] and _row(dev)["verify_code_hash"] == first
    _db().execute("UPDATE push_devices SET verify_code_at=datetime('now','-61 seconds') "
                  "WHERE id=?", (dev,))
    with patch("app.push._post", Relay()) as third:
        assert send_verifications(conn, load_config()) == 1
    assert len(third.calls) == 1 and _row(dev)["verify_code_hash"] != first


def test_a_resend_clears_the_code_stamp_and_sends_at_once(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    _age_deliveries(dev)
    assert client.post("/api/v1/device/verify/resend",
                       headers=_auth(dev, secret)).status_code == 202
    assert len(r.calls) == 2
    # Cleared by the request, stored again by the send that followed.
    from app.repo import request_verification
    request_verification(_db(), dev)
    assert _row(dev)["verify_code_at"] is None


def test_a_request_older_than_a_day_is_no_longer_swept(client, monkeypatch):
    dev, _ = _register(client, verified=False)
    _backdate(dev, "verify_requested_at", "-25 hours")
    _enable_push(monkeypatch)
    with patch("app.push._post", Relay()) as r:
        assert send_verifications(_db(), load_config()) == 0
    assert r.calls == []


def test_a_relay_failure_never_fails_the_registration(client, monkeypatch):
    _enable_push(monkeypatch)
    with patch("app.push.send_verifications", side_effect=RuntimeError("boom")):
        from app.web import create_app
        app = create_app()
        app.config["TESTING"] = True
        r = app.test_client().post("/api/v1/devices",
                                   json={"platform": "apns", "token": "t-9"})
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Housekeeping and snapshots

def test_housekeeping_purges_unverified_devices_without_subscriptions_after_a_day(client):
    conn = _db()

    def add(token, *, verified, age):
        dev, _ = _register(client, token=token, verified=verified)
        conn.execute("UPDATE push_devices SET created_at=datetime('now', ?), "
                     "verify_requested_at=datetime('now', ?) WHERE id=?",
                     (age, age, dev))
        return dev

    stale = add("stale", verified=False, age="-2 days")
    in_progress = add("progress", verified=False, age="-2 days")
    conn.execute("UPDATE push_devices SET verify_requested_at=datetime('now') "
                 "WHERE id=?", (in_progress,))
    young = add("young", verified=False, age="-1 hours")
    verified = add("verified", verified=True, age="-2 days")
    held = add("held", verified=False, age="-2 days")
    sub = insert_push_subscription(
        conn, device_id=held, city="leipzig", language="de", ttl_days=30,
        filter_=Filter(appointment_types=[LEIPZIG_SVC], locations="all",
                       weekdays=[1], time_window_start=time(8, 0),
                       time_window_end=time(12, 0)))
    soft_delete(conn, sub)
    _prune_push_devices(conn)
    left = {r[0] for r in conn.execute("SELECT id FROM push_devices")}
    assert left == {young, verified, held, in_progress} and stale not in left


def test_a_failed_plan_is_logged_once_per_city(tmp_path, capsys):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    good = PollPlan(city="leipzig", appointment_type=LEIPZIG_SVC, locations="all")
    a = PollPlan(city="leipzig", appointment_type=LEIPZIG_SVC_2,
                 locations=[LEIPZIG_LOC])
    b = PollPlan(city="leipzig", appointment_type=LEIPZIG_SVC_2, locations="all")
    record_snapshots(conn, [good, a, b], {"leipzig"}, [good, a], {
        good.key(): [Slot("2026-06-10", "10:30", LEIPZIG_LOC, LEIPZIG_SVC, "t")],
        a.key(): []}, now=datetime(2026, 6, 8, 12, 0))
    out = capsys.readouterr().out.splitlines()
    assert out == ["snapshots: leipzig: kept the previous snapshot for 1 "
                   "service(s) with a failed plan"]
    # The service whose plans all succeeded was still recorded.
    assert conn.execute("SELECT COUNT(*) FROM slot_snapshots").fetchone()[0] == 1


def test_the_poller_sweep_swallows_a_failure(capsys):
    from app.poller import _sweep_verifications
    with patch("app.push.send_verifications", side_effect=RuntimeError("boom")):
        _sweep_verifications(None, None)
    assert "verification sweep failed" in capsys.readouterr().out


def test_the_sweep_gives_the_web_request_its_turn_first(client, monkeypatch):
    dev, _ = _register(client, verified=False)
    _backdate(dev, "verify_requested_at", "-5 seconds")
    _enable_push(monkeypatch)
    conn = _db()
    with patch("app.push._post", Relay()) as r:
        assert send_verifications(conn, load_config()) == 0      # sweep: too fresh
        _backdate(dev, "verify_requested_at", "-1 minutes")
        assert send_verifications(conn, load_config()) == 1      # sweep: due now
    assert len(r.calls) == 1
    other, _ = _register(client, token="t-2", verified=False)    # requested just now
    _backdate(other, "verify_code_at", "-61 seconds")            # no credentials: never sent
    with patch("app.push._post", Relay()) as r2:
        assert send_verifications(conn, load_config(), device_ids=[other]) == 1
    assert len(r2.calls) == 1                                    # in-request: no wait
