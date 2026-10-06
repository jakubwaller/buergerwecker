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

Rate limits: registration and every write are counted against the
subscribe-form limit per IP (soft, per process, see `IPRateLimiter`); a
device may hold MAX_SUBSCRIPTIONS_PER_DEVICE live subscriptions (hard, in
the database).

A device is not trusted until it has proven it receives our pushes. Registering
(or registering the same token again) triggers a push
carrying a one-time code; the app posts the code to `/device/verify`. The code
is stored hashed, lives 24 hours and is replaced by a resend.

Who may call what. The device's main credential (its `secret_hash`) may always
call `GET /device`, `PUT /device`, `DELETE /device` and the two verify routes,
verified or not, so a device waiting for its code can still report a rotated
token or delete its data (`device_owner`); every subscription route also
needs it to be verified (`authenticated`, the default; else 403
`device_unverified`). A pending credential (a re-registration of a verified
token, see repo.register_device) may call only `GET /device` and the two
verify routes (`any_credential`).

The whole blueprint, public catalog routes included, answers 404 until
APP_API_ENABLED=1. An open registration endpoint lets anyone create
subscriptions without a confirmation step, so it stays closed until the app
ships; device verification (above) is what the open endpoint rests on.

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
import secrets
from datetime import datetime
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request

from app.catalog import (CatalogError, available_cities, load_catalog)
from app.config import ttl_days_for
from app.db import connect, transaction
from app.models import Filter
from app.planning import would_exceed_cap
from app.ratelimit import GLOBAL_IP_LIMITER
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
# A push token is a few hundred characters on either platform; anything
# longer is not one.
_TOKEN_MAX = 4096
_LANGS = ("de", "en")

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


def register_cors(app):
    """Install the CORS hooks on the app, keyed on the path prefix rather than
    on the blueprint: a routing error (unknown path, wrong method) matches no
    blueprint, and the app must still be able to read it."""
    app.before_request(_preflight)
    app.after_request(_cors)


@api.before_request
def _gate():
    if not _cfg().app_api_enabled:
        return jsonify({"error": "not_available"}), 404
    return None


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


def _rate_limited() -> bool:
    """Registration and every write share the subscribe form's per-IP
    budget, the same bucket, so one address has one budget across the form
    and the API. Reads are not counted: the app polls its subscription list
    on every launch and a rate limit on that is a self-inflicted outage."""
    return not GLOBAL_IP_LIMITER.hit(f"ip:{_client_ip()}",
                                     _cfg().subscribe_ratelimit_per_ip_per_hour,
                                     3600)


def _json() -> dict:
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


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
    secret is simply rotated. See repo.register_device."""
    if _rate_limited():
        return _error("rate_limited", 429)
    body = _json()
    platform = str(body.get("platform", "")).strip().lower()
    token = str(body.get("token", "")).strip()
    lang = _lang(body.get("language"))
    from app.push import PLATFORMS
    if platform not in PLATFORMS:
        return _error("unknown_platform", 400)
    if not token or len(token) > _TOKEN_MAX or any(c.isspace() for c in token):
        return _error("invalid_push_token", 400)
    secret = secrets.token_urlsafe(32)
    conn = connect(_cfg().db_path)
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
    return jsonify(_device_json(g.device, g.credential_verified))


@api.route("/device", methods=["PUT"])
@device_owner
def device_update():
    """A rotated push token (the platforms do that) or a new language."""
    if _rate_limited():
        return _error("rate_limited", 429, g.device["language"])
    body = _json()
    token = body.get("token")
    language = body.get("language")
    if token is not None:
        token = str(token).strip()
        if not token or len(token) > _TOKEN_MAX or any(c.isspace() for c in token):
            return _error("invalid_push_token", 400, g.device["language"])
    if language is not None and language not in _LANGS:
        return _error("invalid_language", 400, g.device["language"])
    with transaction(g.conn):
        ok = update_device(g.conn, g.device["id"], token=token, language=language)
    if not ok:
        return _error("token_in_use", 409, g.device["language"])
    row = device_by_id(g.conn, g.device["id"])
    if token is not None and token != g.device["token"]:
        _send_verification(g.conn, row["id"])
        row = device_by_id(g.conn, row["id"])
    return jsonify(_device_json(row, row["verified_at"] is not None))


@api.route("/device", methods=["DELETE"])
@device_owner
def device_delete():
    """Delete my data. The row goes, the subscriptions cascade, and the
    secret in the request stops working with this response."""
    with transaction(g.conn):
        delete_device(g.conn, g.device["id"])
    return "", 204


@api.route("/device/verify", methods=["POST"])
@any_credential
def device_verify():
    """`{code}` → `{verified: true}`: the code from the verification push.
    Posting it again once verified is still 200. A missing, wrong or expired
    code is 400 `invalid_code`."""
    lang = g.device["language"]
    if g.credential_verified:
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
    """Ask for a new verification push, at most one a minute and
    MAX_VERIFY_PUSHES_PER_DAY a day, both measured from actual deliveries in
    the database (so they hold across workers). The old code stops working."""
    lang = g.device["language"]
    if g.credential_verified:
        return jsonify({"verified": True})
    if _rate_limited():
        return _error("rate_limited", 429, lang)
    wait = verify_push_wait(g.conn, g.device["id"])
    if wait > 0:
        return _error("rate_limited", 429, lang, retry_after=wait)
    with transaction(g.conn):
        request_verification(g.conn, g.device["id"], code="drop_if_stale")
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
def city_slots(slug):
    """What the last polls found free in this city, per watched service:
    the app's live overview, and the widget's earliest slot. Read from the
    poller's snapshot (app/snapshots.py), never from the city's site, so the
    overview adds no upstream request. A service nobody watches is absent;
    the catalog endpoint lists every service, so the app can say "nobody is
    watching this one yet" for the difference. `?service=<id>` narrows the
    answer to one service."""
    from app.snapshots import city_slots as snapshot_slots
    lang = _lang(request.args.get("lang"))
    try:
        cat = load_catalog(slug)
    except CatalogError:
        return _error("unknown_city", 404, lang)
    only = request.args.get("service")
    conn = connect(_cfg().db_path)
    services = []
    newest = None
    for entry in snapshot_slots(conn, slug):
        if only and entry["service_uuid"] != only:
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
    return jsonify({"slug": slug, "polled_at": _iso_sql(newest), "services": services})


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
