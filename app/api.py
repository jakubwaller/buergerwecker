"""The app's JSON API, under /api/v1.

A device registers once with its push token and gets a secret; everything
after that is authenticated with `Authorization: Bearer <device_id>.<secret>`.
The secret is shown once and stored hashed (push_devices.secret_hash). There
is no account and no address: the device is the subscriber, and "delete my
data" is one DELETE that takes the device row and, by cascade, every
subscription it holds.

The rules are the website's rules. A subscription is validated against the
tenant's catalog by the same `app.signup.build_filter` the form uses, counts
toward the same per-city plan cap, needs the same Art. 9 consent for a
special-category service, lives for the same term and is asked the same
still-looking question before it ends (as a push, see housekeeping). The one
difference is the opt-in: the OS permission prompt the app had to pass is
the opt-in, so a push subscription is live at once.

Rate limits. Registration and every write, `DELETE /device` included, are
counted per client network (an IPv4 address, an IPv6 /64) in the API's own
bucket of the per-process limiter (soft, see `IPRateLimiter`), at the
subscribe form's allowance but apart from it, so app traffic behind a
carrier NAT does not use up the form for everyone there. Held across workers,
in the database: new devices per client network per day
(MAX_NEW_DEVICES_PER_IP_PER_DAY), slot-overview reads per device per hour,
verification pushes per token (see repo.token_verify_wait), and the
MAX_SUBSCRIPTIONS_PER_DEVICE live subscriptions a device may hold.

A device is not trusted until it has proven it receives our pushes. Registering
(or registering the same token again) triggers a push
carrying a one-time code; the app posts the code to `/device/verify`. The code
is stored hashed, lives 24 hours and is replaced by a resend. A push token is
checked against its platform's format (`normalize_push_token`): anything
else could name the same phone under another row.

Who may call what. The device's main credential (its `secret_hash`) may always
call `GET /device`, `PUT /device`, `DELETE /device` and the two verify routes,
verified or not, so a device waiting for its code can still report a rotated
token or delete its data (`device_owner`); every subscription route and the
slot overview also need it to be verified (`authenticated`, the default; else
403 `device_unverified`). A pending credential (a re-registration of a known
token, see repo.register_device) may call only `GET /device`, which tells it
nothing but `{"verified": false}`, and the two verify routes
(`any_credential`).

Every POST and PUT must be `application/json` (else 415): a cross-site form
or a `text/plain` fetch is sent without a CORS preflight, and would let any
web page register devices from its visitors' addresses. Every response but
the public catalog's is `Cache-Control: no-store`.

All of /api/v1, the public catalog routes and routing errors included,
answers 404 `not_available` until APP_API_ENABLED=1 (`register_cors` installs
the gate). An open registration endpoint lets anyone create subscriptions
without a confirmation step, so it stays closed until the app ships; device
verification (above) is what the open endpoint rests on.

CORS. The app's WebView calls this API cross-origin, so every response under
/api/v1 (routing errors such as an unknown path's 404 or a wrong method's 405
included, which match no blueprint) answers CORS, through app-level hooks
(`register_cors`) keyed on the path prefix, for exactly the Capacitor origins in `CORS_ORIGINS` and nobody else: the
origin is echoed only on an exact match (never `*`, never reflected), no
credentials flag (auth is a Bearer header, not a cookie), `Vary: Origin`
always. An OPTIONS preflight is answered 204 before the gate, so it succeeds
whether or not APP_API_ENABLED is set: a browser fails the real request
outright when its preflight is not 2xx, and the app must see the gated
`404 not_available` (which carries the CORS headers too) to show its "not
released yet" screen. The preflight reveals nothing the gate protects.
"""
from __future__ import annotations
import hashlib
import hmac
import ipaddress
import re
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from app.catalog import (CatalogError, available_cities, load_catalog)
from app.config import ttl_days_for
from app.db import connect, transaction
from app.models import Filter
from app.planning import would_exceed_cap
from app.ratelimit import GLOBAL_IP_LIMITER, db_rate_hit, db_rate_hit_all
from app.repo import (active_subscriptions, delete_device, device_by_id,
                      insert_push_subscription, live_subscription_count,
                      register_device, renew_subscription,
                      pending_secret_fresh, request_verification,
                      verify_push_wait,
                      set_special_consent, soft_delete,
                      subscriptions_for_device, touch_device, update_device,
                      verify_device)
from app.signup import FormError, build_filter

api = Blueprint("api", __name__, url_prefix="/api/v1")

# Live subscriptions one device may hold. The website has no equivalent
# because an address costs a confirmation click per subscription; a device
# costs nothing, so the ceiling is what keeps a looping client from filling
# a city's plan cap on its own.
MAX_SUBSCRIPTIONS_PER_DEVICE = 10
# What a push token looks like, per platform (after normalize_push_token's
# lowercasing for APNs). APNs: the device token in hex, 32 bytes today, and
# Apple reserves the right to make it longer (up to 100 bytes). FCM: a
# registration token, URL-safe base64 with a ':' after the installation id,
# about 150 to 165 characters in every format so far (the older GCM-era ones
# started at about 140); the lower bound only keeps out what is plainly not
# one, since junk can be any length, and 64 is far enough below every known
# format not to refuse a real token.
_TOKEN_PATTERNS = {
    "apns": re.compile(r"[0-9a-f]{64,200}"),
    "fcm": re.compile(r"[A-Za-z0-9_:\-]{64,4096}"),
}
# Request body ceiling for /api/v1 (see _body_limit): the largest request the
# app makes is under 1 kB.
MAX_BODY_BYTES = 16 * 1024
# Slot-overview reads one device may make per hour, across workers. The app
# reads it when the overview opens and the widget on its refresh schedule.
MAX_SLOT_READS_PER_DEVICE_PER_HOUR = 60
_LANGS = ("de", "en")
# Endpoints whose answer is the same for everyone and carries no credential.
_PUBLIC_ENDPOINTS = frozenset({"api.cities", "api.city"})

# Sentences for the errors only the API has (the website's come from
# app.web._RESULT_MESSAGES), keyed like them: {key: {lang: sentence}}.
_API_MESSAGES = {
    "device_unverified": {
        "de": "Dieses Gerät ist noch nicht bestätigt. Warte auf die Test-Benachrichtigung.",
        "en": "This device is not verified yet. Wait for the test notification.",
    },
    "invalid_code": {
        "de": "Der Code ist ungültig oder abgelaufen.",
        "en": "The code is invalid or has expired.",
    },
    "too_large": {
        "de": "Die Anfrage ist zu groß.",
        "en": "The request is too large.",
    },
    "unsupported_media_type": {
        "de": "Die Anfrage muss JSON sein.",
        "en": "The request must be JSON.",
    },
    "not_subscribed": {
        "de": "Du beobachtest in dieser Stadt noch nichts. Lege zuerst einen Alarm an.",
        "en": "You aren't watching anything in this city yet. Set up an alert first.",
    },
}


def _cfg():
    return current_app.config["TERMINE_CONFIG"]


# The WebView origins of the Capacitor app: Android https://localhost and iOS
# capacitor://localhost, the defaults, because server.androidScheme and
# server.iosScheme are unset in client/capacitor.config.json (`ios.scheme`
# there is the Xcode scheme name, not the WebView URL scheme). If either is
# ever set, this list must follow.
CORS_ORIGINS = frozenset({
    "https://localhost",
    "capacitor://localhost",
})
_CORS_METHODS = "GET, POST, PUT, DELETE, OPTIONS"
_CORS_HEADERS = "Authorization, Content-Type"
_CORS_MAX_AGE = "86400"


def _in_api() -> bool:
    return request.path == "/api/v1" or request.path.startswith("/api/v1/")


def _is_preflight() -> bool:
    return request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers


def _preflight():
    """Answer a CORS preflight for every /api/v1 path, ahead of routing's
    errors, the gate and authentication (a preflight carries no credentials).
    A disallowed origin gets a bare 204 without CORS headers, which the
    browser rejects."""
    if _in_api() and _is_preflight():
        return current_app.response_class(status=204)
    return None


def _cors(resp):
    if not _in_api():
        return resp
    resp.vary.add("Origin")
    origin = request.headers.get("Origin")
    if origin in CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        if _is_preflight():
            resp.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
            resp.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS
            resp.headers["Access-Control-Max-Age"] = _CORS_MAX_AGE
    return resp


def _gate():
    """APP_API_ENABLED, for every /api/v1 path: a routing error (an unknown
    path, a wrong method) would otherwise answer Flask's HTML 404 or 405 and
    show the API is there. Runs after the preflight and before routing's
    errors are raised."""
    if _in_api() and not _cfg().app_api_enabled:
        return jsonify({"error": "not_available"}), 404
    return None


def register_cors(app):
    """Install the app-level hooks for /api/v1, keyed on the path prefix
    rather than on the blueprint: a routing error (unknown path, wrong
    method) matches no blueprint, and it must still be gated and readable by
    the app. In order: the CORS preflight, the APP_API_ENABLED gate, and the
    CORS headers on every response."""
    app.before_request(_preflight)
    app.before_request(_gate)
    app.after_request(_cors)


@api.before_request
def _body_limit():
    """No request the app makes comes near MAX_BODY_BYTES (a subscription for
    every office of the largest city is under 1 kB). Without a limit Flask
    parsed any size: a 30 MB body to the unauthenticated register route was
    accepted, at about four times its size in memory. The app-wide default
    stays unset for the provider webhooks, which batch events."""
    request.max_content_length = MAX_BODY_BYTES
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return _error("too_large", 413)
    return None


@api.before_request
def _require_json():
    """Every POST and PUT carries a JSON body (the app sends `{}` where it
    has nothing to say). A cross-site form post or a `text/plain` fetch is a
    "simple" request the browser sends without asking first; requiring
    `application/json` forces the CORS preflight, which only the app's own
    origins pass. Without it any web page could register devices from its
    visitors' addresses, around every per-address limit."""
    if request.method in ("POST", "PUT", "PATCH") and not request.is_json:
        return _error("unsupported_media_type", 415)
    return None


@api.after_request
def _no_store(resp):
    """Nothing personal is stored on the way: every answer that depends on a
    credential, or hands one out, is `no-store` (a view may set something
    stricter, the slot overview does)."""
    if request.endpoint not in _PUBLIC_ENDPOINTS:
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _lang(value, default="de") -> str:
    return value if value in _LANGS else default


def _error(key: str, status: int, lang: str = "de", **extra):
    """`{"error": key}` plus the website's wording for the same case when it
    has one, in the device's language, so the app can show the server's
    sentence without a copy of the table."""
    from app.web import _RESULT_MESSAGES
    body = {"error": key, **extra}
    spec = _RESULT_MESSAGES.get(key)
    if spec:
        body["message"] = spec[_lang(lang)][2]
    elif key in _API_MESSAGES:
        body["message"] = _API_MESSAGES[key][_lang(lang)]
    return jsonify(body), status


def _client_ip() -> str:
    from app.web import _client_ip as web_client_ip
    return web_client_ip()


def _client_address():
    """The client's address as an ipaddress object, or the raw string when
    it is not one. An IPv6 address that carries an IPv4 address is that IPv4:
    IPv4-mapped, 6to4 (2002:<v4>::/48, 65,536 /64s for whoever holds the one
    IPv4) and Teredo (the client's IPv4)."""
    raw = _client_ip()
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo or (None, None))[1]
        if embedded is not None:
            return embedded
    return ip


def _client_network() -> str:
    """The client's address as the rate limits count it: an IPv4 address as
    it is, an IPv6 address by its /64, the smallest network one subscriber
    is handed (counting single IPv6 addresses would give each phone 2^64
    budgets); see `_client_address` for the ones that carry an IPv4."""
    ip = _client_address()
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _client_ip6_48() -> str | None:
    """The client's IPv6 /48, the coarser count on top of the /64 one, or
    None for an IPv4 client (an embedded IPv4 included)."""
    ip = _client_address()
    if isinstance(ip, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{ip}/48", strict=False))
    return None


def _network_bucket(prefix: str, network: str | None = None) -> str:
    """A database rate-limit bucket for a client network (by default the
    client's, `_client_network`), keyed by an HMAC under a key derived from
    TOKEN_SECRET_PRIMARY: the table holds no address or prefix, and a backup
    without the .env cannot be searched for one."""
    key = hashlib.sha256(
        f"ratelimit|{_cfg().token_secret_primary}".encode("utf-8")).digest()
    mac = hmac.new(key, (network or _client_network()).encode("utf-8"),
                   hashlib.sha256)
    return f"{prefix}:{mac.hexdigest()[:32]}"


def _rate_limited() -> bool:
    """Registration and every write are counted per client network in the
    API's own bucket, at the subscribe form's allowance but apart from it:
    behind one carrier NAT, app traffic must not use up the form for
    everyone. Reads are not counted: the app polls its subscription list on
    every launch and a rate limit on that is a self-inflicted outage."""
    return not GLOBAL_IP_LIMITER.hit(f"api:{_client_network()}",
                                     _cfg().subscribe_ratelimit_per_ip_per_hour,
                                     3600)


def _json() -> dict:
    """The JSON object in the body, or {} for anything else. The content
    type was checked by `_require_json`."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def normalize_push_token(platform: str, raw) -> str | None:
    """The push token as stored and sent, or None when `raw` is not one for
    `platform`. Only the platform's own alphabet and length pass: the token
    becomes a URL path segment at APNs, and `T#1`, `T?x`, `x/../T` or `./T`
    all reached `/3/device/T`, one phone behind any number of rows, each
    verifiable by the same phone. ASCII first: a lone surrogate does not
    encode as UTF-8, which was a 500, and lowercasing some non-ASCII letters
    yields ASCII ones. APNs hex is lowercased, so one token has one form."""
    if not isinstance(raw, str):
        return None
    token = raw.strip()
    if not token.isascii():
        return None
    if platform == "apns":
        token = token.lower()
    pattern = _TOKEN_PATTERNS.get(platform)
    if pattern is None or not pattern.fullmatch(token):
        return None
    return token


def _resolve_device():
    """The device behind the Authorization header, put on `g`, or an error
    response (401, or 410 for a retired device). Returns None on success."""
    header = request.headers.get("Authorization", "")
    bearer = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    dev_id, _, secret = bearer.partition(".")
    # ASCII digits only and short: str.isdigit() takes "²", int() then
    # raises, and a 30-digit id overflows SQLite; both would be an
    # unauthenticated 500.
    if (not secret or not dev_id.isascii() or not dev_id.isdigit()
            or len(dev_id) > 12):
        return _error("unauthorized", 401)
    conn = connect(_cfg().db_path)
    row = device_by_id(conn, int(dev_id))
    if row is None:
        return _error("unauthorized", 401)
    digest = _hash(secret)
    # The device's own credential is verified iff the row is. The pending one
    # (a re-registration of a verified token, see repo.register_device) works
    # for 24 hours and is never verified.
    g.credential_pending = False
    g.pending_hash = None
    # The hash that authenticated: a write on behalf of the main credential
    # repeats it in its WHERE, so a secret promoted in between stops it.
    g.secret_hash = digest
    if hmac.compare_digest(row["secret_hash"], digest):
        g.credential_verified = row["verified_at"] is not None
    elif (row["pending_secret_hash"]
          and hmac.compare_digest(row["pending_secret_hash"], digest)
          and pending_secret_fresh(conn, row["id"])):
        g.credential_verified = False
        g.credential_pending = True
        # The verify transaction promotes exactly this secret, not whatever
        # is pending by then.
        g.pending_hash = row["pending_secret_hash"]
    else:
        return _error("unauthorized", 401)
    if row["retired_at"] is not None:
        return _error("device_retired", 410, row["language"])
    if g.credential_verified:
        # An unverified row is on the 24-hour purge clock; touching it would
        # restart the 30-day one.
        touch_device(conn, row["id"])
    g.device = row
    g.conn = conn
    return None


def authenticated(view):
    """Resolve `Authorization: Bearer <device_id>.<secret>` to a live device
    on `g.device`, or answer 401. A retired device (the relay said its token
    is dead, see push.send_push_batch) answers 410 with `device_retired`,
    which tells the app to register afresh; its subscriptions ended with the
    retirement, so there is nothing to carry over. This is the locked
    default: only the main credential of a verified device passes, anything
    else answers 403 `device_unverified`. See `device_owner` and
    `any_credential` for the routes that are open to more."""
    return _guard(view, owner_only=False, verified=True)


def device_owner(view):
    """The main credential in any state, verified or not, never a pending
    one: `PUT /device` and `DELETE /device`. A device waiting for its code
    can still report a second token rotation or delete its data."""
    return _guard(view, owner_only=True, verified=False)


def any_credential(view):
    """Main or pending credential, verified or not: `GET /device` and the
    two verify routes, the only ones a pending credential may call."""
    return _guard(view, owner_only=False, verified=False)


def _guard(view, *, owner_only: bool, verified: bool):
    @wraps(view)
    def wrapper(*args, **kwargs):
        failure = _resolve_device()
        if failure is not None:
            return failure
        if ((verified and not g.credential_verified)
                or (owner_only and g.credential_pending)):
            return _error("device_unverified", 403, g.device["language"])
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Devices

@api.route("/devices", methods=["POST"])
def register():
    """`{platform, token, language}` → `{device_id, secret, language,
    verified}`; `verified` is always false here, the verification push goes
    out right after.

    The same token registering again (a reinstall, a first launch re-run) is
    the same device and keeps its subscriptions, and it never breaks the
    install that already works: on a verified device the old secret keeps
    full access and the new one is only a pending secret with the three
    unverified routes, until its holder posts the code pushed to the token;
    then it replaces the old secret, which dies at that moment (the right
    answer for a phone that changed hands). On a never-verified device the
    secret is simply rotated. See repo.register_device.

    A token no row holds yet is a new device, and a client network may add
    MAX_NEW_DEVICES_PER_IP_PER_DAY of those a rolling day, and a whole IPv6
    /48 MAX_NEW_DEVICES_PER_IP6_48_PER_DAY, across workers (429
    `rate_limited` with `retry_after`; one counts only if both have room).
    The counts outlive the rows, so deleting and registering again does not
    reset them."""
    if _rate_limited():
        return _error("rate_limited", 429)
    body = _json()
    platform = str(body.get("platform", "")).strip().lower()
    lang = _lang(body.get("language"))
    from app.push import PLATFORMS
    if platform not in PLATFORMS:
        return _error("unknown_platform", 400, lang)
    token = normalize_push_token(platform, body.get("token"))
    if token is None:
        return _error("invalid_push_token", 400, lang)
    cfg = _cfg()
    conn = connect(cfg.db_path)
    known = conn.execute("SELECT 1 FROM push_devices WHERE platform=? AND token=?",
                         (platform, token)).fetchone()
    if known is None:
        hits = [(_network_bucket("newdev"), cfg.max_new_devices_per_ip_per_day, 86400)]
        ip6_48 = _client_ip6_48()
        if ip6_48 is not None:
            hits.append((_network_bucket("newdev48", ip6_48),
                         cfg.max_new_devices_per_ip6_48_per_day, 86400))
        wait = db_rate_hit_all(conn, hits)
        if wait:
            return _error("rate_limited", 429, lang, retry_after=wait)
    secret = secrets.token_urlsafe(32)
    with transaction(conn):
        device_id = register_device(conn, platform=platform, token=token,
                                    secret_hash=_hash(secret), language=lang)
    _send_verification(conn, device_id)
    return jsonify({"device_id": device_id, "secret": secret,
                    "language": lang, "verified": False}), 201


def _send_verification(conn, device_id: int) -> None:
    """Best effort, in the request: the web container has the push
    credentials too, so this normally delivers at once. Without them (or on a
    relay failure) nothing is stamped and the poller's sweep sends it within
    a minute. Never fails the request."""
    try:
        from app.push import send_verifications
        send_verifications(conn, _cfg(), device_ids=[device_id])
    except Exception as exc:
        print(f"api: verification push for device {device_id} failed: {exc!r}",
              flush=True)


@api.route("/device", methods=["GET"])
@any_credential
def device_status():
    """The device as the main credential sees it. A pending credential (a
    re-registration of a known token that has not posted its code) learns
    only that it is not verified: whoever re-registers somebody's token must
    not read that device's id, age or language either."""
    if g.credential_pending:
        return jsonify({"verified": False})
    return jsonify(_device_json(g.device, g.credential_verified))


@api.route("/device", methods=["PUT"])
@device_owner
def device_update():
    """A rotated push token (the platforms do that) or a new language."""
    lang = g.device["language"]
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    body = _json()
    token = body.get("token")
    language = body.get("language")
    if token is not None:
        token = normalize_push_token(g.device["platform"], token)
        if token is None:
            return _error("invalid_push_token", 400, lang)
    if language is not None and language not in _LANGS:
        return _error("invalid_language", 400, lang)
    with transaction(g.conn):
        outcome = update_device(g.conn, g.device["id"], secret_hash=g.secret_hash,
                                token=token, language=language)
    if outcome == "unauthorized":
        return _error("unauthorized", 401)
    if outcome == "token_in_use":
        return _error("token_in_use", 409, lang)
    row = device_by_id(g.conn, g.device["id"])
    if token is not None and token != g.device["token"]:
        _send_verification(g.conn, row["id"])
        row = device_by_id(g.conn, row["id"])
    return jsonify(_device_json(row, row["verified_at"] is not None))


@api.route("/device", methods=["DELETE"])
@device_owner
def device_delete():
    """Delete my data. The row goes, the subscriptions cascade, and the
    secret in the request stops working with this response. Counted against
    the network's write budget, so registering and deleting in a loop runs
    out twice as fast, but never refused for it: erasure does not wait on a
    rate limit."""
    _rate_limited()
    with transaction(g.conn):
        deleted = delete_device(g.conn, g.device["id"], g.secret_hash)
    if not deleted:
        return _error("unauthorized", 401)
    return "", 204


@api.route("/device/verify", methods=["POST"])
@any_credential
def device_verify():
    """`{code}` → `{verified: true}`: the code from the verification push.
    Posting it again once verified is still 200, and a verified main
    credential posting the current code drops any pending secret with it
    (the phone answered its owner). A missing, wrong or expired code is 400
    `invalid_code`."""
    lang = g.device["language"]
    if g.credential_verified:
        code = _json().get("code")
        if g.device["pending_secret_hash"] and isinstance(code, str):
            with transaction(g.conn):
                verify_device(g.conn, g.device["id"], code.strip())
        return jsonify({"verified": True})
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    code = _json().get("code")
    with transaction(g.conn):
        ok = isinstance(code, str) and verify_device(
            g.conn, g.device["id"], code.strip(), pending_hash=g.pending_hash)
    if not ok:
        return _error("invalid_code", 400, lang)
    return jsonify({"verified": True})


@api.route("/device/verify/resend", methods=["POST"])
@any_credential
def device_verify_resend():
    """Ask for a new verification push: at most one a minute per token, and
    a daily budget per token, the main credential's apart from everyone
    else's (a pending credential's resend is a stranger's as far as the
    budget knows), counted in the database across workers and rows (see
    repo.token_verify_wait). The old code stops working."""
    lang = g.device["language"]
    if g.credential_verified:
        return jsonify({"verified": True})
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    kind = "open" if g.credential_pending else "owner"
    from app.push import config_fingerprint
    wait = verify_push_wait(g.conn, g.device["id"], kind=kind,
                            config=config_fingerprint(_cfg(), g.device["platform"]))
    if wait > 0:
        return _error("rate_limited", 429, lang, retry_after=wait)
    with transaction(g.conn):
        request_verification(g.conn, g.device["id"], code="drop_if_stale",
                             kind=kind)
    _send_verification(g.conn, g.device["id"])
    return jsonify({"verified": False}), 202


def _device_json(row, verified: bool) -> dict:
    """`verified` is the caller's credential, not the row alone. An unverified holder gets no subscriptions: whoever re-registers a
    known token must not read the real user's filters before proving they
    receive its pushes. The key stays so the shape does not change."""
    return {"device_id": row["id"], "platform": row["platform"],
            "language": row["language"], "created_at": row["created_at"],
            "verified": verified,
            "subscriptions": [_sub_json(s) for s in
                              subscriptions_for_device(g.conn, row["id"])]
                             if verified else []}


# ---------------------------------------------------------------------------
# Catalog (public, same content as the website)

@api.route("/cities", methods=["GET"])
def cities():
    """Every tenant, for the city picker: one entry per Amt with the city
    name it belongs to, like the website's switcher."""
    lang = _lang(request.args.get("lang"))
    out = []
    for slug in available_cities():
        try:
            cat = load_catalog(slug)
        except CatalogError:
            continue
        city_name = cat.display_text("city_name", lang) or slug
        out.append({
            "slug": slug,
            "city": city_name,
            "office": _office_label(cat, city_name, lang, slug),
            "label": cat.display_text("label", lang) or slug,
            "heading": cat.display_text("heading", lang),
            "note": cat.display_text("note", lang),
            "has_sensitive": cat.has_sensitive,
        })
    return jsonify({"cities": out})


@api.route("/cities/<slug>", methods=["GET"])
def city(slug):
    """One tenant's form: services, offices, which offices offer which
    service, and the terms, so the app can build the same sign-up form the
    website shows."""
    lang = _lang(request.args.get("lang"))
    try:
        cat = load_catalog(slug)
    except CatalogError:
        return _error("unknown_city", 404, lang)
    cfg = _cfg()
    services = [{"id": uuid, "name": name, "sensitive": cat.is_sensitive(uuid),
                 "locations": cat.service_locations.get(uuid)}
                for name, uuid in cat.appointment_types_for(lang).items()]
    locations = [{"id": uuid, "name": name}
                 for name, uuid in cat.locations_for(lang).items()]
    return jsonify({
        "slug": slug,
        "city": cat.display_text("city_name", lang) or slug,
        "label": cat.display_text("label", lang) or slug,
        "heading": cat.display_text("heading", lang),
        "note": cat.display_text("note", lang),
        "services": services,
        "locations": locations,
        "ttl_days": ttl_days_for(cfg, False),
        "sensitive_ttl_days": ttl_days_for(cfg, True),
        "booking_url": f"{cfg.public_base_url}/go/{slug}"
                       + ("?lang=en" if lang == "en" else ""),
    })


@api.route("/cities/<slug>/slots", methods=["GET"])
@authenticated
def city_slots(slug):
    """What the last polls found free in this city, per watched service:
    the app's live overview, and the widget's earliest slot. Read from the
    poller's snapshot (app/snapshots.py), never from the city's site, so the
    overview adds no upstream request. A service nobody watches is absent;
    the catalog endpoint lists every service, so the app can say "nobody is
    watching this one yet" for the difference. `?service=<id>` narrows the
    answer to one service.

    For a verified device that watches something in this city (a live
    subscription, not deleted, not expired; else 403 `not_subscribed`), at
    most MAX_SLOT_READS_PER_DEVICE_PER_HOUR times an hour across workers. A
    special-category service (Art. 9) is shown only to a device that watches
    that service itself: that somebody watches it is the sensitive fact, and
    the overview was answering it for anyone who asked. `private, no-store`."""
    from app.snapshots import city_slots as snapshot_slots
    lang = _lang(request.args.get("lang"))
    wait = db_rate_hit(g.conn, f"slots:{g.device['id']}",
                       MAX_SLOT_READS_PER_DEVICE_PER_HOUR, 3600)
    if wait:
        return _error("rate_limited", 429, lang, retry_after=wait)
    try:
        cat = load_catalog(slug)
    except CatalogError:
        return _error("unknown_city", 404, lang)
    now = datetime.utcnow()
    watched = [s for s in subscriptions_for_device(g.conn, g.device["id"])
               if s.city == slug and s.expires_at is not None and s.expires_at > now]
    if not watched:
        return _error("not_subscribed", 403, lang)
    own_services = {u for s in watched for u in s.sub_filter.appointment_types}
    only = request.args.get("service")
    services = []
    newest = None
    for entry in snapshot_slots(g.conn, slug):
        if only and entry["service_uuid"] != only:
            continue
        if (cat.is_sensitive(entry["service_uuid"])
                and entry["service_uuid"] not in own_services):
            continue
        slots = [{"date": d, "time": t, "location": loc,
                  "location_name": cat.location_label(loc, lang)}
                 for d, t, loc in entry["slots"]]
        services.append({
            "id": entry["service_uuid"],
            "name": cat.appointment_type_label(entry["service_uuid"], lang),
            "polled_at": _iso_sql(entry["polled_at"]),
            "n_total": entry["n_total"],
            "earliest": slots[0] if slots else None,
            "slots": slots,
        })
        if newest is None or entry["polled_at"] > newest:
            newest = entry["polled_at"]
    services.sort(key=lambda e: e["name"].casefold())
    resp = jsonify({"slug": slug, "polled_at": _iso_sql(newest), "services": services})
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _iso_sql(ts: str | None) -> str | None:
    """SQLite's UTC shape ("2026-06-08 12:00:00") as ISO 8601 with the Z the
    app's date parser needs; the subscription timestamps go out the same way
    (`_iso`)."""
    return f"{ts[:10]}T{ts[11:19]}Z" if ts else None


def _office_label(cat, city_name: str, lang: str, fallback: str) -> str:
    from app.web import _office_label as web_office_label
    return web_office_label(cat, city_name, lang, fallback)


# ---------------------------------------------------------------------------
# Subscriptions

@api.route("/subscriptions", methods=["GET"])
@authenticated
def list_subscriptions():
    return jsonify({"subscriptions": [
        _sub_json(s) for s in subscriptions_for_device(g.conn, g.device["id"])]})


@api.route("/subscriptions", methods=["POST"])
@authenticated
def create_subscription():
    """`{city, appointment_type, locations: [...] | "all", weekdays: [...],
    time_start, time_end, max_days_ahead, consent_special, language}`.
    Live at once; the first matching slot arrives as a push."""
    lang = g.device["language"]
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    body = _json()
    slug = str(body.get("city", "")).strip()
    try:
        catalog = load_catalog(slug) if slug else None
    except CatalogError:
        catalog = None
    if catalog is None:
        return _error("unknown_city", 400, lang)
    try:
        f, sensitive = _filter_from_json(body, catalog)
    except FormError as err:
        return _error(err.key, 400, lang)
    sub_lang = _lang(body.get("language"), lang)
    cfg = _cfg()
    with transaction(g.conn):
        if live_subscription_count(g.conn, g.device["id"]) >= MAX_SUBSCRIPTIONS_PER_DEVICE:
            return _error("too_many_subscriptions", 409, lang,
                          limit=MAX_SUBSCRIPTIONS_PER_DEVICE)
        existing = [(s.city, s.sub_filter) for s in active_subscriptions(g.conn)]
        if would_exceed_cap(existing, slug, f,
                            max_plans_per_city=cfg.max_plans_per_city):
            return _error("waitlist_full", 503, lang)
        sub_id = insert_push_subscription(
            g.conn, device_id=g.device["id"], city=slug, language=sub_lang,
            filter_=f, ttl_days=ttl_days_for(cfg, sensitive),
            consent_special=sensitive)
    return jsonify(_sub_json(_own(sub_id))), 201


@api.route("/subscriptions/<int:sub_id>", methods=["GET"])
@authenticated
def get_subscription(sub_id):
    sub = _own(sub_id)
    if sub is None:
        return _error("not_found", 404, g.device["language"])
    return jsonify(_sub_json(sub))


@api.route("/subscriptions/<int:sub_id>", methods=["PUT"])
@authenticated
def update_subscription(sub_id):
    """Replace the filter (a full PUT, every field as on a sign-up): the same
    validation, consent gate and plan-cap check as the website's manage
    form, and the same reset of the cadence state, which was measured
    against the old filter."""
    lang = g.device["language"]
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    sub = _own(sub_id)
    if sub is None:
        return _error("not_found", 404, lang)
    try:
        catalog = load_catalog(sub.city)
    except CatalogError:
        return _error("unknown_city", 400, lang)
    body = _json()
    try:
        f, sensitive = _filter_from_json(body, catalog)
    except FormError as err:
        return _error(err.key, 400, lang)
    cfg = _cfg()
    with transaction(g.conn):
        existing = [(s.city, s.sub_filter)
                    for s in active_subscriptions(g.conn) if s.id != sub_id]
        if would_exceed_cap(existing, sub.city, f,
                            max_plans_per_city=cfg.max_plans_per_city):
            return _error("waitlist_full", 503, lang)
        g.conn.execute("UPDATE subscriptions SET filters_json=?, "
                       "last_match_count=NULL, consecutive_digests=0 "
                       "WHERE id=?", (f.to_json(), sub_id))
        set_special_consent(g.conn, sub_id, sensitive)
        if sensitive:
            # Switching into a special-category service pulls the expiry in
            # to the shorter retention; never pushes it out.
            short = ttl_days_for(cfg, True)
            g.conn.execute(
                "UPDATE subscriptions SET expires_at=datetime('now', ?) "
                "WHERE id=? AND expires_at > datetime('now', ?)",
                (f"+{short} days", sub_id, f"+{short} days"))
        if body.get("language") in _LANGS:
            g.conn.execute("UPDATE subscriptions SET language=? WHERE id=?",
                           (body["language"], sub_id))
    return jsonify(_sub_json(_own(sub_id)))


@api.route("/subscriptions/<int:sub_id>", methods=["DELETE"])
@authenticated
def delete_subscription(sub_id):
    if _own(sub_id) is None:
        return _error("not_found", 404, g.device["language"])
    with transaction(g.conn):
        soft_delete(g.conn, sub_id)
    return "", 204


@api.route("/subscriptions/<int:sub_id>/renew", methods=["POST"])
@authenticated
def renew(sub_id):
    """"Yes, keep looking": a new term from now, the same length the website's
    renew link gives (shorter for a special-category subscription). Works on
    an expired subscription too, for as long as it exists (EXPIRED_GRACE_DAYS
    after expiry, then housekeeping deletes it)."""
    lang = g.device["language"]
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    sub = _own(sub_id)
    if sub is None:
        return _error("not_found", 404, lang)
    ttl = ttl_days_for(_cfg(), sub.consent_special)
    with transaction(g.conn):
        renew_subscription(g.conn, sub_id, ttl)
    return jsonify(_sub_json(_own(sub_id)))


def _own(sub_id: int):
    """The device's own live subscription, or None. Another device's id is
    not found, not forbidden: the id space is not this device's business."""
    for s in subscriptions_for_device(g.conn, g.device["id"]):
        if s.id == sub_id:
            return s
    return None


def _filter_from_json(body: dict, catalog) -> tuple[Filter, bool]:
    locations = body.get("locations")
    if locations is not None and locations != "all" and not isinstance(locations, list):
        raise FormError("unknown_location")
    all_locations = locations == "all" or not locations
    weekdays = body.get("weekdays")
    return build_filter(
        catalog,
        appointment_type=body.get("appointment_type"),
        locations=locations if isinstance(locations, list) else [],
        all_locations=all_locations,
        weekdays=weekdays if isinstance(weekdays, list) else [],
        time_start=body.get("time_start"),
        time_end=body.get("time_end"),
        max_days_ahead=body.get("max_days_ahead"),
        consent_special=body.get("consent_special") is True,
    )


def _sub_json(sub) -> dict:
    f = sub.sub_filter
    now = datetime.utcnow()
    return {
        "id": sub.id,
        "city": sub.city,
        "language": sub.language,
        "appointment_type": f.appointment_types[0] if f.appointment_types else None,
        "locations": f.locations,
        "weekdays": list(f.weekdays),
        "time_start": f.time_window_start.strftime("%H:%M"),
        "time_end": f.time_window_end.strftime("%H:%M"),
        "max_days_ahead": f.max_days_ahead,
        "consent_special": sub.consent_special,
        "created_at": _iso(sub.created_at),
        "expires_at": _iso(sub.expires_at),
        "last_notified_at": _iso(sub.last_notified_at),
        "active": sub.expires_at is not None and sub.expires_at > now,
    }


def _iso(dt) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None
