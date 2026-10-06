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
                      set_special_consent, soft_delete,
                      subscriptions_for_device, touch_device, update_device)
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


def _cfg():
    return current_app.config["TERMINE_CONFIG"]


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
    return jsonify(body), status


def _client_ip() -> str:
    from app.web import _client_ip as web_client_ip
    return web_client_ip()


def _rate_limited() -> bool:
    """Registration and every write share the subscribe form's per-IP
    budget. Reads are not counted: the app polls its subscription list on
    every launch and a rate limit on that is a self-inflicted outage."""
    return not GLOBAL_IP_LIMITER.hit(f"api:{_client_ip()}",
                                     _cfg().subscribe_ratelimit_per_ip_per_hour,
                                     3600)


def _json() -> dict:
    body = request.get_json(force=True, silent=True)
    return body if isinstance(body, dict) else {}


def authenticated(view):
    """Resolve `Authorization: Bearer <device_id>.<secret>` to a live device
    on `g.device`, or answer 401. A retired device (the relay said its token
    is dead, see push.send_push_batch) answers 410 with `device_retired`,
    which tells the app to register afresh; its subscriptions ended with the
    retirement, so there is nothing to carry over."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        bearer = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        dev_id, _, secret = bearer.partition(".")
        if not dev_id.isdigit() or not secret:
            return _error("unauthorized", 401)
        conn = connect(_cfg().db_path)
        row = device_by_id(conn, int(dev_id))
        if row is None or not hmac.compare_digest(row["secret_hash"], _hash(secret)):
            return _error("unauthorized", 401)
        if row["retired_at"] is not None:
            return _error("device_retired", 410, row["language"])
        touch_device(conn, row["id"])
        g.device = row
        g.conn = conn
        return view(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Devices

@api.route("/devices", methods=["POST"])
def register():
    """`{platform, token, language}` → `{device_id, secret, language}`.

    The same token registering again (a reinstall, a first launch re-run) is
    the same device: it takes a new secret, keeps its subscriptions and loses
    any retirement (see repo.register_device). The old secret stops working
    at once, which is the right answer for a phone that changed hands: the
    new install owns the token now."""
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
    return jsonify({"device_id": device_id, "secret": secret,
                    "language": lang}), 201


@api.route("/device", methods=["GET"])
@authenticated
def device_status():
    return jsonify(_device_json(g.device))


@api.route("/device", methods=["PUT", "PATCH"])
@authenticated
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
    return jsonify(_device_json(device_by_id(g.conn, g.device["id"])))


@api.route("/device", methods=["DELETE"])
@authenticated
def device_delete():
    """Delete my data. The row goes, the subscriptions cascade, and the
    secret in the request stops working with this response."""
    with transaction(g.conn):
        delete_device(g.conn, g.device["id"])
    return "", 204


def _device_json(row) -> dict:
    return {"device_id": row["id"], "platform": row["platform"],
            "language": row["language"], "created_at": row["created_at"],
            "subscriptions": [_sub_json(s) for s in
                              subscriptions_for_device(g.conn, row["id"])]}


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


@api.route("/subscriptions/<int:sub_id>", methods=["PUT", "PATCH"])
@authenticated
def update_subscription(sub_id):
    """Edit the filter: the same validation, consent gate and plan-cap check
    as the website's manage form, and the same reset of the cadence state,
    which was measured against the old filter."""
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
