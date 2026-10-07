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
from app.repo import (verify_push_wait, active_subscriptions, insert_push_subscription,
                      soft_delete, token_key)
from app.snapshots import record_snapshots
from test_api import (LEIPZIG_SVC, LEIPZIG_SVC_2, LEIPZIG_LOC, _auth, _db,
                      _register, _subscribe, client, tok)  # noqa: F401  (fixtures)

_EC_PEM = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()
_APNS_ENV = {"APNS_TEAM_ID": "TEAM123456", "APNS_KEY_ID": "KEY1234567",
             "APNS_KEY_P8": _EC_PEM, "APNS_TOPIC": "app.example.test"}


class Relay:
    """Stands in for `app.push._post`; answers every request with `status`
    (and an APNs `reason`, if given)."""

    def __init__(self, status=200, reason=None):
        self.status = status
        self.reason = reason
        self.calls: list[dict] = []

    def __call__(self, platform, url, *, headers=None, json=None, data=None):
        self.calls.append({"url": url, "json": json})
        return httpx.Response(self.status,
                              json={"reason": self.reason} if self.reason else {})

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
    """Make the device's verification pushes `minutes` older: the
    one-a-minute and daily limits are measured from the per-token attempt
    rows, the claim from the code's stamp."""
    db = _db()
    age = f"-{minutes} minutes"
    # The key changes too: a real delivery that old has a different minute.
    db.execute("UPDATE sent_idempotency SET sent_at=datetime(sent_at, ?), "
               "idem_key=idem_key || '-' || abs(random()) WHERE idem_key LIKE ?",
               (age, f"verify|{dev}|%"))
    row = db.execute("SELECT platform, token FROM push_devices WHERE id=?",
                     (dev,)).fetchone()
    db.execute("UPDATE verify_attempts SET at=datetime(at, ?) WHERE token_key=?",
               (age, token_key(row["platform"], row["token"])))
    db.execute("UPDATE push_devices SET verify_code_at=datetime(verify_code_at, ?), "
               "verify_next_at=datetime(verify_next_at, ?) WHERE id=?", (age, age, dev))


def _attempts(platform="apns", name="tok-1"):
    """The attempts that count toward the token's daily budget: deliveries
    and refusals with evidence that the platform works."""
    cols = {r["name"] for r in _db().execute("PRAGMA table_info(verify_attempts)")}
    credited = " AND credited=1" if "credited" in cols else ""
    return [(r["kind"]) for r in _db().execute(
        f"SELECT kind FROM verify_attempts WHERE token_key=?{credited} ORDER BY at, rowid",
        (token_key(platform, tok(name, platform)),))]


def _ceiling():
    from app import repo
    return getattr(repo, "MAX_VERIFY_ATTEMPTS_PER_TOKEN_PER_DAY", 20)


def _time_passes(minutes=2):
    """Every verification clock `minutes` older: attempts, codes, not-befores."""
    db = _db()
    age = f"-{minutes} minutes"
    db.execute("UPDATE verify_attempts SET at=datetime(at, ?)", (age,))
    db.execute("UPDATE sent_idempotency SET sent_at=datetime(sent_at, ?), "
               "idem_key=idem_key || '-' || abs(random()) WHERE idem_key LIKE 'verify|%'",
               (age,))
    db.execute("UPDATE push_devices SET verify_code_at=datetime(verify_code_at, ?), "
               "verify_next_at=datetime(verify_next_at, ?), "
               "verify_requested_at=datetime(verify_requested_at, ?)", (age, age, age))


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
              client.post("/api/v1/subscriptions/1/renew", json={}, headers=auth),
              ):
        body = r.get_json()
        assert r.status_code == 403 and body["error"] == "device_unverified"
        assert body["message"] == ("Dieses Gerät ist noch nicht bestätigt. "
                                   "Warte auf die Test-Benachrichtigung.")
    assert client.get("/api/v1/cities").status_code == 200
    assert client.get("/api/v1/cities/leipzig").status_code == 200
    r = client.get("/api/v1/cities/leipzig/slots", headers=auth)
    assert r.status_code == 403 and r.get_json()["error"] == "device_unverified"
    assert _db().execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 1


def test_the_locked_message_is_english_for_an_english_device(client):
    dev, secret = _register(client, language="en", verified=False)
    r = _subscribe(client, _auth(dev, secret))
    assert r.get_json()["message"] == ("This device is not verified yet. "
                                       "Wait for the test notification.")


def test_register_answers_verified_false(client):
    r = client.post("/api/v1/devices", json={"platform": "apns", "token": tok("t-1")})
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
    again = client.post("/api/v1/device/verify/resend", json={}, headers=auth)
    body = again.get_json()
    assert again.status_code == 429 and body["error"] == "rate_limited"
    assert 0 < body["retry_after"] <= 60
    assert len(r.calls) == 1
    _age_deliveries(dev)
    resent = client.post("/api/v1/device/verify/resend", json={}, headers=auth)
    assert resent.status_code == 202 and resent.get_json() == {"verified": False}
    assert len(r.calls) == 2
    old, new = r.codes()
    assert old != new
    assert _post_code(client, auth, old).status_code == 400
    assert _post_code(client, auth, new).status_code == 200
    again = client.post("/api/v1/device/verify/resend", json={}, headers=auth)
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
    # Not the subscriptions, nor the device's id, age or language.
    assert seen == {"verified": False}
    assert len(active_subscriptions(_db())) == 1       # still running
    # The old install keeps full access; the new secret is locked.
    assert len(client.get("/api/v1/device", headers=old).get_json()["subscriptions"]) == 1
    assert _subscribe(client, old).status_code == 201
    assert _subscribe(client, auth).status_code == 403
    put = client.put(f"/api/v1/subscriptions/{sub}", json={}, headers=auth)
    assert put.status_code == 403 and put.get_json()["error"] == "device_unverified"
    assert len(r.calls) == 2
    code = r.codes()[1]
    # A wrong code from the old install changes nothing.
    assert _post_code(client, old, "wrong").status_code == 200
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
    assert client.post("/api/v1/device/verify/resend", json={},
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
    put = client.put("/api/v1/device", json={"token": tok("tok-new")}, headers=auth)
    assert put.status_code == 200 and put.get_json()["verified"] is False
    assert put.get_json()["subscriptions"] == []
    assert active_subscriptions(_db()) == []           # paused: not polled, not counted
    assert len(r.calls) == 2 and r.calls[1]["url"].endswith("/" + tok("tok-new"))
    assert client.get("/api/v1/subscriptions", headers=auth).status_code == 403
    assert _post_code(client, auth, r.codes()[1]).status_code == 200
    assert [s.id for s in active_subscriptions(_db())] == [sub]


def test_swapping_the_token_for_junk_does_not_mint_verified_devices(relay):
    client, r = relay
    dev, secret = _register(client, token="T")
    auth = _auth(dev, secret)
    _subscribe(client, auth)
    client.put("/api/v1/device", json={"token": tok("junk")}, headers=auth)
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
    client.put("/api/v1/device", json={"token": tok("T2")}, headers=own)   # unverified now
    _age_deliveries(dev)
    # A stranger who knows the new token registers it.
    dev2, stranger = _register(client, token="T2", verified=False)
    assert dev2 == dev
    pend = _auth(dev, stranger)
    for resp in (client.put("/api/v1/device", json={"token": "x"}, headers=pend),
                 client.delete("/api/v1/device", headers=pend)):
        assert resp.status_code == 403
    assert client.get("/api/v1/device", headers=pend).get_json() == {"verified": False}
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
        assert _row(dev)["verify_next_at"] is not None     # tried, not delivered
        conn.execute("DELETE FROM sent_idempotency")
        _backdate(dev, "verify_code_at", "-61 seconds")
        _backdate(dev, "verify_next_at", "-1 seconds")
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
    put = client.put("/api/v1/device", json={"token": tok("tok-2")}, headers=own)
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
    put = client.put("/api/v1/device", json={"token": tok("tok-new")},
                     headers=_auth(dev, secret))
    assert put.status_code == 200
    assert len(r.calls) == 1                           # delivered under a minute ago
    row = _row(dev)
    assert row["verify_sent_at"] is None and row["verify_code_hash"] is None
    assert row["verify_next_at"] is not None           # the sweep waits it out
    cfg = load_config()
    conn = _db()
    assert send_verifications(conn, cfg) == 0          # sweep: grace and minute
    _age_deliveries(dev)
    _db().execute("UPDATE push_devices SET verify_requested_at="
                  "datetime('now','-1 minutes') WHERE id=?", (dev,))
    assert send_verifications(conn, cfg) == 1          # the sweep goes first
    assert send_verifications(conn, cfg, device_ids=[dev]) == 0   # never a second
    assert send_verifications(conn, cfg) == 0
    assert len(r.calls) == 2 and r.calls[1]["url"].endswith("/" + tok("tok-new"))
    assert _attempts() == ["open"] and _attempts(name="tok-new") == ["owner"]


def test_the_tokens_minute_holds_across_a_deleted_row(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    assert client.delete("/api/v1/device", headers=_auth(dev, secret)).status_code == 204
    dev2, _ = _register(client, verified=False)        # a new row, the same token
    assert dev2 != dev and len(r.calls) == 1
    assert _row(dev2)["verify_next_at"] is not None
    _age_deliveries(dev2)
    _backdate(dev2, "verify_requested_at", "-1 minutes")
    assert send_verifications(_db(), load_config()) == 1
    assert len(r.calls) == 2


def test_the_dashboard_does_not_count_paused_subscriptions(relay):
    from app.admin import stats as collect_stats
    client, r = relay
    dev, secret = _register(client)
    _subscribe(client, _auth(dev, secret))
    assert collect_stats(_db())["active_subscriptions"] == 1
    client.put("/api/v1/device", json={"token": tok("other")}, headers=_auth(dev, secret))
    stats = collect_stats(_db())
    assert stats["active_subscriptions"] == 0 and stats["active_subscribers"] == 0
    assert stats["active_subscriptions_by_city"] == {}


def test_the_owner_has_its_own_daily_budget_per_token(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)    # open: 1
    auth = _auth(dev, secret)
    for _ in range(5):                                 # owner: 5
        _age_deliveries(dev)
        assert client.post("/api/v1/device/verify/resend", json={},
                           headers=auth).status_code == 202
    assert len(r.calls) == 6
    _age_deliveries(dev)
    over = client.post("/api/v1/device/verify/resend", json={}, headers=auth)
    assert over.status_code == 429 and over.get_json()["retry_after"] > 3600
    cfg = load_config()
    _db().execute("UPDATE push_devices SET verify_requested_at="
                  "datetime('now','-1 minutes') WHERE id=?", (dev,))
    assert send_verifications(_db(), cfg) == 0         # nothing outstanding to sweep
    assert len(r.calls) == 6
    assert _attempts() == ["open"] + ["owner"] * 5
    # The window frees once the oldest owner push is over a day old.
    _db().execute("UPDATE verify_attempts SET at=datetime('now','-25 hours')")
    assert verify_push_wait(_db(), dev, kind="owner") == 0


def test_strangers_re_registering_a_token_cannot_spend_the_owners_budget(relay):
    """Re-registering somebody's token used up the device's five a day: the
    owner's next token change got no push, and resend said 429 for a day."""
    client, r = relay
    dev, secret = _register(client, verified=True)     # the owner's phone
    own = _auth(dev, secret)
    for _ in range(4):                                 # open: 1 + 4
        _age_deliveries(dev)
        _register(client, verified=False)
    assert len(r.calls) == 5
    _age_deliveries(dev)
    _, pending = _register(client, verified=False)     # open budget spent: no push
    assert len(r.calls) == 5 and _row(dev)["verify_next_at"] is not None
    over = client.post("/api/v1/device/verify/resend", json={},
                       headers=_auth(dev, pending))     # a stranger's resend is open
    assert over.status_code == 429 and over.get_json()["retry_after"] > 3600
    cfg = load_config()
    _db().execute("UPDATE push_devices SET verify_requested_at="
                  "datetime('now','-1 minutes') WHERE id=?", (dev,))
    assert send_verifications(_db(), cfg) == 0         # the sweep respects it too
    assert len(r.calls) == 5
    # The owner still verifies a token change at once.
    put = client.put("/api/v1/device", json={"token": tok("rotated")}, headers=own)
    assert put.status_code == 200 and len(r.calls) == 6
    assert _post_code(client, own, r.codes()[-1]).status_code == 200
    # And back on the first token, whose open budget is spent, the owner's
    # token change and resend still go out.
    _age_deliveries(dev)
    client.put("/api/v1/device", json={"token": tok()}, headers=own)
    assert len(r.calls) == 7
    _age_deliveries(dev)
    assert client.post("/api/v1/device/verify/resend", json={},
                       headers=own).status_code == 202
    assert len(r.calls) == 8
    assert _attempts() == ["open"] * 5 + ["owner"] * 2


def test_deleting_and_registering_again_starts_no_new_budget(relay):
    """Register, delete, register again was a fresh device id and a fresh
    five a day, every time: one token could be made to buzz without end."""
    client, r = relay
    for _ in range(7):
        dev, secret = _register(client, verified=False)
        assert client.delete("/api/v1/device", headers=_auth(dev, secret)).status_code == 204
        _db().execute("UPDATE verify_attempts SET at=datetime(at, '-2 minutes')")
    assert len(r.calls) == 5
    assert _attempts() == ["open"] * 5


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
    # Not counted against anyone, and the device waits a minute at the back of
    # the queue; the stored code holds the claim as long. Then a new one goes out.
    assert row["verify_failures"] == 0 and _attempts() == []
    assert row["verify_next_at"] is not None
    _backdate(dev, "verify_code_at", "-61 seconds")
    _backdate(dev, "verify_next_at", "-1 seconds")
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
    # The sweep's own not-before would hold it too; take it away to see the
    # claim alone (another sender does not go by it).
    _db().execute("UPDATE push_devices SET verify_next_at=NULL WHERE id=?", (dev,))
    with patch("app.push._post", Relay()) as second:
        assert send_verifications(conn, load_config()) == 0      # claim held
    assert second.calls == [] and _row(dev)["verify_code_hash"] == first
    _db().execute("UPDATE push_devices SET verify_code_at=datetime('now','-61 seconds'), "
                  "verify_next_at=NULL WHERE id=?", (dev,))
    with patch("app.push._post", Relay()) as third:
        assert send_verifications(conn, load_config()) == 1
    assert len(third.calls) == 1 and _row(dev)["verify_code_hash"] != first


def test_a_resend_leaves_an_in_flight_code_alone(client, monkeypatch):
    dev, secret = _register(client, verified=False)
    # Another sender has just stored a code and is delivering it.
    _db().execute("UPDATE push_devices SET verify_code_hash='in-flight', "
                  "verify_code_at=CURRENT_TIMESTAMP WHERE id=?", (dev,))
    before = _row(dev)
    assert before["verify_code_hash"] and before["verify_code_at"]
    # Now the web process has credentials: a relay WOULD be called if the
    # resend cleared the in-flight code and stored a new one.
    _enable_push(monkeypatch)
    from app.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    with patch("app.push._post", Relay()) as r:
        assert app.test_client().post("/api/v1/device/verify/resend", json={},
                                      headers=_auth(dev, secret)).status_code == 202
    after = _row(dev)
    assert r.calls == []                                   # the claim is held
    assert after["verify_code_hash"] == before["verify_code_hash"]
    assert after["verify_code_at"] == before["verify_code_at"]


def test_a_resend_clears_a_stale_code_and_sends_a_new_one(relay):
    client, r = relay
    dev, secret = _register(client, verified=False)
    old = _row(dev)["verify_code_hash"]
    # Age the delivery rows only; the code's own stamp is aged on its own.
    _db().execute("UPDATE sent_idempotency SET sent_at=datetime(sent_at,'-2 minutes'), "
                  "idem_key=idem_key || '-old' WHERE idem_key LIKE ?", (f"verify|{dev}|%",))
    _db().execute("UPDATE verify_attempts SET at=datetime(at,'-2 minutes')")
    _backdate(dev, "verify_code_at", "-61 seconds")
    assert client.post("/api/v1/device/verify/resend", json={},
                       headers=_auth(dev, secret)).status_code == 202
    assert len(r.calls) == 2
    assert _row(dev)["verify_code_hash"] not in (None, old)
    assert _post_code(client, _auth(dev, secret), r.codes()[0]).status_code == 400
    assert _post_code(client, _auth(dev, secret), r.codes()[1]).status_code == 200


def test_a_resend_drops_a_stale_code_even_when_the_following_send_stores_nothing(relay):
    # set_verify_code overwrites a stale code anyway, so only a send that never
    # gets that far shows request_verification("drop_if_stale") clearing it.
    client, r = relay
    dev, secret = _register(client, verified=False)
    old_code = r.codes()[0]
    _db().execute("UPDATE sent_idempotency SET sent_at=datetime(sent_at,'-2 minutes'), "
                  "idem_key=idem_key || '-old' WHERE idem_key LIKE ?", (f"verify|{dev}|%",))
    _db().execute("UPDATE verify_attempts SET at=datetime(at,'-2 minutes')")
    _backdate(dev, "verify_code_at", "-61 seconds")
    with patch("app.push.send_verifications", side_effect=RuntimeError("boom")):
        assert client.post("/api/v1/device/verify/resend", json={},
                           headers=_auth(dev, secret)).status_code == 202
    row = _row(dev)
    assert row["verify_code_hash"] is None and row["verify_code_at"] is None
    assert _post_code(client, _auth(dev, secret), old_code).status_code == 400
    assert len(r.calls) == 1


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
                                   json={"platform": "apns", "token": tok("t-9")})
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Junk tokens: refused attempts count, the sweep gives up and rotates

def _sweep_again(*devs):
    """A minute later, as far as the sweep's clocks are concerned."""
    for dev in devs:
        _age_deliveries(dev)
        _backdate(dev, "verify_requested_at", "-2 minutes")


class ByToken(Relay):
    """Answers `status` (and `reason`) to the tokens named, 200 to every
    other: one refused phone among working ones."""

    def __init__(self, names, status=400, reason=None):
        super().__init__(status, reason)
        self.refuse = {tok(n) for n in names}

    def __call__(self, platform, url, *, headers=None, json=None, data=None):
        self.calls.append({"url": url, "json": json})
        if url.rsplit("/", 1)[1] in self.refuse:
            return httpx.Response(self.status,
                                  json={"reason": self.reason} if self.reason else {})
        return httpx.Response(200, json={})

    def to(self, name):
        return [c for c in self.calls if c["url"].endswith("/" + tok(name))]


def test_a_refused_verification_push_counts_and_the_sweep_gives_up_after_three(client, monkeypatch):
    dev, secret = _register(client, verified=False)       # no credentials: unsent
    _enable_push(monkeypatch)
    cfg = load_config()
    refusing = ByToken(["tok-1"], status=400)              # a payload refused for this token
    with patch("app.push._post", refusing):
        for n in range(1, 4):
            # Another phone's push goes out in the same pass: the platform works,
            # so the refusal is about this token.
            other, _ = _register(client, token=f"live-{n}", verified=False)
            _backdate(other, "verify_requested_at", "-1 minutes")
            _sweep_again(dev)
            assert send_verifications(_db(), cfg) == 1
            row = _row(dev)
            assert row["verify_failures"] == n and row["verify_sent_at"] is None
            assert row["verify_next_at"] is not None       # back of the queue
        _sweep_again(dev)
        assert send_verifications(_db(), cfg) == 0
    assert len(refusing.to("tok-1")) == 3                  # and not every minute for a day
    # Refused attempts are attempts: they count toward the token's budget.
    assert _attempts() == ["open"] * 3
    # The owner's resend is a new request and starts the count over.
    app_client = _relay_client(monkeypatch)
    with patch("app.push._post", Relay()) as ok:
        _age_deliveries(dev)
        r = app_client.post("/api/v1/device/verify/resend", json={},
                            headers=_auth(dev, secret))
        assert r.status_code == 202 and len(ok.calls) == 1
    assert _row(dev)["verify_failures"] == 0
    assert _attempts() == ["open"] * 3 + ["owner"]


def _relay_client(monkeypatch):
    _enable_push(monkeypatch)
    from app.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_an_outage_is_not_a_refusal(client, monkeypatch):
    dev, _ = _register(client, verified=False)
    _enable_push(monkeypatch)
    with patch("app.push._post", Relay(status=503)):
        for _ in range(5):
            _sweep_again(dev)
            assert send_verifications(_db(), load_config()) == 0
    assert _row(dev)["verify_failures"] == 0 and _attempts() == []


def test_a_dead_token_retires_a_new_device_once_the_platform_shows_it_works(
        monkeypatch, client):
    """A junk registration is retired on the first dead answer after anyone
    else got a push: the evidence that rules out a misconfiguration."""
    app_client = _relay_client(monkeypatch)
    relay = ByToken(["junk"], status=410)
    with patch("app.push._post", relay):
        r = app_client.post("/api/v1/devices", json={"platform": "apns",
                                                     "token": tok("junk")})
        dev, secret = r.get_json()["device_id"], r.get_json()["secret"]
        row = _row(dev)                                # nothing delivered yet: held
        assert row["retired_at"] is None and row["dead_since"] is not None
        assert _attempts(name="junk") == [] and row["verify_failures"] == 0
        _register(app_client, token="live", verified=False)   # someone's push goes out
        _sweep_again(dev)
        assert send_verifications(_db(), load_config()) == 0
        assert _row(dev)["retired_at"] is not None
        gone = app_client.get("/api/v1/device", headers=_auth(dev, secret))
        assert gone.status_code == 410 and gone.get_json()["error"] == "device_retired"
        _sweep_again(dev)
        assert send_verifications(_db(), load_config()) == 0
    assert len(relay.to("junk")) == 2
    assert _attempts(name="junk") == ["open"]


def test_a_misconfigured_platform_spends_no_budget_and_retires_nobody(client, monkeypatch):
    """A wrong APNS_SANDBOX or APNS_TOPIC answers dead for every device at
    once. Counting those answers against the token, or retiring on them,
    locked every phone that tried in that window out of verification for a
    day after the knob was fixed."""
    app_client = _relay_client(monkeypatch)
    wrong = Relay(status=400, reason="BadDeviceToken")
    with patch("app.push._post", wrong):
        r = app_client.post("/api/v1/devices", json={"platform": "apns", "token": tok()})
        dev = r.get_json()["device_id"]
        for _ in range(5):
            _age_deliveries(dev)
            _register(app_client, verified=False)          # the app tries again
            _sweep_again(dev)
            send_verifications(_db(), load_config())
    assert len(wrong.calls) >= 6
    row = _row(dev)
    assert row["retired_at"] is None and row["verify_failures"] == 0
    assert _attempts() == []
    _age_deliveries(dev)
    assert verify_push_wait(_db(), dev) == 0
    # The knob is fixed: the next registration verifies at once.
    with patch("app.push._post", Relay()) as fixed:
        _register(app_client, verified=False)
    assert len(fixed.calls) == 1


def test_a_token_change_under_a_misconfiguration_does_not_pause_subscriptions_for_a_day(
        client, monkeypatch):
    app_client = _relay_client(monkeypatch)
    with patch("app.push._post", Relay()):
        dev, secret = _register(app_client)
    own = _auth(dev, secret)
    sub = _subscribe(app_client, own).get_json()["id"]
    wrong = Relay(status=400, reason="DeviceTokenNotForTopic")
    with patch("app.push._post", wrong):
        _age_deliveries(dev)
        put = app_client.put("/api/v1/device", json={"token": tok("new")}, headers=own)
        assert put.status_code == 200
        for _ in range(3):
            _sweep_again(dev)
            send_verifications(_db(), load_config())
        for _ in range(2):
            _age_deliveries(dev)
            assert app_client.post("/api/v1/device/verify/resend", json={},
                                   headers=own).status_code == 202
    # The token change, two sweeps (the third waits out the backoff), two resends.
    assert len(wrong.calls) == 5
    assert active_subscriptions(_db()) == []           # paused, as any token change
    row = _row(dev)
    assert row["retired_at"] is None and row["verify_failures"] == 0
    assert _attempts(name="new") == []
    # Fixed: the owner's next resend goes out, the code verifies, and the
    # subscription runs again.
    _age_deliveries(dev)
    with patch("app.push._post", Relay()) as fixed:
        r = app_client.post("/api/v1/device/verify/resend", json={}, headers=own)
        assert r.status_code == 202 and len(fixed.calls) == 1
    assert _post_code(app_client, own, fixed.codes()[0]).status_code == 200
    assert [s.id for s in active_subscriptions(_db())] == [sub]


def test_rows_of_a_platform_this_process_cannot_send_do_not_starve_the_sweep(
        client, monkeypatch):
    """Sixty FCM registrations, older than an APNs one, on a poller with only
    APNs credentials: they filled every sweep, and the APNs device was never
    tried."""
    fcm = [_register(client, platform="fcm", token=f"f{i}", verified=False)[0]
           for i in range(60)]
    for d in fcm:
        _backdate(d, "verify_requested_at", "-10 minutes")
    apns, _ = _register(client, token="a1", verified=False)
    _backdate(apns, "verify_requested_at", "-5 minutes")
    _enable_push(monkeypatch)                          # APNs only
    with patch("app.push._post", Relay()) as r:
        for _ in range(5):
            send_verifications(_db(), load_config())
    assert len(r.calls) == 1 and r.calls[0]["url"].endswith("/" + tok("a1"))
    # Untouched: a process with FCM credentials sends them as before.
    assert all(_row(d)["verify_code_hash"] is None for d in fcm)


def test_a_junk_token_has_a_daily_ceiling_on_the_request_path(client, monkeypatch):
    """On a platform that delivers to nobody, a refusal had no evidence and
    was recorded nowhere: registering, deleting and registering the token
    again from ever new networks pushed it without end."""
    app_client = _relay_client(monkeypatch)
    refusing = Relay(status=400, reason="BadDeviceToken")
    with patch("app.push._post", refusing):
        for i in range(_ceiling() + 10):
            r = app_client.post("/api/v1/devices",
                                json={"platform": "apns", "token": tok("junk")},
                                headers={"X-Forwarded-For": f"2001:db8:{i:x}::1"})
            assert r.status_code == 201
            body = r.get_json()
            assert app_client.delete("/api/v1/device", headers=_auth(
                body["device_id"], body["secret"])).status_code == 204
            _time_passes()
    assert len(refusing.calls) == _ceiling()


def test_a_junk_token_has_a_daily_ceiling_on_resend(client, monkeypatch):
    app_client = _relay_client(monkeypatch)
    refusing = Relay(status=400, reason="BadDeviceToken")
    with patch("app.push._post", refusing):
        r = app_client.post("/api/v1/devices", json={"platform": "apns",
                                                     "token": tok("junk")})
        auth = _auth(r.get_json()["device_id"], r.get_json()["secret"])
        answers = []
        for _ in range(_ceiling() + 10):
            _time_passes()
            answers.append(app_client.post("/api/v1/device/verify/resend",
                                           json={}, headers=auth))
    assert len(refusing.calls) == _ceiling()
    assert answers[-1].status_code == 429 and answers[-1].get_json()["retry_after"] > 3600


def test_a_device_with_nothing_to_lose_is_retired_after_three_unconfirmed_refusals(
        client, monkeypatch):
    """The sweep retried a junk registration every minute for a day when the
    platform delivered to nobody else."""
    app_client = _relay_client(monkeypatch)
    refusing = Relay(status=400, reason="BadDeviceToken")
    with patch("app.push._post", refusing):
        r = app_client.post("/api/v1/devices", json={"platform": "apns",
                                                     "token": tok("junk")})
        dev, secret = r.get_json()["device_id"], r.get_json()["secret"]
        for _ in range(20):
            _time_passes(minutes=61)       # past any backoff
            send_verifications(_db(), load_config())
    assert len(refusing.calls) == 3
    assert _row(dev)["retired_at"] is not None
    gone = app_client.get("/api/v1/device", headers=_auth(dev, secret))
    assert gone.status_code == 410 and gone.get_json()["error"] == "device_retired"
    assert _attempts(name="junk") == []                # still no budget spent


def test_a_paused_device_backs_off_and_stays_under_the_ceiling(client, monkeypatch):
    """A token change on a device with subscriptions is never retired on
    unconfirmed answers; it backs off instead, and the ceiling bounds it."""
    app_client = _relay_client(monkeypatch)
    with patch("app.push._post", Relay()):
        dev, secret = _register(app_client)
    own = _auth(dev, secret)
    _subscribe(app_client, own)
    refusing = Relay(status=400, reason="BadDeviceToken")

    def due_in():
        return _db().execute(
            "SELECT CAST(strftime('%s', verify_next_at) - strftime('%s','now') "
            "AS INTEGER) FROM push_devices WHERE id=?", (dev,)).fetchone()[0]
    with patch("app.push._post", refusing):
        _age_deliveries(dev)
        app_client.put("/api/v1/device", json={"token": tok("new")}, headers=own)
        waits = [due_in()]
        for _ in range(3):
            _time_passes(minutes=61)
            send_verifications(_db(), load_config())
            waits.append(due_in())
        assert waits[0] < waits[1] < waits[2] < waits[3]          # backs off
        for _ in range(18):                                       # still inside a day
            _time_passes(minutes=61)
            send_verifications(_db(), load_config())
    assert len(refusing.calls) == _ceiling()
    assert _row(dev)["retired_at"] is None and active_subscriptions(_db()) == []


def test_a_misconfiguration_that_spent_the_ceiling_is_forgiven_once_anyone_gets_a_push(
        client, monkeypatch):
    app_client = _relay_client(monkeypatch)
    wrong = Relay(status=400, reason="BadDeviceToken")
    with patch("app.push._post", wrong):
        for _ in range(_ceiling()):
            _time_passes()
            r = app_client.post("/api/v1/devices", json={"platform": "apns",
                                                         "token": tok()})
    dev = r.get_json()["device_id"]
    assert len(wrong.calls) == _ceiling()
    _time_passes()
    assert verify_push_wait(_db(), dev) > 3600                # the ceiling holds...
    with patch("app.push._post", Relay()) as fixed:
        _register(app_client, token="someone-else", verified=False)
        assert len(fixed.calls) == 1
        assert verify_push_wait(_db(), dev) == 0              # ...until the platform works
        _register(app_client, verified=False)
    assert len(fixed.calls) == 2 and _attempts() == ["open"]


def test_junk_on_one_platform_cannot_crowd_out_the_other(client, monkeypatch):
    """Sixty refused FCM rows, older than three APNs ones: the APNs rows
    still all go out in the first sweep."""
    from test_push import SERVICE_ACCOUNT
    fcm = [_register(client, platform="fcm", token=f"f{i}", verified=False)[0]
           for i in range(60)]
    for d in fcm:
        _backdate(d, "verify_requested_at", "-10 minutes")
    apns = [_register(client, token=f"a{i}", verified=False)[0] for i in range(3)]
    for d in apns:
        _backdate(d, "verify_requested_at", "-5 minutes")
    _enable_push(monkeypatch)
    monkeypatch.setenv("FCM_SERVICE_ACCOUNT_JSON", SERVICE_ACCOUNT)
    calls = []

    def relay(platform, url, *, headers=None, json=None, data=None):
        if data is not None:                                   # FCM token exchange
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        calls.append(platform)
        if platform == "fcm":
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})
        return httpx.Response(200, json={})
    with patch("app.push._post", relay):
        assert send_verifications(_db(), load_config()) == 3
    assert calls.count("apns") == 3 and calls.count("fcm") <= 25


def test_a_real_row_is_reached_within_a_bounded_time_behind_a_queue_of_junk(client):
    """However many junk rows are due, the sweep works through them in due
    order, 50 a cycle, and every row it tried goes behind the rest."""
    from app.repo import register_device
    db = _db()
    for i in range(120):
        register_device(db, platform="apns", token=tok(f"j{i}"), secret_hash="h" * 64,
                        language="de")
    real = register_device(db, platform="apns", token=tok("real"), secret_hash="h" * 64,
                           language="de")
    db.execute("UPDATE push_devices SET verify_requested_at=datetime('now','-10 minutes'), "
               "verify_next_at=datetime('now','-5 minutes')")
    db.execute("UPDATE push_devices SET verify_next_at=datetime('now','-1 minutes') "
               "WHERE id=?", (real,))                         # due last
    import os
    for k, v in _APNS_ENV.items():
        os.environ[k] = v
    try:
        refusing_junk = ByToken([f"j{i}" for i in range(120)], status=400,
                                reason="BadDeviceToken")
        sweeps = 0
        with patch("app.push._post", refusing_junk):
            while _row(real)["verify_sent_at"] is None and sweeps < 10:
                send_verifications(_db(), load_config())
                sweeps += 1
                _time_passes(minutes=1)
    finally:
        for k in _APNS_ENV:
            os.environ.pop(k, None)
    # 120 junk rows due first, 50 a sweep: the real one in the third.
    assert _row(real)["verify_sent_at"] is not None and sweeps == 3


def test_rows_an_outage_left_undelivered_go_to_the_back_of_the_queue(client, monkeypatch):
    devs = [_register(client, token=f"o{i}", verified=False)[0] for i in range(3)]
    for i, dev in enumerate(devs):
        _backdate(dev, "verify_requested_at", f"-{10 - i} minutes")
    _enable_push(monkeypatch)
    cfg = load_config()
    with patch("app.push.MAX_SWEEP_DEVICES", 2):
        with patch("app.push._post", Relay(status=503)) as down:
            assert send_verifications(_db(), cfg) == 0
        assert len(down.calls) == 1                    # the outage ends the turn
        with patch("app.push._post", Relay()) as up:
            assert send_verifications(_db(), cfg) == 1
        assert up.calls[0]["url"].endswith("/" + tok("o2"))
        assert all(_row(d)["verify_failures"] == 0 for d in devs)


def test_one_sweep_handles_a_bounded_number_of_devices_and_rotates(client, monkeypatch):
    devs = [_register(client, token=f"q{i}", verified=False)[0] for i in range(3)]
    for i, dev in enumerate(devs):                     # oldest request first
        _backdate(dev, "verify_requested_at", f"-{10 - i} minutes")
    _enable_push(monkeypatch)
    cfg = load_config()
    refusing = Relay(status=400)
    with patch("app.push._post", refusing), patch("app.push.MAX_SWEEP_DEVICES", 2):
        send_verifications(_db(), cfg)
        first = [c["url"].rsplit("/", 1)[1] for c in refusing.calls]
        assert first == [tok("q0"), tok("q1")]
        # The next sweep: the two just tried wait their minute, the third's turn.
        for dev in devs:
            _db().execute("UPDATE push_devices SET verify_code_at="
                          "datetime(verify_code_at, '-2 minutes') WHERE id=?", (dev,))
        send_verifications(_db(), cfg)
        assert refusing.calls[2]["url"].endswith("/" + tok("q2"))
        assert len(refusing.calls) == 3
        # Then the least recently tried come round again.
        _sweep_again(*devs)
        send_verifications(_db(), cfg)
        assert [c["url"].rsplit("/", 1)[1] for c in refusing.calls[3:]] \
            == [tok("q0"), tok("q1")]


def test_the_owner_posting_the_code_drops_a_pending_secret(relay):
    """The docstring promised it; the route answered 200 before looking."""
    client, r = relay
    dev, secret = _register(client)
    own = _auth(dev, secret)
    _age_deliveries(dev)
    _, pending = _register(client, verified=False)     # a stranger knows the token
    code = r.codes()[-1]
    row = _row(dev)
    assert row["pending_secret_hash"] and row["pending_since"] and row["verify_code_hash"]
    assert _post_code(client, own, code).get_json() == {"verified": True}
    row = _row(dev)
    assert row["pending_secret_hash"] is None and row["pending_since"] is None
    assert row["verify_code_hash"] is None and row["verified_at"] is not None
    assert client.get("/api/v1/device", headers=_auth(dev, pending)).status_code == 401
    assert _post_code(client, _auth(dev, pending), code).status_code == 401
    assert client.get("/api/v1/device", headers=own).get_json()["verified"] is True


def test_a_delivery_is_stamped_only_while_the_row_still_holds_its_code(client, monkeypatch):
    """A resend or a token change that replaced the code while the push was in
    flight has its own push to deliver; stamping would stop the sweep."""
    dev, _ = _register(client, verified=False)
    _backdate(dev, "verify_requested_at", "-1 minutes")
    _enable_push(monkeypatch)

    class Replacing(Relay):
        def __call__(self, *a, **k):
            _db().execute("UPDATE push_devices SET verify_code_hash='replaced' "
                          "WHERE id=?", (dev,))
            return super().__call__(*a, **k)
    with patch("app.push._post", Replacing()):
        assert send_verifications(_db(), load_config()) == 1
    row = _row(dev)
    assert row["verify_sent_at"] is None and row["verify_code_hash"] == "replaced"


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
