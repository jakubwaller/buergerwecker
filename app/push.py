"""Push delivery for the app: APNs for the iPhone, FCM for Android.

Same contract as `app.mail.send_batch`, so `flush_digests` treats a push digest
and a mail digest alike: claim the idempotency row first, deliver, report the
idem_keys that went out, release the claims of anything that must be retried.
The differences are the ones the relays force. One HTTP request per device
(FCM's batch endpoint is gone, APNs never had one). No quota wall to defer
against. And the dead-token signal arrives synchronously (APNs 410, FCM
UNREGISTERED) instead of through a webhook hours later, so a device is retired
on the spot rather than by a counter.

What leaves the server: a title, a body of offices with dates and times, the
city slug and a booking URL. No address, no name, no filter. For a
special-category subscription the body names nothing and the city slug stays
home too (see `render_push`): the relay payload goes through Apple or Google.
"""
from __future__ import annotations
import json
import sqlite3
import time
from dataclasses import dataclass, field

import httpx
import jwt

from app.i18n import t
from app.mail import _claim
from app.models import Slot, Subscription

APNS_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TIMEOUT_S = 10.0
# Apple: a provider token is valid for an hour and must not be refreshed more
# often than every 20 minutes. Fifty minutes keeps it comfortably inside both.
_APNS_JWT_LIFETIME_S = 50 * 60
# A push body is a lock-screen glance, not a list: this many "Office: times"
# lines, this many times per line, then a "+n more" tail.
MAX_PUSH_LINES = 3
MAX_TIMES_PER_LINE = 3

PLATFORMS = ("apns", "fcm")


class PushAuthError(Exception):
    """Our credentials were refused, not the device's token."""


@dataclass(frozen=True)
class OutgoingPush:
    device_id: int
    title: str
    body: str
    idem_key: str
    # Custom keys the app reads: `url` (booking page), `sub` (subscription id)
    # and, for an ordinary subscription, `city`.
    data: dict[str, str] = field(default_factory=dict)
    # Relay-side collapse: a newer digest for the same subscription replaces
    # the one still sitting unread on the lock screen.
    collapse_id: str | None = None


@dataclass
class PushResult:
    delivered: set[str] = field(default_factory=set)   # idem_keys that went out
    deferred: int = 0            # relay throttled or unreachable: next cycle retries
    retired: set[int] = field(default_factory=set)     # device ids now dead
    # idem_keys dropped for good this cycle: a retired or unknown device, a
    # platform this deploy has no credentials for, a payload the relay refused.
    undeliverable: set[str] = field(default_factory=set)
    sent_by_platform: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rendering

def render_push(sub: Subscription, slots: list[Slot], *, catalog,
                booking_url: str) -> tuple[str, str, dict[str, str]]:
    """(title, body, data) for a digest's worth of slots.

    Body lines are offices, soonest first, each with up to MAX_TIMES_PER_LINE
    times; the weekday+date prefix is written once per day within a line.
    Omitted slots are counted in a "+n more" tail. A special-category
    subscription (Art. 9) gets a count and nothing else: the service and the
    office are the sensitive facts, and the payload transits a relay.
    """
    from app.catalog import city_display_name
    from app.digest import _format_date
    lang = sub.language
    redacted = catalog is not None and any(
        catalog.is_sensitive(u) for u in sub.sub_filter.appointment_types)
    data = {"url": booking_url, "sub": str(sub.id)}
    if redacted:
        return (t(lang, "push.title"),
                t(lang, "push.body_redacted", n=len(slots)), data)
    city_name = city_display_name(sub.city, lang)
    title = (t(lang, "push.title_city", city=city_name) if city_name
             else t(lang, "push.title"))
    data["city"] = sub.city

    def loc_label(uuid: str) -> str:
        return catalog.location_label(uuid, lang) if catalog else uuid

    by_office: dict[str, list[Slot]] = {}
    for s in slots:
        by_office.setdefault(s.location_uuid, []).append(s)
    for office in by_office.values():
        office.sort(key=lambda s: (s.date, s.time_str))
    offices = sorted(by_office.values(),
                     key=lambda ss: (ss[0].date, ss[0].time_str))
    lines: list[str] = []
    shown = 0
    for office in offices[:MAX_PUSH_LINES]:
        parts: list[str] = []
        last_date = None
        for s in office[:MAX_TIMES_PER_LINE]:
            date_str = _format_date(s.date, lang)
            parts.append(f"{date_str} {s.time_str}" if s.date != last_date
                         else s.time_str)
            last_date = s.date
        shown += len(office[:MAX_TIMES_PER_LINE])
        lines.append(f"{loc_label(office[0].location_uuid)}: {', '.join(parts)}")
    omitted = len(slots) - shown
    if omitted > 0:
        lines.append(t(lang, "push.more", n=omitted))
    return title, "\n".join(lines), data


# ---------------------------------------------------------------------------
# Credentials and transport

def configured(cfg, platform: str) -> bool:
    if platform == "apns":
        return all(getattr(cfg, k, "") for k in
                   ("apns_team_id", "apns_key_id", "apns_key_p8", "apns_topic"))
    if platform == "fcm":
        return bool(getattr(cfg, "fcm_service_account_json", ""))
    return False


_apns_jwt: dict[str, tuple[str, float]] = {}      # key id -> (token, issued at)
_fcm_token: dict[str, tuple[str, float]] = {}     # client_email -> (token, expires at)
_clients: dict[str, httpx.Client] = {}


def _post(platform: str, url: str, *, headers: dict | None = None,
          json: dict | None = None, data: dict | None = None) -> httpx.Response:
    """Every request to a relay leaves through here, so tests can stand in for
    Apple and Google by patching one name. One client per platform, kept
    open: APNs wants a long-lived HTTP/2 connection, not one per push."""
    client = _clients.get(platform)
    if client is None:
        client = httpx.Client(http2=(platform == "apns"), timeout=TIMEOUT_S)
        _clients[platform] = client
    return client.post(url, headers=headers, json=json, data=data)


def _forget_credentials(platform: str) -> None:
    (_apns_jwt if platform == "apns" else _fcm_token).clear()


def _apns_bearer(cfg) -> str:
    cached = _apns_jwt.get(cfg.apns_key_id)
    now = time.time()
    if cached and now - cached[1] < _APNS_JWT_LIFETIME_S:
        return cached[0]
    token = jwt.encode({"iss": cfg.apns_team_id, "iat": int(now)},
                       cfg.apns_key_p8, algorithm="ES256",
                       headers={"kid": cfg.apns_key_id})
    _apns_jwt[cfg.apns_key_id] = (token, now)
    return token


def _fcm_account(cfg) -> dict:
    return json.loads(cfg.fcm_service_account_json)


def _fcm_access_token(cfg) -> str:
    """OAuth2 access token for the FCM scope via the service account's signed
    JWT assertion. Cached until a minute before it expires."""
    acct = _fcm_account(cfg)
    cached = _fcm_token.get(acct["client_email"])
    if cached and time.time() < cached[1] - 60:
        return cached[0]
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": acct["client_email"], "scope": FCM_SCOPE,
         "aud": acct["token_uri"], "iat": now, "exp": now + 3600},
        acct["private_key"], algorithm="RS256")
    resp = _post("fcm", acct["token_uri"], data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion})
    if resp.status_code != 200:
        raise PushAuthError(f"fcm token exchange: status {resp.status_code}")
    body = resp.json()
    _fcm_token[acct["client_email"]] = (
        body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


def _send_one(cfg, platform: str, item: OutgoingPush, token: str) -> httpx.Response:
    if platform == "apns":
        host = APNS_SANDBOX_HOST if cfg.apns_sandbox else APNS_HOST
        headers = {
            "authorization": f"bearer {_apns_bearer(cfg)}",
            "apns-topic": cfg.apns_topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": str(int(time.time()) + cfg.push_ttl_seconds),
        }
        if item.collapse_id:
            headers["apns-collapse-id"] = item.collapse_id[:64]
        payload = {
            "aps": {"alert": {"title": item.title, "body": item.body},
                    "sound": "default",
                    "thread-id": item.data.get("city", "buergerwecker")},
            **item.data,
        }
        return _post("apns", f"{host}/3/device/{token}",
                     headers=headers, json=payload)
    acct = _fcm_account(cfg)
    android: dict = {"priority": "high", "ttl": f"{cfg.push_ttl_seconds}s",
                     "notification": {"channel_id": "slots"}}
    if item.collapse_id:
        android["collapse_key"] = item.collapse_id
    message = {"message": {
        "token": token,
        "notification": {"title": item.title, "body": item.body},
        "data": item.data,
        "android": android,
    }}
    return _post("fcm", FCM_SEND_URL.format(project=acct["project_id"]),
                 headers={"authorization": f"Bearer {_fcm_access_token(cfg)}"},
                 json=message)


# ---------------------------------------------------------------------------
# What a relay's answer means

OK = "ok"            # delivered to the relay
RETIRE = "retire"    # the token is dead: retire the device, end its subscriptions
DEFER = "defer"      # relay down or out of quota: release, platform waits for next cycle
THROTTLE = "throttle"  # this one token is being throttled: release just this item
AUTH = "auth"        # our credentials were refused: refresh, retry once, then give up this cycle
DROP = "drop"        # the relay read our payload and refused it: retrying cannot help

_APNS_DEAD_TOKEN = {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered",
                    "ExpiredToken"}
_APNS_AUTH = {"ExpiredProviderToken", "InvalidProviderToken",
              "MissingProviderToken"}


def _json_or_empty(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _apns_verdict(resp: httpx.Response) -> tuple[str, str]:
    status = resp.status_code
    if status == 200:
        return OK, ""
    reason = str(_json_or_empty(resp).get("reason", ""))
    if status == 410 or reason in _APNS_DEAD_TOKEN:
        return RETIRE, reason or str(status)
    if status == 403 or reason in _APNS_AUTH:
        return AUTH, reason or str(status)
    if status == 429:
        # Apple: "too many requests were made consecutively to the same
        # device token". The other devices are fine.
        return THROTTLE, reason or str(status)
    if status >= 500:
        return DEFER, reason or str(status)
    return DROP, reason or str(status)


def _fcm_verdict(resp: httpx.Response) -> tuple[str, str]:
    status = resp.status_code
    if status == 200:
        return OK, ""
    err = _json_or_empty(resp).get("error", {})
    err = err if isinstance(err, dict) else {}
    fcm_code = ""
    for d in err.get("details", []) or []:
        if isinstance(d, dict) and d.get("errorCode"):
            fcm_code = str(d["errorCode"])
            break
    grpc = str(err.get("status", ""))
    message = str(err.get("message", ""))
    label = fcm_code or grpc or str(status)
    if status == 404 or fcm_code in ("UNREGISTERED", "SENDER_ID_MISMATCH"):
        return RETIRE, label
    if fcm_code == "INVALID_ARGUMENT" or grpc == "INVALID_ARGUMENT":
        # Google uses one code for "this token is garbage" and "this payload
        # is garbage"; only the message tells them apart.
        return (RETIRE if "token" in message.lower() else DROP), label
    if status in (401, 403):
        return AUTH, label
    if status == 429 or status >= 500:
        return DEFER, label
    return DROP, label


# ---------------------------------------------------------------------------
# Delivery

def send_push_batch(conn: sqlite3.Connection, items: list[OutgoingPush],
                    cfg) -> PushResult:
    """Deliver `items`, one relay request each. Returns what went out.

    Claims every idempotency row first (one transaction, as `send_batch`
    does); an already-claimed key is skipped as already-sent. A device that is
    retired, unknown, or on a platform this deploy has no credentials for is
    reported undeliverable without a claim. A dead-token answer retires the
    device only once the platform has delivered to someone since that device
    first answered dead: a wrong APNS_TOPIC, APNS_SANDBOX or FCM project makes
    every device answer dead and nothing succeed, and that must not end every
    app user's subscriptions, while three uninstalled phones are retired the
    moment any live device gets a push. An unreachable relay, a quota wall, or
    credentials refused twice end that platform's turn for the cycle; a
    per-token throttle releases just that item. Everything released is
    retried by the next cycle (fresh cycle_id), and the caller must not record
    seen_slots for it, exactly as for a quota-deferred mail.
    """
    from app.db import transaction
    from app.repo import live_devices, retire_device
    result = PushResult()
    if not items:
        return result
    devices = live_devices(conn, sorted({it.device_id for it in items}))
    pending: list[tuple[OutgoingPush, str, str]] = []
    with transaction(conn):
        for it in items:
            dev = devices.get(it.device_id)
            if dev is None or not configured(cfg, dev[0]):
                result.undeliverable.add(it.idem_key)
                continue
            if _claim(conn, it.idem_key):
                pending.append((it, dev[0], dev[1]))
    unusable: set[str] = set()
    released: list[OutgoingPush] = []
    dead: dict[str, list[tuple[OutgoingPush, str]]] = {}  # platform -> RETIRE answers
    for it, platform, token in pending:
        if platform in unusable:
            released.append(it)
            continue
        verdict, reason = _attempt(cfg, platform, it, token)
        if verdict == OK:
            with transaction(conn):
                conn.execute(
                    "UPDATE sent_idempotency SET provider=?, "
                    "sent_at=CURRENT_TIMESTAMP WHERE idem_key=?",
                    (platform, it.idem_key))
                conn.execute("UPDATE push_devices SET dead_since=NULL "
                             "WHERE id=? AND dead_since IS NOT NULL",
                             (it.device_id,))
            result.delivered.add(it.idem_key)
            result.sent_by_platform[platform] = (
                result.sent_by_platform.get(platform, 0) + 1)
        elif verdict == RETIRE:
            # Judged after the loop, once this cycle's deliveries are known.
            dead.setdefault(platform, []).append((it, reason))
        elif verdict == THROTTLE:
            released.append(it)
            print(f"push: {platform} throttling device {it.device_id} "
                  f"({reason}); retried next cycle", flush=True)
        elif verdict == DROP:
            with transaction(conn):
                conn.execute("DELETE FROM sent_idempotency WHERE idem_key=?",
                             (it.idem_key,))
            result.undeliverable.add(it.idem_key)
            print(f"push: {platform} refused payload for device "
                  f"{it.device_id}: {reason}", flush=True)
        else:  # DEFER, or AUTH after the one retry
            # One unreachable, out-of-quota or refused answer ends this
            # platform's turn for the cycle. The rest would only repeat it,
            # and during an outage each repeat costs a TIMEOUT_S the poller
            # does not have: the mail digests and every city poll wait behind
            # this loop.
            released.append(it)
            unusable.add(platform)
            print(f"push: {platform} {verdict} ({reason}); the platform waits "
                  f"for the next cycle", flush=True)
    for platform, answers in dead.items():
        # The relay said "dead token". That is also what a wrong APNS_TOPIC,
        # APNS_SANDBOX or FCM project says, for every device at once, and
        # retiring on it would end every app user's subscriptions with no way
        # back. A misconfiguration that hits the whole platform cannot produce
        # a delivery, so a device is retired only once the platform has
        # delivered to someone since this device first answered dead: in this
        # cycle, or in a later one (the first dead answer is remembered in
        # dead_since). Dead phones cost a request per cycle until then; wiped
        # subscriptions cannot be bought back. The rule is blind to a *mixed*
        # fleet: with APNS_SANDBOX=1 a development build on the sandbox
        # delivers while every TestFlight device answers BadDeviceToken, and
        # that delivery would count as evidence. The runbook keeps the VPS on
        # production for that reason.
        retire_now: list[tuple[OutgoingPush, str]] = []
        hold: list[tuple[OutgoingPush, str]] = []
        for it, reason in answers:
            if result.sent_by_platform.get(platform):
                retire_now.append((it, reason))
                continue
            row = conn.execute("SELECT dead_since FROM push_devices WHERE id=?",
                               (it.device_id,)).fetchone()
            since = row["dead_since"] if row else None
            if since and conn.execute(
                    "SELECT 1 FROM sent_idempotency WHERE provider=? "
                    "AND sent_at >= ? LIMIT 1", (platform, since)).fetchone():
                retire_now.append((it, reason))
            else:
                hold.append((it, reason))
        if retire_now:
            with transaction(conn):
                for it, reason in retire_now:
                    retire_device(conn, it.device_id, reason)
                    conn.execute("DELETE FROM sent_idempotency WHERE idem_key=?",
                                 (it.idem_key,))
            for it, reason in retire_now:
                result.retired.add(it.device_id)
                result.undeliverable.add(it.idem_key)
                print(f"push: {platform} retired device {it.device_id}: {reason}",
                      flush=True)
        if hold:
            with transaction(conn):
                conn.executemany(
                    "UPDATE push_devices SET dead_since="
                    "COALESCE(dead_since, CURRENT_TIMESTAMP) WHERE id=?",
                    [(it.device_id,) for it, _ in hold])
            released.extend(it for it, _ in hold)
            print(f"push: {platform} answered dead-token for {len(hold)} "
                  f"device(s) ({hold[0][1]}) and has delivered nothing since; "
                  f"not retiring. If every cycle says this, check APNS_TOPIC "
                  f"and APNS_SANDBOX, or the FCM project", flush=True)
    if released:
        with transaction(conn):
            conn.executemany("DELETE FROM sent_idempotency WHERE idem_key=?",
                             [(it.idem_key,) for it in released])
        result.deferred = len(released)
    return result


def _attempt(cfg, platform: str, item: OutgoingPush, token: str) -> tuple[str, str]:
    """One delivery attempt, with a single retry on refused credentials: an
    APNs provider token or an FCM access token that aged out between cycles
    is renewed and the push goes out in the same cycle."""
    verdict, reason = _try(cfg, platform, item, token)
    if verdict == AUTH:
        _forget_credentials(platform)
        verdict, reason = _try(cfg, platform, item, token)
    return verdict, reason


def _try(cfg, platform: str, item: OutgoingPush, token: str) -> tuple[str, str]:
    try:
        resp = _send_one(cfg, platform, item, token)
    except PushAuthError as exc:
        return AUTH, str(exc)
    except Exception as exc:  # the relay is unreachable, same as a 503
        return DEFER, repr(exc)
    return (_apns_verdict if platform == "apns" else _fcm_verdict)(resp)
