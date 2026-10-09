"""Push delivery for the app (app/push.py) and its wiring into the digest
flush, the repo, housekeeping and the schema. No network: every relay request
leaves through `app.push._post`, which these tests replace."""
from dataclasses import replace
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app import push
from app.catalog import Catalog
from app.db import connect, init_schema, sql_ts
from app.digest import QueuedDigest, flush_digests, send_digest
from app.mail import BatchResult, Outgoing, _idem_key
from app.models import Filter, Slot, Subscription
from app.push import OutgoingPush, PushResult, render_push, send_push_batch
from app.repo import (insert_pending, confirm, insert_push_subscription,
                      register_device, live_devices, retire_device)


# ---------------------------------------------------------------------------
# Fixtures

def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()


EC_PEM = _pem(ec.generate_private_key(ec.SECP256R1()))
RSA_PEM = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
SERVICE_ACCOUNT = (
    '{"project_id": "bw-test", "client_email": "fcm@example.com", '
    '"token_uri": "https://oauth2.example.com/token", '
    '"private_key": ' + __import__("json").dumps(RSA_PEM) + '}')


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "p.db"))
    init_schema(conn)
    return conn


@pytest.fixture(autouse=True)
def _fresh_credentials():
    push._apns_jwt.clear()
    push._fcm_token.clear()
    push._clients.clear()
    yield
    push._apns_jwt.clear()
    push._fcm_token.clear()
    push._clients.clear()


def _cfg(**over):
    base = dict(apns_team_id="TEAM123456", apns_key_id="KEY1234567",
                apns_key_p8=EC_PEM, apns_topic="de.buergerwecker.app",
                apns_sandbox=False, fcm_service_account_json=SERVICE_ACCOUNT,
                push_ttl_seconds=1800,
                # what flush_digests / send_digest read (the mail quotas for
                # the room ahead of the mail wall, see digest._behind_the_mail_wall)
                mailjet_hourly_quota=10, mailjet_daily_quota=200,
                token_secret_primary="x" * 32, token_secret_previous="",
                public_base_url="https://x", kofi_url="https://k",
                developer_email="dev@example.com")
    base.update(over)
    return SimpleNamespace(**base)


class FakeRelay:
    """Stands in for `_post`. `script` is a list of (status, json_body) answers
    handed out in order to message requests; the FCM token exchange is
    answered separately so a test can count it."""

    def __init__(self, script, token_status=200):
        self.script = list(script)
        self.token_status = token_status
        self.calls: list[dict] = []
        self.token_calls = 0

    def __call__(self, platform, url, *, headers=None, json=None, data=None):
        if data is not None and "assertion" in data:
            self.token_calls += 1
            return httpx.Response(self.token_status,
                                  json={"access_token": "ya29.test",
                                        "expires_in": 3600})
        self.calls.append(dict(platform=platform, url=url, headers=headers,
                               json=json))
        status, body = self.script.pop(0)
        return httpx.Response(status, json=body)


def _filter(types=("svc-A",)):
    return Filter(appointment_types=list(types), locations="all",
                  weekdays=[1, 2, 3, 4, 5, 6, 7],
                  time_window_start=time(0, 0), time_window_end=time(23, 59))


# The token each device of the running test was registered with, so a test
# push can be made for it (a push carries the token it was queued for).
_TOKENS: dict[int, str] = {}


def _device(db, platform="apns", token="tok-1", language="de", verified=True):
    dev = register_device(db, platform=platform, token=token,
                          secret_hash="h" * 64, language=language)
    _TOKENS[dev] = token
    if verified:
        # Verified: an unverified device's subscriptions do not run.
        db.execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP WHERE id=?",
                   (dev,))
    return dev


def _push_sub(db, device_id, city="leipzig", **kw):
    return insert_push_subscription(db, device_id=device_id, city=city,
                                    language="de", filter_=_filter(),
                                    ttl_days=30, **kw)


def _item(device_id, key="k1", token=None, kind="slots"):
    return OutgoingPush(device_id=device_id, title="t", body="b", idem_key=key,
                        data={"type": kind, "url": "https://x/go/leipzig", "sub": "1",
                              "city": "leipzig"},
                        collapse_id="sub-1",
                        token=token if token is not None else _TOKENS.get(device_id))


def _claimed(db, key):
    return db.execute("SELECT provider FROM sent_idempotency WHERE idem_key=?",
                      (key,)).fetchone()


# ---------------------------------------------------------------------------
# Rendering

def _cat(sensitive=()):
    return Catalog(
        city="leipzig",
        appointment_types={"Personalausweis": "svc-A", "Reisepass": "svc-B"},
        locations={"Bürgerbüro Mitte": "loc-1", "Bürgerbüro Nord": "loc-2",
                   "Bürgerbüro Süd": "loc-3", "Bürgerbüro West": "loc-4"},
        scraper_config={},
        appointment_types_en={"Identity card": "svc-A"},
        locations_en={"Citizen office centre": "loc-1"},
        sensitive_services=frozenset(sensitive),
    )


def _sub(**over):
    base = dict(
        id=7, email="", city="leipzig", language="de", sub_filter=_filter(),
        created_at=datetime(2026, 5, 1), confirmed_at=datetime(2026, 5, 1),
        last_notified_at=None, expires_at=datetime(2026, 8, 1),
        reminder_sent_at=None, heartbeat_30d_at=None, heartbeat_60d_at=None,
        deleted_at=None, device_id=3)
    base.update(over)
    return Subscription(**base)


def test_render_push_groups_by_office_soonest_first_and_counts_the_rest():
    slots = [
        Slot("2026-06-11", "08:00", "loc-2", "svc-A", "t"),
        Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t"),
        Slot("2026-06-10", "11:00", "loc-1", "svc-A", "t"),
        Slot("2026-06-12", "09:15", "loc-1", "svc-A", "t"),
        Slot("2026-06-12", "09:45", "loc-1", "svc-A", "t"),   # 4th at Mitte: omitted
        Slot("2026-06-13", "14:00", "loc-3", "svc-A", "t"),
        Slot("2026-06-14", "14:00", "loc-4", "svc-A", "t"),   # 4th office: omitted
    ]
    title, body, data = render_push(_sub(), slots, catalog=_cat(),
                                    booking_url="https://x/go/leipzig")
    assert title == "Neue Termine in Leipzig"
    lines = body.splitlines()
    # Soonest office leads; same-day times share one date prefix.
    assert lines[0] == "Bürgerbüro Mitte: Mi 10.06. 10:30, 11:00, Fr 12.06. 09:15"
    assert lines[1] == "Bürgerbüro Nord: Do 11.06. 08:00"
    assert lines[2] == "Bürgerbüro Süd: Sa 13.06. 14:00"
    assert lines[3] == "+2 weitere"
    assert len(lines) == 4
    assert data == {"type": "slots", "url": "https://x/go/leipzig", "sub": "7",
                    "city": "leipzig"}


def test_render_push_english():
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    title, body, _ = render_push(_sub(language="en"), slots, catalog=_cat(),
                                 booking_url="https://x/go/leipzig?lang=en")
    assert title == "New appointments in Leipzig"
    assert body == "Citizen office centre: Wed 10.06. 10:30"


def test_render_push_for_a_sensitive_service_names_nothing():
    """The payload transits Apple or Google. A count, a generic title, an
    opaque URL, and no city slug (it spells out the Amt)."""
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-B", "t"),
             Slot("2026-06-11", "09:00", "loc-2", "svc-B", "t")]
    sub = _sub(sub_filter=_filter(["svc-B"]))
    title, body, data = render_push(sub, slots, catalog=_cat(sensitive={"svc-B"}),
                                    booking_url="https://x/go/sub/opaque")
    assert title == "Neue Termine verfügbar"
    assert body == "2 neue passende Termine. Tippen, um zur Buchung zu gehen."
    for forbidden in ("Mitte", "Nord", "Reisepass", "svc-B", "leipzig", "10:30"):
        assert forbidden not in body and forbidden not in title
    assert data == {"type": "slots", "url": "https://x/go/sub/opaque", "sub": "7"}


def test_render_push_without_catalog_falls_back_to_uuids():
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    title, body, _ = render_push(_sub(city="nowhere"), slots, catalog=None,
                                 booking_url="u")
    assert title == "Neue Termine verfügbar"
    assert body == "loc-1: Mi 10.06. 10:30"


# ---------------------------------------------------------------------------
# send_digest stages a push for an app subscription

def test_send_digest_stages_a_push_item_for_a_device_subscription():
    sink = []
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    send_digest(conn=None, subscription=_sub(device_id=42), matched_slots=slots,
                cycle_id="c1", cfg=_cfg(), sink=sink)
    item = sink[0].item
    assert isinstance(item, OutgoingPush)
    assert item.device_id == 42
    assert item.idem_key == _idem_key(7, [slots[0].hash()], "c1")
    assert item.collapse_id == "sub-7"
    assert item.data["url"] == "https://x/go/leipzig"
    assert item.data["city"] == "leipzig"
    assert "10:30" in item.body


def test_send_digest_english_push_links_the_english_booking_page():
    sink = []
    send_digest(conn=None, subscription=_sub(device_id=42, language="en"),
                matched_slots=[Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")],
                cycle_id="c1", cfg=_cfg(), sink=sink)
    assert sink[0].item.data["url"] == "https://x/go/leipzig?lang=en"


def test_send_digest_without_device_still_stages_mail():
    sink = []
    send_digest(conn=None, subscription=_sub(device_id=None, email="a@example.com"),
                matched_slots=[Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")],
                cycle_id="c1", cfg=_cfg(), sink=sink)
    assert isinstance(sink[0].item, Outgoing)
    assert sink[0].item.to == "a@example.com"


# ---------------------------------------------------------------------------
# send_push_batch: APNs

def test_apns_delivery_records_the_platform_as_provider(db):
    dev = _device(db)
    relay = FakeRelay([(200, {})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.delivered == {"k1"} and res.deferred == 0
    assert res.sent_by_platform == {"apns": 1}
    assert _claimed(db, "k1")["provider"] == "apns"
    call = relay.calls[0]
    assert call["url"] == "https://api.push.apple.com/3/device/tok-1"
    assert call["headers"]["apns-topic"] == "de.buergerwecker.app"
    assert call["headers"]["apns-collapse-id"] == "sub-1"
    assert call["headers"]["apns-push-type"] == "alert"
    assert call["json"]["aps"]["alert"] == {"title": "t", "body": "b"}
    assert call["json"]["url"] == "https://x/go/leipzig"
    # The provider token is an ES256 JWT carrying the key id and team id.
    bearer = call["headers"]["authorization"].split(" ", 1)[1]
    assert jwt.get_unverified_header(bearer) == {"alg": "ES256", "kid": "KEY1234567",
                                                 "typ": "JWT"}
    assert jwt.decode(bearer, options={"verify_signature": False})["iss"] == "TEAM123456"


def test_apns_sandbox_host_for_test_builds(db):
    dev = _device(db)
    relay = FakeRelay([(200, {})])
    with patch("app.push._post", relay):
        send_push_batch(db, [_item(dev)], _cfg(apns_sandbox=True))
    assert relay.calls[0]["url"].startswith("https://api.sandbox.push.apple.com/")


def test_apns_provider_token_is_reused_across_sends(db):
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    relay = FakeRelay([(200, {}), (200, {})])
    with patch("app.push._post", relay):
        send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    bearers = {c["headers"]["authorization"] for c in relay.calls}
    assert len(bearers) == 1


def test_apns_410_retires_the_device_and_ends_its_subscriptions(db):
    live = _device(db, token="live")   # a delivery this cycle proves the platform works
    dev = _device(db)
    sid = _push_sub(db, dev)
    relay = FakeRelay([(200, {}), (410, {"reason": "Unregistered"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(live, "k0"), _item(dev)], _cfg())
    assert res.retired == {dev} and res.undeliverable == {"k1"}
    assert res.delivered == {"k0"}
    row = db.execute("SELECT retired_at, retire_reason FROM push_devices "
                     "WHERE id=?", (dev,)).fetchone()
    assert row["retired_at"] is not None and row["retire_reason"] == "Unregistered"
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["deleted_at"] is not None
    assert _claimed(db, "k1") is None   # claim released


def test_apns_bad_device_token_is_a_dead_token_too(db):
    live, dev = _device(db, token="live"), _device(db)
    with patch("app.push._post", FakeRelay([(200, {}), (400, {"reason": "BadDeviceToken"})])):
        res = send_push_batch(db, [_item(live, "k0"), _item(dev)], _cfg())
    assert res.retired == {dev}


def test_apns_payload_rejection_is_dropped_not_retried_and_keeps_the_device(db):
    dev = _device(db)
    with patch("app.push._post", FakeRelay([(400, {"reason": "PayloadTooLarge"})])):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.undeliverable == {"k1"} and res.retired == set()
    assert res.deferred == 0
    assert _claimed(db, "k1") is None
    assert live_devices(db, [dev])


def test_relay_5xx_defers_and_releases_the_claim(db):
    dev = _device(db)
    with patch("app.push._post", FakeRelay([(503, {"reason": "ServiceUnavailable"})])):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.deferred == 1 and res.delivered == set()
    assert _claimed(db, "k1") is None
    assert live_devices(db, [dev])


def test_apns_429_is_per_token_and_releases_only_that_push(db):
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    relay = FakeRelay([(429, {"reason": "TooManyRequests"}), (200, {})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert res.deferred == 1 and res.delivered == {"k2"}
    assert len(relay.calls) == 2
    assert _claimed(db, "k1") is None


def test_one_deferral_ends_the_platform_for_this_cycle(db):
    """During an outage every further request would cost TIMEOUT_S and block
    the poller; the second device is not even tried."""
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    relay = FakeRelay([(503, {"reason": "ServiceUnavailable"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert res.deferred == 2 and len(relay.calls) == 1
    assert _claimed(db, "k1") is None and _claimed(db, "k2") is None


def test_dead_token_answers_with_nothing_delivered_retire_nobody(db):
    """A wrong APNS_TOPIC answers BadDeviceToken for every device, one or a
    hundred. Retiring on that would end every app user's subscriptions."""
    devs = [_device(db, token=f"t{i}") for i in range(2)]
    sids = [_push_sub(db, d) for d in devs]
    relay = FakeRelay([(400, {"reason": "DeviceTokenNotForTopic"})] * 2)
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in enumerate(devs)],
                              _cfg())
    assert res.retired == set() and res.deferred == 2
    assert len(live_devices(db, devs)) == 2
    alive = db.execute("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                       "AND id IN (?,?)", sids).fetchone()[0]
    assert alive == 2
    since = [r[0] for r in db.execute("SELECT dead_since FROM push_devices ORDER BY id")]
    assert all(since)   # remembered for the next cycle's evidence


def test_dead_tokens_retire_once_something_got_through_this_cycle(db):
    devs = [_device(db, token=f"t{i}") for i in range(4)]
    relay = FakeRelay([(410, {"reason": "Unregistered"})] * 3 + [(200, {})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in enumerate(devs)],
                              _cfg())
    assert res.delivered == {"k3"}
    assert res.retired == set(devs[:3])   # the delivery came after them in the loop


def test_a_dead_token_is_retired_once_a_later_cycle_delivers_on_the_platform(db):
    """Cycle 1: only a dead answer, held. Cycle 2: someone else gets a push.
    Cycle 3: the same dead answer now has its evidence and retires."""
    dead, live = _device(db, token="dead"), _device(db, token="live")
    with patch("app.push._post", FakeRelay([(410, {"reason": "Unregistered"})])):
        assert send_push_batch(db, [_item(dead, "c1")], _cfg()).retired == set()
    with patch("app.push._post", FakeRelay([(200, {})])):
        send_push_batch(db, [_item(live, "c2")], _cfg())
    with patch("app.push._post", FakeRelay([(410, {"reason": "Unregistered"})])):
        res = send_push_batch(db, [_item(dead, "c3")], _cfg())
    assert res.retired == {dead}


def test_a_delivery_before_the_first_dead_answer_is_no_evidence(db):
    """A platform that worked yesterday and answers dead for everyone today
    is a configuration that changed today."""
    dead, live = _device(db, token="dead"), _device(db, token="live")
    with patch("app.push._post", FakeRelay([(200, {})])):
        send_push_batch(db, [_item(live, "c1")], _cfg())
    db.execute("UPDATE sent_idempotency SET sent_at=datetime('now','-1 day')")
    with patch("app.push._post", FakeRelay([(410, {"reason": "Unregistered"})] * 2)):
        send_push_batch(db, [_item(dead, "c2")], _cfg())
        res = send_push_batch(db, [_item(dead, "c3")], _cfg())
    assert res.retired == set() and res.deferred == 1


def test_a_delivery_clears_a_devices_dead_since(db):
    dev = _device(db)
    with patch("app.push._post", FakeRelay([(410, {"reason": "Unregistered"})])):
        send_push_batch(db, [_item(dev, "c1")], _cfg())
    assert db.execute("SELECT dead_since FROM push_devices").fetchone()[0]
    with patch("app.push._post", FakeRelay([(200, {})])):
        send_push_batch(db, [_item(dev, "c2")], _cfg())
    assert db.execute("SELECT dead_since FROM push_devices").fetchone()[0] is None


def test_relay_unreachable_defers(db):
    dev = _device(db)

    def boom(*a, **k):
        raise httpx.ConnectError("no route")
    with patch("app.push._post", boom):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.deferred == 1 and _claimed(db, "k1") is None


def test_expired_provider_token_is_renewed_and_the_push_retried_once(db):
    dev = _device(db)
    relay = FakeRelay([(403, {"reason": "ExpiredProviderToken"}), (200, {})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.delivered == {"k1"}
    assert len(relay.calls) == 2
    first, second = (c["headers"]["authorization"] for c in relay.calls)
    assert first != second   # a fresh JWT after the refusal


def test_credentials_refused_twice_defer_everything_on_that_platform(db):
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    relay = FakeRelay([(403, {"reason": "InvalidProviderToken"}),
                       (403, {"reason": "InvalidProviderToken"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert res.deferred == 2 and res.delivered == set()
    assert len(relay.calls) == 2          # the second device was not even tried
    assert _claimed(db, "k1") is None and _claimed(db, "k2") is None


# ---------------------------------------------------------------------------
# send_push_batch: FCM

def _fcm_error(status, code, message="", grpc=None):
    err = {"code": status, "message": message, "status": grpc or "UNKNOWN",
           "details": [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                        "errorCode": code}]}
    return status, {"error": err}


def test_fcm_delivery_exchanges_a_service_account_jwt_for_a_bearer(db):
    dev = _device(db, platform="fcm", token="fcm-tok")
    relay = FakeRelay([(200, {"name": "projects/bw-test/messages/1"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.delivered == {"k1"} and res.sent_by_platform == {"fcm": 1}
    assert _claimed(db, "k1")["provider"] == "fcm"
    assert relay.token_calls == 1
    call = relay.calls[0]
    assert call["url"] == "https://fcm.googleapis.com/v1/projects/bw-test/messages:send"
    assert call["headers"]["authorization"] == "Bearer ya29.test"
    msg = call["json"]["message"]
    assert msg["token"] == "fcm-tok"
    assert msg["notification"] == {"title": "t", "body": "b"}
    assert msg["data"]["url"] == "https://x/go/leipzig"
    assert msg["data"]["title"] == "t" and msg["data"]["body"] == "b"
    assert msg["android"]["collapse_key"] == "sub-1"
    assert msg["android"]["notification"] == {"channel_id": "slots", "tag": "sub-1"}
    assert msg["android"]["ttl"] == "1800s"


def test_fcm_access_token_is_cached_across_sends(db):
    d1, d2 = _device(db, "fcm", "a"), _device(db, "fcm", "b")
    relay = FakeRelay([(200, {}), (200, {})])
    with patch("app.push._post", relay):
        send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert relay.token_calls == 1


def test_fcm_unregistered_retires_the_device(db):
    live = _device(db, "fcm", "live")
    dev = _device(db, "fcm", "dead")
    sid = _push_sub(db, dev)
    with patch("app.push._post", FakeRelay([(200, {}),
                                            _fcm_error(404, "UNREGISTERED",
                                                       "Requested entity was not found.",
                                                       "NOT_FOUND")])):
        res = send_push_batch(db, [_item(live, "k0"), _item(dev)], _cfg())
    assert res.retired == {dev}
    assert db.execute("SELECT retire_reason FROM push_devices WHERE id=?",
                      (dev,)).fetchone()["retire_reason"] == "UNREGISTERED"
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["deleted_at"] is not None


def test_fcm_invalid_argument_about_the_token_retires_but_about_the_payload_drops(db):
    live, d1, d2 = _device(db, "fcm", "live"), _device(db, "fcm", "a"), _device(db, "fcm", "b")
    relay = FakeRelay([
        (200, {}),
        _fcm_error(400, "INVALID_ARGUMENT",
                   "The registration token is not a valid FCM registration token",
                   "INVALID_ARGUMENT"),
        _fcm_error(400, "INVALID_ARGUMENT",
                   "Invalid JSON payload received. Unknown name \"foo\"",
                   "INVALID_ARGUMENT"),
    ])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(live, "k0"), _item(d1, "k1"), _item(d2, "k2")],
                              _cfg())
    assert res.retired == {d1}
    assert res.undeliverable == {"k1", "k2"}
    assert set(live_devices(db, [d1, d2])) == {d2}


def test_fcm_token_exchange_failure_defers_without_sending(db):
    dev = _device(db, "fcm", "a")
    relay = FakeRelay([], token_status=401)
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.deferred == 1 and relay.calls == []
    assert relay.token_calls == 2   # once, then once more after forgetting


def test_fcm_quota_429_is_project_wide_and_ends_the_platform_for_this_cycle(db):
    d1, d2 = _device(db, "fcm", "a"), _device(db, "fcm", "b")
    relay = FakeRelay([_fcm_error(429, "QUOTA_EXCEEDED", "", "RESOURCE_EXHAUSTED")])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert res.deferred == 2 and len(relay.calls) == 1


# ---------------------------------------------------------------------------
# send_push_batch: bookkeeping shared by both platforms

def test_unconfigured_platform_is_undeliverable_without_a_claim_or_a_request(db):
    dev = _device(db, "fcm", "a")
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg(fcm_service_account_json=""))
    assert res.undeliverable == {"k1"} and relay.calls == []
    assert _claimed(db, "k1") is None


def test_retired_or_unknown_device_is_undeliverable(db):
    dev = _device(db)
    retire_device(db, dev, "Unregistered", token=_TOKENS[dev])
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev, "k1"), _item(9999, "k2")], _cfg())
    assert res.undeliverable == {"k1", "k2"} and relay.calls == []


def test_an_already_claimed_key_is_not_sent_again(db):
    dev = _device(db)
    db.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('k1','apns')")
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.delivered == set() and relay.calls == []


def test_empty_batch_is_a_noop(db):
    assert send_push_batch(db, [], _cfg()) == PushResult()


# ---------------------------------------------------------------------------
# send_push_batch: a push is bound to its token, one bad item stays one item

@pytest.mark.parametrize("token, path", [
    # Unescaped, httpx drops a fragment or a query and resolves dot segments,
    # so every one of these reached Apple as /3/device/T: one phone behind
    # any number of rows. Rows stored before the API checked the format.
    ("T#1", "/3/device/T%231"), ("x/../T", "/3/device/x%2F%2E%2E%2FT"),
    ("./T", "/3/device/%2E%2FT"), ("..", "/3/device/%2E%2E"), ("T?x", "/3/device/T%3Fx"),
])
def test_the_apns_path_carries_the_token_as_one_escaped_segment(db, token, path):
    dev = _device(db, token=token)
    relay = FakeRelay([(400, {"reason": "BadDeviceToken"})])
    with patch("app.push._post", relay):
        send_push_batch(db, [_item(dev)], _cfg())
    url = relay.calls[0]["url"]
    assert url == "https://api.push.apple.com" + path
    assert httpx.Request("POST", url).url.raw_path.decode() == path


def test_a_token_httpx_refuses_drops_that_push_alone(db):
    """A token with a control character made httpx raise InvalidURL, which
    deferred the whole platform: one poisoned row held back every push and
    the verification sweep, every cycle."""
    bad, good = _device(db, token="bad"), _device(db, token="good")

    def post(platform, url, **kw):
        if url.endswith("/bad"):
            # What httpx raises for "a\x01b" in a URL.
            raise httpx.InvalidURL("Invalid non-printable ASCII character in URL")
        return httpx.Response(200, json={})
    with patch("app.push._post", post):
        res = send_push_batch(db, [_item(bad, "k1"), _item(good, "k2")], _cfg())
    assert res.delivered == {"k2"} and res.deferred == 0
    assert res.undeliverable == {"k1"} and res.failed == {"k1"}
    assert _claimed(db, "k1") is None
    assert live_devices(db, [bad])                    # not retired either


def test_a_control_character_never_reaches_httpx_unescaped(db):
    dev = _device(db, token="a\x01b")
    relay = FakeRelay([(200, {})])
    with patch("app.push._post", relay):
        assert send_push_batch(db, [_item(dev)], _cfg()).delivered == {"k1"}
    assert relay.calls[0]["url"].endswith("/a%01b")


@pytest.mark.parametrize("error", [
    httpx.ConnectError("no route"), httpx.ReadTimeout("slow"),
    httpx.RemoteProtocolError("reset"),
    # What an HTTP/2 connection the relay closed (GOAWAY) raises on reuse.
    httpx.LocalProtocolError("Invalid input ConnectionInputs.SEND_HEADERS "
                             "in state ConnectionState.CLOSED"),
])
def test_only_network_errors_defer_the_platform(db, error):
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    calls = []

    def post(*a, **k):
        calls.append(1)
        raise error
    with patch("app.push._post", post):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert res.deferred == 2 and len(calls) == 1 and res.failed == set()


def test_credentials_that_cannot_be_built_end_the_platform_not_the_items(db):
    """A broken key is ours: it must not count as every item's failure."""
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")],
                              _cfg(apns_key_p8="not a key"))
    assert res.deferred == 2 and res.failed == set() and relay.calls == []
    assert len(live_devices(db, [d1, d2])) == 2


def test_failed_lists_item_refusals_from_a_platform_that_works(db):
    live, d1, d2, d3, d4 = (_device(db, token=t) for t in ("live", "a", "b", "c", "d"))
    relay = FakeRelay([(200, {}),
                       (400, {"reason": "PayloadTooLarge"}),
                       (429, {"reason": "TooManyRequests"}),
                       (410, {"reason": "Unregistered"}),
                       (503, {"reason": "ServiceUnavailable"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in
                                   enumerate((live, d1, d2, d3, d4))], _cfg())
    assert res.failed == {"k1", "k2", "k3"}


def test_refusals_from_a_platform_that_delivers_nothing_are_no_ones_failure(db):
    """A topic APNs does not accept, or the wrong sandbox, answers the same
    refusal for every device: none of them says anything about the item."""
    d1, d2, d3 = (_device(db, token=t) for t in "abc")
    relay = FakeRelay([(400, {"reason": "TopicDisallowed"}),
                       (429, {"reason": "TooManyRequests"}),
                       (400, {"reason": "BadDeviceToken"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in
                                   enumerate((d1, d2, d3), 1)], _cfg())
    assert res.failed == set() and res.retired == set()
    assert len(relay.calls) == 3


def test_a_push_goes_only_to_the_token_it_was_queued_for(db):
    """A verified device queued a digest, then switched to somebody else's
    token before the flush: the digest went to the new token."""
    dev = _device(db, token="mine")
    item = _item(dev)
    db.execute("UPDATE push_devices SET token='victim' WHERE id=?", (dev,))
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [item], _cfg())
    assert relay.calls == [] and res.undeliverable == {"k1"}
    assert _claimed(db, "k1") is None


def test_a_push_without_a_token_is_never_sent(db):
    dev = _device(db)
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev, token="")], _cfg())
        res2 = send_push_batch(db, [replace(_item(dev, "k2"), token=None)], _cfg())
    assert relay.calls == [] and res.undeliverable == {"k1"}
    assert res2.undeliverable == {"k2"}


def test_only_the_verification_push_reaches_an_unverified_device(db):
    dev = _device(db, verified=False)
    relay = FakeRelay([(200, {})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev, "digest"),
                                   _item(dev, "verify", kind="verify")], _cfg())
    assert res.undeliverable == {"digest"} and res.delivered == {"verify"}
    assert len(relay.calls) == 1


def test_a_device_that_never_verified_gets_the_same_evidence_rule(db):
    """Retiring it on the first dead answer looked free, but a wrong
    APNS_SANDBOX answers dead for every phone registering in that window."""
    dev, live = _device(db, verified=False), _device(db, token="live")
    dead = (410, {"reason": "Unregistered"})
    with patch("app.push._post", FakeRelay([dead])):
        res = send_push_batch(db, [_item(dev, "c1", kind="verify")], _cfg())
    assert res.retired == set() and res.failed == set() and res.deferred == 1
    assert live_devices(db, [dev])
    # Someone gets a push: the platform works, and the next dead answer retires.
    with patch("app.push._post", FakeRelay([(200, {})])):
        send_push_batch(db, [_item(live, "c2")], _cfg())
    with patch("app.push._post", FakeRelay([dead])):
        res = send_push_batch(db, [_item(dev, "c3", kind="verify")], _cfg())
    assert res.retired == {dev} and res.failed == {"c3"}
    assert not live_devices(db, [dev])


def test_a_dead_answer_beside_a_delivery_retires_at_once(db):
    live, dev = _device(db, token="live"), _device(db, verified=False)
    relay = FakeRelay([(200, {}), (410, {"reason": "Unregistered"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(live, "k0"),
                                   _item(dev, "k1", kind="verify")], _cfg())
    assert res.retired == {dev} and res.failed == {"k1"}


def test_an_unverified_device_with_paused_subscriptions_keeps_the_safeguard(db):
    """A token change un-verifies a device that holds subscriptions; a
    misconfigured platform must not end them."""
    dev = _device(db, verified=False)
    sid = _push_sub(db, dev)
    with patch("app.push._post", FakeRelay([(410, {"reason": "Unregistered"})])):
        res = send_push_batch(db, [_item(dev, kind="verify")], _cfg())
    assert res.retired == set() and res.deferred == 1
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] is None


def test_a_dead_answer_for_a_replaced_token_retires_nothing(db):
    """The relay answered dead for the old token while the device moved to a
    new one: the answer is about a token the device no longer has."""
    live, dev = _device(db, token="live"), _device(db, token="old")
    sid = _push_sub(db, dev)
    answers = iter([(200, {}), (410, {"reason": "Unregistered"})])

    def post(platform, url, **kw):
        status, body = next(answers)
        if url.endswith("/old"):
            db.execute("UPDATE push_devices SET token='new' WHERE id=?", (dev,))
        return httpx.Response(status, json=body)
    with patch("app.push._post", post):
        res = send_push_batch(db, [_item(live, "k0"), _item(dev)], _cfg())
    assert res.retired == set() and res.undeliverable == {"k1"}
    row = db.execute("SELECT retired_at, token FROM push_devices WHERE id=?",
                     (dev,)).fetchone()
    assert row["retired_at"] is None and row["token"] == "new"
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] is None


def test_retire_device_is_bound_to_the_token(db):
    dev = _device(db, token="old")
    sid = _push_sub(db, dev)
    db.execute("UPDATE push_devices SET token='new' WHERE id=?", (dev,))
    assert retire_device(db, dev, "Unregistered", token="old") is False
    assert live_devices(db, [dev])
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] is None
    assert retire_device(db, dev, "Unregistered", token="new") is True
    assert retire_device(db, dev, "Unregistered", token="new") is False
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] is not None


def test_a_held_dead_answer_is_remembered_only_for_its_token(db):
    dev = _device(db, token="old")
    _push_sub(db, dev)
    item = _item(dev)

    def post(platform, url, **kw):
        db.execute("UPDATE push_devices SET token='new' WHERE id=?", (dev,))
        return httpx.Response(410, json={"reason": "Unregistered"})
    with patch("app.push._post", post):
        send_push_batch(db, [item], _cfg())
    assert db.execute("SELECT dead_since FROM push_devices").fetchone()[0] is None


def test_the_cycle_binds_a_digest_to_the_token_it_found_verified(db):
    from app.repo import active_subscriptions
    dev = _device(db, token="mine")
    _push_sub(db, dev)
    sub = active_subscriptions(db)[0]
    assert sub.push_token == "mine"
    sink = []
    send_digest(conn=db, subscription=sub,
                matched_slots=[Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")],
                cycle_id="c1", cfg=_cfg(), sink=sink)
    assert sink[0].item.token == "mine"


# ---------------------------------------------------------------------------
# The per-cycle push budget (security review 2026-10-07: one serial relay
# request per push, so a few thousand app subscriptions held the poller, and
# every city's polling, for minutes)

def _devices(db, n):
    return [_device(db, token=f"tok-{i}") for i in range(n)]


def test_the_push_budget_caps_the_attempts_and_releases_the_rest(db):
    devs = _devices(db, 5)
    items = [_item(d, f"k{i}") for i, d in enumerate(devs)]
    relay = FakeRelay([(200, {})] * 5)
    with patch("app.push._post", relay):
        res = send_push_batch(db, items, _cfg(), max_items=2)
    assert len(relay.calls) == 2
    assert res.delivered == {"k0", "k1"} and res.deferred == 3
    # Released like a deferral: no claim left, so the next cycle sends them
    # under its own key, and the caller records nothing for them.
    assert [k for k in ("k2", "k3", "k4") if _claimed(db, k)] == []
    assert _claimed(db, "k0")["provider"] == "apns"


def test_the_push_budget_stops_on_the_clock(db):
    """A slow relay: each request takes 6 s of a 10 s budget, so the third
    push is not tried."""
    devs = _devices(db, 4)
    items = [_item(d, f"k{i}") for i, d in enumerate(devs)]
    now = [100.0]
    inner = FakeRelay([(200, {})] * 4)

    def slow(*a, **kw):
        now[0] += 6
        return inner(*a, **kw)

    with patch("app.push._post", slow), patch("app.push._clock", lambda: now[0]):
        res = send_push_batch(db, items, _cfg(), max_seconds=10)
    assert res.delivered == {"k0", "k1"} and res.deferred == 2


def test_no_budget_means_no_bound(db):
    devs = _devices(db, 3)
    relay = FakeRelay([(200, {})] * 3)
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in enumerate(devs)],
                              _cfg(), max_items=0, max_seconds=0)
    assert len(res.delivered) == 3


def _push_digest(db, dev, last_notified_at, key):
    sid = _push_sub(db, dev)
    if last_notified_at:
        db.execute("UPDATE subscriptions SET last_notified_at=? WHERE id=?",
                   (last_notified_at, sid))
    return sid, QueuedDigest(
        item=_item(dev, key),
        subscription=SimpleNamespace(id=sid, last_notified_at=last_notified_at),
        slots=[Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")], match_count=1)


def test_the_push_budget_rotates_who_goes_first(db):
    """Longest-waiting first, as mail under its quota: whoever the budget
    leaves out is not stamped, so leads the next cycle."""
    a, b = _devices(db, 2)
    sid_a, qa = _push_digest(db, a, "2026-06-01 10:00:00", "ka")
    sid_b, qb = _push_digest(db, b, "2026-06-01 08:00:00", "kb")     # waited longer
    relay = FakeRelay([(200, {})] * 2)
    with patch("app.push._post", relay), patch("app.digest.maybe_quota_alert"):
        flush_digests(db, [qa, qb], _cfg(push_budget_per_cycle=1))
    assert [c["url"].rsplit("/", 1)[1] for c in relay.calls] == ["tok-1"]   # b's
    recorded = {r[0] for r in db.execute("SELECT subscription_id FROM seen_slots")}
    assert recorded == {sid_b}
    # Next cycle: b was just served, a was not and now leads.
    stamp = db.execute("SELECT last_notified_at FROM subscriptions WHERE id=?",
                       (sid_b,)).fetchone()[0]
    qa2 = QueuedDigest(item=_item(a, "ka2"), subscription=SimpleNamespace(
        id=sid_a, last_notified_at="2026-06-01 10:00:00"), slots=qa.slots, match_count=1)
    qb2 = QueuedDigest(item=_item(b, "kb2"), subscription=SimpleNamespace(
        id=sid_b, last_notified_at=stamp), slots=qb.slots, match_count=1)
    with patch("app.push._post", relay), patch("app.digest.maybe_quota_alert"):
        flush_digests(db, [qb2, qa2], _cfg(push_budget_per_cycle=1))
    assert [c["url"].rsplit("/", 1)[1] for c in relay.calls] == ["tok-1", "tok-0"]


# ---------------------------------------------------------------------------
# flush_digests routes mail and push and records both the same way

def test_flush_records_seen_slots_for_delivered_push_and_mail_alike(db):
    mail_id = insert_pending(db, email="m@example.com", city="leipzig",
                             language="de", filter_=_filter(), ttl_days=30)
    confirm(db, mail_id)
    dev = _device(db)
    push_id = _push_sub(db, dev)
    slot = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")
    sink = [
        QueuedDigest(item=Outgoing(to="m@example.com", subject="s", body="b",
                                   idem_key="km"),
                     subscription=SimpleNamespace(id=mail_id, last_notified_at=None),
                     slots=[slot], match_count=1),
        QueuedDigest(item=_item(dev, "kp"),
                     subscription=SimpleNamespace(id=push_id, last_notified_at=None),
                     slots=[slot], match_count=1),
    ]
    with patch("app.digest.send_batch", return_value=BatchResult(delivered={"km"})) as sb, \
         patch("app.push.send_push_batch", return_value=PushResult(delivered={"kp"})) as sp, \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(db, sink, _cfg())
    assert [i.idem_key for i in sb.call_args.args[1]] == ["km"]
    assert [i.idem_key for i in sp.call_args.args[1]] == ["kp"]
    seen = {r["subscription_id"] for r in db.execute("SELECT subscription_id FROM seen_slots")}
    assert seen == {mail_id, push_id}
    notified = db.execute("SELECT COUNT(*) FROM subscriptions "
                          "WHERE last_notified_at IS NOT NULL").fetchone()[0]
    assert notified == 2


def test_flush_leaves_a_deferred_push_unrecorded(db):
    dev = _device(db)
    push_id = _push_sub(db, dev)
    slot = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")
    sink = [QueuedDigest(item=_item(dev, "kp"),
                         subscription=SimpleNamespace(id=push_id, last_notified_at=None),
                         slots=[slot], match_count=1)]
    with patch("app.digest.send_batch") as sb, \
         patch("app.push.send_push_batch", return_value=PushResult(deferred=1)), \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(db, sink, _cfg())
    sb.assert_not_called()   # nothing to mail: the mail provider is not touched
    assert db.execute("SELECT COUNT(*) FROM seen_slots").fetchone()[0] == 0


def test_flush_with_only_mail_never_imports_the_push_path(db):
    sid = insert_pending(db, email="m@example.com", city="leipzig",
                         language="de", filter_=_filter(), ttl_days=30)
    confirm(db, sid)
    sink = [QueuedDigest(item=Outgoing(to="m@example.com", subject="s", body="b",
                                       idem_key="km"),
                         subscription=SimpleNamespace(id=sid, last_notified_at=None),
                         slots=[], match_count=0)]
    with patch("app.digest.send_batch", return_value=BatchResult(delivered={"km"})), \
         patch("app.push.send_push_batch") as sp, \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(db, sink, _cfg())
    sp.assert_not_called()


def test_flush_sends_mail_and_push_side_by_side(db):
    """A slow mail provider must not hold the push batch: the push batch
    starts while the mail batch is still on the wire. Neither is scheduled
    ahead of the other (push is not a fast lane), they simply overlap. The
    push batch gets its own connection, so a mock that writes is fine too."""
    import threading
    mail_id = insert_pending(db, email="m@example.com", city="leipzig",
                             language="de", filter_=_filter(), ttl_days=30)
    confirm(db, mail_id)
    dev = _device(db)
    push_id = _push_sub(db, dev)
    slot = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")
    sink = [
        QueuedDigest(item=Outgoing(to="m@example.com", subject="s", body="b",
                                   idem_key="km"),
                     subscription=SimpleNamespace(id=mail_id, last_notified_at=None),
                     slots=[slot], match_count=1),
        QueuedDigest(item=_item(dev, "kp"),
                     subscription=SimpleNamespace(id=push_id, last_notified_at=None),
                     slots=[slot], match_count=1),
    ]
    push_started = threading.Event()
    seen = {}

    def slow_mail(conn, items, cfg):
        # Blocks until the push batch has started, or the test fails.
        seen["push_ran_during_mail"] = push_started.wait(5)
        return BatchResult(delivered={"km"})

    def push(conn, items, cfg, **budget):
        assert conn is not db          # its own connection
        conn.execute("SELECT 1").fetchone()
        push_started.set()
        return PushResult(delivered={"kp"})

    with patch("app.digest.send_batch", slow_mail), \
         patch("app.push.send_push_batch", push), \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(db, sink, _cfg())
    assert seen["push_ran_during_mail"] is True
    recorded = {r["subscription_id"] for r in db.execute("SELECT subscription_id FROM seen_slots")}
    assert recorded == {mail_id, push_id}


def test_a_failing_push_batch_does_not_lose_the_mail_bookkeeping(db):
    mail_id = insert_pending(db, email="m@example.com", city="leipzig",
                             language="de", filter_=_filter(), ttl_days=30)
    confirm(db, mail_id)
    dev = _device(db)
    push_id = _push_sub(db, dev)
    slot = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")
    sink = [
        QueuedDigest(item=Outgoing(to="m@example.com", subject="s", body="b",
                                   idem_key="km"),
                     subscription=SimpleNamespace(id=mail_id, last_notified_at=None),
                     slots=[slot], match_count=1),
        QueuedDigest(item=_item(dev, "kp"),
                     subscription=SimpleNamespace(id=push_id, last_notified_at=None),
                     slots=[slot], match_count=1),
    ]
    with patch("app.digest.send_batch", return_value=BatchResult(delivered={"km"})), \
         patch("app.push.send_push_batch", side_effect=RuntimeError("relay exploded")), \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(db, sink, _cfg())
    recorded = {r["subscription_id"] for r in db.execute("SELECT subscription_id FROM seen_slots")}
    assert recorded == {mail_id}


def test_a_failing_mail_batch_still_records_the_pushes_that_went_out(db):
    """A push that went out and is not recorded goes out again next cycle
    under a fresh key, outside the cap: the bookkeeping comes before the
    mail error is raised."""
    mail_id = insert_pending(db, email="m@example.com", city="leipzig",
                             language="de", filter_=_filter(), ttl_days=30)
    confirm(db, mail_id)
    dev = _device(db)
    push_id = _push_sub(db, dev)
    slot = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")
    sink = [
        QueuedDigest(item=Outgoing(to="m@example.com", subject="s", body="b",
                                   idem_key="km"),
                     subscription=SimpleNamespace(id=mail_id, last_notified_at=None),
                     slots=[slot], match_count=1),
        QueuedDigest(item=_item(dev, "kp"),
                     subscription=SimpleNamespace(id=push_id, last_notified_at=None),
                     slots=[slot], match_count=1),
    ]
    with patch("app.digest.send_batch", side_effect=RuntimeError("database is locked")), \
         patch("app.push.send_push_batch", return_value=PushResult(delivered={"kp"})), \
         patch("app.digest.maybe_quota_alert") as alert, \
         pytest.raises(RuntimeError, match="locked"):
        flush_digests(db, sink, _cfg())
    recorded = {r["subscription_id"] for r in db.execute("SELECT subscription_id FROM seen_slots")}
    assert recorded == {push_id}
    assert db.execute("SELECT COUNT(*) FROM digest_deliveries WHERE subscription_id=?",
                      (push_id,)).fetchone()[0] == 1
    alert.assert_not_called()


def test_flush_on_an_in_memory_database_runs_the_batches_in_turn():
    """A second connection cannot reach an in-memory database, so the two
    batches run one after the other on the one connection."""
    from app.db import connect
    conn = connect(":memory:")
    from app.db import init_schema
    init_schema(conn)
    mail_id = insert_pending(conn, email="m@example.com", city="leipzig",
                             language="de", filter_=_filter(), ttl_days=30)
    confirm(conn, mail_id)
    dev = _device(conn)
    push_id = _push_sub(conn, dev)
    slot = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")
    sink = [
        QueuedDigest(item=Outgoing(to="m@example.com", subject="s", body="b",
                                   idem_key="km"),
                     subscription=SimpleNamespace(id=mail_id, last_notified_at=None),
                     slots=[slot], match_count=1),
        QueuedDigest(item=_item(dev, "kp"),
                     subscription=SimpleNamespace(id=push_id, last_notified_at=None),
                     slots=[slot], match_count=1),
    ]
    conns = []

    def push(c, items, cfg, **budget):
        conns.append(c)
        return PushResult(delivered={"kp"})

    with patch("app.digest.send_batch", return_value=BatchResult(delivered={"km"})), \
         patch("app.push.send_push_batch", push), \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(conn, sink, _cfg())
    assert conns == [conn]
    recorded = {r["subscription_id"] for r in conn.execute("SELECT subscription_id FROM seen_slots")}
    assert recorded == {mail_id, push_id}


# ---------------------------------------------------------------------------
# The still-looking check-in as a push

def test_render_checkin_names_the_city_and_the_date_only():
    from app.push import render_checkin
    item = render_checkin("de", sub_id=7, city_name="Leipzig",
                          expires_at="2026-06-20 09:00:00")
    assert item.title == "Suchst du noch einen Termin in Leipzig?"
    assert item.body == ("Ohne Antwort stoppen die Benachrichtigungen am 20.06.2026. "
                         "Tippen, um weiter zu suchen oder zu beenden.")
    assert item.data == {"type": "checkin", "sub": "7"}
    assert item.collapse_id == "checkin-7"
    assert item.idem_key == _idem_key(7, [], "renewal-7-2026-06-20")
    en = render_checkin("en", sub_id=7, city_name=None, expires_at="2026-06-20 09:00:00")
    assert en.title == "Still looking for an appointment?"
    assert "20 June 2026" in en.body
    assert render_checkin("de", sub_id=7, city_name="X", expires_at="garbage") is None


def _checkin_cfg(**over):
    return _cfg(renewal_reminder_days_before=10, **over)


def test_housekeeping_asks_app_subscriptions_by_push_and_latches_once(db):
    from app.housekeeping import _send_push_checkins
    dev = _device(db)
    due = _push_sub(db, dev)
    later = _push_sub(db, dev)
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','+5 days') WHERE id=?", (due,))
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','+40 days') WHERE id=?", (later,))
    # A mail subscription in the same window is the mail path's business.
    mail_id = insert_pending(db, email="m@example.com", city="leipzig",
                             language="de", filter_=_filter(), ttl_days=5)
    confirm(db, mail_id)
    relay = FakeRelay([(200, {})])
    with patch("app.push._post", relay):
        _send_push_checkins(db, _checkin_cfg())
    assert len(relay.calls) == 1
    payload = relay.calls[0]["json"]
    assert payload["aps"]["alert"]["title"] == "Suchst du noch einen Termin in Leipzig?"
    assert payload["type"] == "checkin" and payload["sub"] == str(due)
    assert relay.calls[0]["headers"]["apns-collapse-id"] == f"checkin-{due}"
    stamped = {r["id"] for r in db.execute(
        "SELECT id FROM subscriptions WHERE reminder_sent_at IS NOT NULL")}
    assert stamped == {due}
    # Once per term.
    with patch("app.push._post", relay):
        _send_push_checkins(db, _checkin_cfg())
    assert len(relay.calls) == 1


def test_a_deferred_checkin_push_is_asked_again_next_run(db):
    from app.housekeeping import _send_push_checkins
    dev = _device(db)
    due = _push_sub(db, dev)
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','+5 days') WHERE id=?", (due,))
    with patch("app.push._post", FakeRelay([(503, {"reason": "ServiceUnavailable"})])):
        _send_push_checkins(db, _checkin_cfg())
    assert db.execute("SELECT reminder_sent_at FROM subscriptions WHERE id=?",
                      (due,)).fetchone()[0] is None
    assert _claimed(db, _idem_key(due, [], f"renewal-{due}-" + db.execute(
        "SELECT substr(expires_at,1,10) FROM subscriptions WHERE id=?", (due,)).fetchone()[0])) is None
    relay = FakeRelay([(200, {})])
    with patch("app.push._post", relay):
        _send_push_checkins(db, _checkin_cfg())
    assert len(relay.calls) == 1
    assert db.execute("SELECT reminder_sent_at FROM subscriptions WHERE id=?",
                      (due,)).fetchone()[0] is not None


def test_a_retired_device_gets_no_checkin(db):
    from app.housekeeping import _send_push_checkins
    dev = _device(db)
    due = _push_sub(db, dev)
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','+5 days') WHERE id=?", (due,))
    retire_device(db, dev, "Unregistered", token=_TOKENS[dev])
    relay = FakeRelay([])
    with patch("app.push._post", relay):
        _send_push_checkins(db, _checkin_cfg())
    assert relay.calls == []


def test_checkin_runs_inside_housekeeping(db, monkeypatch):
    """run_once reaches _send_push_checkins, with the config it loaded."""
    from app import housekeeping
    called = []
    monkeypatch.setattr(housekeeping, "_send_push_checkins",
                        lambda conn, cfg: called.append(cfg.renewal_reminder_days_before))
    for k, v in {"MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m",
                 "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x",
                 "MAILJET_DAILY_QUOTA": "6000", "TOKEN_SECRET_PRIMARY": "x" * 32,
                 "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
                 "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
                 "SUBSCRIPTION_TTL_DAYS": "90", "RENEWAL_REMINDER_DAYS_BEFORE": "7",
                 "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
                 "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
                 "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
                 "DEVELOPER_EMAIL": "dev@example.com", "KOFI_URL": "https://k"}.items():
        monkeypatch.setenv(k, v)
    with patch("app.mail.send"):
        housekeeping.run_once(db)
    assert called == [7]


# ---------------------------------------------------------------------------
# Repo and schema

def test_push_subscription_is_live_at_once_with_the_empty_address_sentinel(db):
    dev = _device(db)
    sid = _push_sub(db, dev)
    row = db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["email"] == "" and row["device_id"] == dev
    assert row["confirmed_at"] is not None
    from app.repo import active_subscriptions
    subs = active_subscriptions(db)
    assert [s.id for s in subs] == [sid]
    assert subs[0].is_push and subs[0].device_id == dev


def test_register_device_revives_the_same_token_and_keeps_its_subscriptions(db):
    dev = _device(db, token="same")
    sid = _push_sub(db, dev)
    retire_device(db, dev, "Unregistered", token=_TOKENS[dev])
    db.execute("UPDATE push_devices SET dead_since=CURRENT_TIMESTAMP WHERE id=?", (dev,))
    again = register_device(db, platform="apns", token="same",
                            secret_hash="n" * 64, language="en")
    assert again == dev
    row = db.execute("SELECT * FROM push_devices WHERE id=?", (dev,)).fetchone()
    # The device was verified, so the new secret is only pending.
    assert row["retired_at"] is None and row["secret_hash"] == "h" * 64
    assert row["pending_secret_hash"] == "n" * 64
    assert row["dead_since"] is None   # the evidence clock starts over
    # The retirement soft-deleted the subscription; a revived device starts
    # clean and the app re-subscribes. The row itself is still there for the
    # 30-day purge.
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["deleted_at"] is not None


def test_deleting_a_device_cascades_to_its_subscriptions(db):
    dev = _device(db)
    sid = _push_sub(db, dev)
    db.execute("INSERT INTO seen_slots (subscription_id, slot_hash) VALUES (?, 'h')", (sid,))
    db.execute("DELETE FROM push_devices WHERE id=?", (dev,))
    assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM seen_slots").fetchone()[0] == 0


def test_migration_adds_device_id_to_a_preexisting_subscriptions_table(tmp_path):
    conn = connect(str(tmp_path / "old.db"))
    conn.execute("""CREATE TABLE subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL,
        city TEXT NOT NULL DEFAULT 'leipzig', language TEXT NOT NULL DEFAULT 'de',
        filters_json TEXT NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        confirmed_at TIMESTAMP, last_notified_at TIMESTAMP,
        expires_at TIMESTAMP NOT NULL, reminder_sent_at TIMESTAMP,
        heartbeat_30d_at TIMESTAMP, heartbeat_60d_at TIMESTAMP,
        deleted_at TIMESTAMP)""")
    conn.execute("INSERT INTO subscriptions (email, filters_json, expires_at) "
                 "VALUES ('a@example.com', '{}', '2030-01-01 00:00:00')")
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    assert "device_id" in cols
    assert conn.execute("SELECT device_id FROM subscriptions").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM push_devices").fetchone()[0] == 0


def test_person_counts_do_not_collapse_app_users_into_one(db):
    """Three app users and one mail user are four people, not two."""
    for tok in ("a", "b", "c"):
        _push_sub(db, _device(db, token=tok))
    sid = insert_pending(db, email="m@example.com", city="leipzig",
                         language="de", filter_=_filter(), ttl_days=30)
    confirm(db, sid)
    n = db.execute("SELECT COUNT(DISTINCT COALESCE('d'||device_id, lower(email))) "
                   "FROM subscriptions").fetchone()[0]
    assert n == 4


# ---------------------------------------------------------------------------
# Housekeeping

@pytest.fixture
def hk_env(monkeypatch, tmp_path):
    for k, v in {
        "DB_PATH": str(tmp_path / "h.db"), "RENEWAL_REMINDER_DAYS_BEFORE": "10",
        "SUBSCRIPTION_TTL_DAYS": "90", "TOKEN_SECRET_PRIMARY": "x" * 32,
        "TOKEN_SECRET_PREVIOUS": "", "PUBLIC_BASE_URL": "https://x",
        "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m",
        "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x",
        "MAILJET_DAILY_QUOTA": "6000", "ADMIN_TOKEN": "a" * 32,
        "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
        "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
        "DEVELOPER_EMAIL": "dev@x", "KOFI_URL": "https://k",
        "WEBHOOK_SECRET": "w" * 32,
    }.items():
        monkeypatch.setenv(k, v)


def test_push_subscriptions_get_no_renewal_or_heartbeat_mail(db, hk_env):
    from app.config import load_config
    from app.housekeeping import _send_renewal_reminders, _send_heartbeats
    dev = _device(db)
    sid = _push_sub(db, dev)
    # Due for the check-in (expires in 3 days) and for the 30-day heartbeat.
    db.execute("UPDATE subscriptions SET expires_at=?, confirmed_at=? WHERE id=?",
               (sql_ts(datetime.utcnow() + timedelta(days=3)),
                sql_ts(datetime.utcnow() - timedelta(days=40)), sid))
    cfg = load_config()
    with patch("app.mail.send") as send:
        _send_renewal_reminders(db, cfg)
        _send_heartbeats(db, cfg, milestone_days=30, milestone_col="heartbeat_30d_at")
    send.assert_not_called()
    row = db.execute("SELECT reminder_sent_at, heartbeat_30d_at FROM subscriptions "
                     "WHERE id=?", (sid,)).fetchone()
    assert row["reminder_sent_at"] is None and row["heartbeat_30d_at"] is None


def test_prune_push_devices_purges_retired_and_abandoned_rows_only(db):
    from app.housekeeping import _prune_push_devices
    old = sql_ts(datetime.utcnow() - timedelta(days=31))
    retired_old = _device(db, token="r-old")
    retire_device(db, retired_old, "Unregistered", token=_TOKENS[retired_old])
    db.execute("UPDATE push_devices SET retired_at=? WHERE id=?", (old, retired_old))
    retired_fresh = _device(db, token="r-new")
    retire_device(db, retired_fresh, "Unregistered", token=_TOKENS[retired_fresh])
    abandoned = _device(db, token="abandoned")           # never subscribed
    db.execute("UPDATE push_devices SET last_seen_at=? WHERE id=?", (old, abandoned))
    dormant_with_sub = _device(db, token="dormant")      # old, but still subscribed
    _push_sub(db, dormant_with_sub)
    db.execute("UPDATE push_devices SET last_seen_at=? WHERE id=?", (old, dormant_with_sub))
    unsubscribed = _device(db, token="unsubbed")         # old, subscription long gone
    sid = _push_sub(db, unsubscribed)
    db.execute("UPDATE subscriptions SET deleted_at=? WHERE id=?", (old, sid))
    db.execute("UPDATE push_devices SET last_seen_at=? WHERE id=?", (old, unsubscribed))
    just_left = _device(db, token="just-left")           # old, but unsubscribed today
    sid2 = _push_sub(db, just_left)
    db.execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (sid2,))
    db.execute("UPDATE push_devices SET last_seen_at=? WHERE id=?", (old, just_left))
    _prune_push_devices(db)
    left = {r["token"] for r in db.execute("SELECT token FROM push_devices")}
    assert left == {"r-new", "dormant", "just-left"}
    # The cascade took the deleted subscription of the purged device along.
    assert db.execute("SELECT COUNT(*) FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()[0] == 0
