"""Push delivery for the app (app/push.py) and its wiring into the digest
flush, the repo, housekeeping and the schema. No network: every relay request
leaves through `app.push._post`, which these tests replace."""
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
                # what flush_digests / send_digest read
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


def _device(db, platform="apns", token="tok-1", language="de"):
    return register_device(db, platform=platform, token=token,
                           secret_hash="h" * 64, language=language)


def _push_sub(db, device_id, city="leipzig", **kw):
    return insert_push_subscription(db, device_id=device_id, city=city,
                                    language="de", filter_=_filter(),
                                    ttl_days=30, **kw)


def _item(device_id, key="k1"):
    return OutgoingPush(device_id=device_id, title="t", body="b", idem_key=key,
                        data={"url": "https://x/go/leipzig", "sub": "1",
                              "city": "leipzig"},
                        collapse_id="sub-1")


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
    assert data == {"url": "https://x/go/leipzig", "sub": "7", "city": "leipzig"}


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
    assert data == {"url": "https://x/go/sub/opaque", "sub": "7"}


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
    dev = _device(db)
    sid = _push_sub(db, dev)
    relay = FakeRelay([(410, {"reason": "Unregistered"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.retired == {dev} and res.undeliverable == {"k1"}
    assert res.delivered == set()
    row = db.execute("SELECT retired_at, retire_reason FROM push_devices "
                     "WHERE id=?", (dev,)).fetchone()
    assert row["retired_at"] is not None and row["retire_reason"] == "Unregistered"
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["deleted_at"] is not None
    assert _claimed(db, "k1") is None   # claim released


def test_apns_bad_device_token_is_a_dead_token_too(db):
    dev = _device(db)
    with patch("app.push._post", FakeRelay([(400, {"reason": "BadDeviceToken"})])):
        res = send_push_batch(db, [_item(dev)], _cfg())
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


def test_one_deferral_ends_the_platform_for_this_cycle(db):
    """During an outage every further request would cost TIMEOUT_S and block
    the poller; the second device is not even tried."""
    d1, d2 = _device(db, token="a"), _device(db, token="b")
    relay = FakeRelay([(503, {"reason": "ServiceUnavailable"})])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert res.deferred == 2 and len(relay.calls) == 1
    assert _claimed(db, "k1") is None and _claimed(db, "k2") is None


def test_platform_wide_dead_token_answers_retire_nobody(db):
    """A wrong APNS_TOPIC answers BadDeviceToken for every device. Retiring on
    that would end every app user's subscriptions in one cycle."""
    devs = [_device(db, token=f"t{i}") for i in range(3)]
    sids = [_push_sub(db, d) for d in devs]
    relay = FakeRelay([(400, {"reason": "DeviceTokenNotForTopic"})] * 3)
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in enumerate(devs)],
                              _cfg())
    assert res.retired == set() and res.deferred == 3
    assert len(live_devices(db, devs)) == 3
    alive = db.execute("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                       "AND id IN (%s)" % ",".join("?" * 3), sids).fetchone()[0]
    assert alive == 3


def test_dead_token_answers_still_retire_once_something_got_through(db):
    devs = [_device(db, token=f"t{i}") for i in range(4)]
    relay = FakeRelay([(200, {})] + [(410, {"reason": "Unregistered"})] * 3)
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in enumerate(devs)],
                              _cfg())
    assert res.delivered == {"k0"}
    assert res.retired == set(devs[1:])


def test_two_dead_tokens_are_below_the_breaker(db):
    devs = [_device(db, token=f"t{i}") for i in range(2)]
    relay = FakeRelay([(410, {"reason": "Unregistered"})] * 2)
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d, f"k{i}") for i, d in enumerate(devs)],
                              _cfg())
    assert res.retired == set(devs)


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
    assert msg["android"]["collapse_key"] == "sub-1"
    assert msg["android"]["ttl"] == "1800s"


def test_fcm_access_token_is_cached_across_sends(db):
    d1, d2 = _device(db, "fcm", "a"), _device(db, "fcm", "b")
    relay = FakeRelay([(200, {}), (200, {})])
    with patch("app.push._post", relay):
        send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
    assert relay.token_calls == 1


def test_fcm_unregistered_retires_the_device(db):
    dev = _device(db, "fcm", "dead")
    sid = _push_sub(db, dev)
    with patch("app.push._post", FakeRelay([_fcm_error(404, "UNREGISTERED",
                                                       "Requested entity was not found.",
                                                       "NOT_FOUND")])):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.retired == {dev}
    assert db.execute("SELECT retire_reason FROM push_devices WHERE id=?",
                      (dev,)).fetchone()["retire_reason"] == "UNREGISTERED"
    assert db.execute("SELECT deleted_at FROM subscriptions WHERE id=?",
                      (sid,)).fetchone()["deleted_at"] is not None


def test_fcm_invalid_argument_about_the_token_retires_but_about_the_payload_drops(db):
    d1, d2 = _device(db, "fcm", "a"), _device(db, "fcm", "b")
    relay = FakeRelay([
        _fcm_error(400, "INVALID_ARGUMENT",
                   "The registration token is not a valid FCM registration token",
                   "INVALID_ARGUMENT"),
        _fcm_error(400, "INVALID_ARGUMENT",
                   "Invalid JSON payload received. Unknown name \"foo\"",
                   "INVALID_ARGUMENT"),
    ])
    with patch("app.push._post", relay):
        res = send_push_batch(db, [_item(d1, "k1"), _item(d2, "k2")], _cfg())
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


def test_fcm_quota_429_defers(db):
    dev = _device(db, "fcm", "a")
    with patch("app.push._post", FakeRelay([_fcm_error(429, "QUOTA_EXCEEDED",
                                                       "", "RESOURCE_EXHAUSTED")])):
        res = send_push_batch(db, [_item(dev)], _cfg())
    assert res.deferred == 1


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
    retire_device(db, dev, "Unregistered")
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
    retire_device(db, dev, "Unregistered")
    again = register_device(db, platform="apns", token="same",
                            secret_hash="n" * 64, language="en")
    assert again == dev
    row = db.execute("SELECT * FROM push_devices WHERE id=?", (dev,)).fetchone()
    assert row["retired_at"] is None and row["secret_hash"] == "n" * 64
    assert row["language"] == "en"
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
    retire_device(db, retired_old, "Unregistered")
    db.execute("UPDATE push_devices SET retired_at=? WHERE id=?", (old, retired_old))
    retired_fresh = _device(db, token="r-new")
    retire_device(db, retired_fresh, "Unregistered")
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
