from __future__ import annotations
import logging
import os
from datetime import time as time_cls
from urllib.parse import urlencode
from flask import Flask, request, render_template, redirect
from app.config import load_config
from app.db import connect, init_schema, transaction
from app.catalog import load_catalog
from app.models import Filter
from app.repo import insert_pending, active_subscriptions, confirm, soft_delete
from app.ratelimit import GLOBAL_IP_LIMITER, email_rate_limit_ok
from app.tokens import sign, verify, InvalidToken
from app.planning import would_exceed_cap
from app.mail import send as mail_send, _idem_key

log = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> time_cls:
    h, m = s.split(":")
    return time_cls(int(h), int(m))


def _send_confirmation_email(conn, sub_id: int, email: str, lang: str, cfg) -> None:
    tok = sign(sub_id, "confirm",
               primary=cfg.token_secret_primary,
               previous=cfg.token_secret_previous)
    url = f"{cfg.public_base_url}/confirm/{tok}"
    body_de = f"Bitte bestätige dein Abonnement: {url}"
    body_en = f"Please confirm your subscription: {url}"
    body = body_en if lang == "en" else body_de
    subj = "Bestätigung benötigt" if lang == "de" else "Confirmation needed"
    key = _idem_key(sub_id, [], f"confirm-{sub_id}")
    mail_send(conn, email, subj, body, idem_key=key)


def _send_manage_link_email(conn, sub_id: int, cfg) -> None:
    """Sends a separate email with the /manage link - NEVER in digests."""
    row = conn.execute(
        "SELECT email, language FROM subscriptions WHERE id=?",
        (sub_id,),
    ).fetchone()
    if not row:
        return
    tok = sign(sub_id, "manage",
               primary=cfg.token_secret_primary,
               previous=cfg.token_secret_previous)
    url = f"{cfg.public_base_url}/manage/{tok}"
    if row["language"] == "de":
        body = (f"Dein Verwaltungs-Link: {url}\nMit diesem Link kannst du deine "
                f"Einstellungen jederzeit ändern oder dich abmelden.")
        subj = "Verwaltungs-Link"
    else:
        body = (f"Your management link: {url}\nUse it any time to change your "
                f"settings or unsubscribe.")
        subj = "Management link"
    key = _idem_key(sub_id, [], f"manage-link-{sub_id}")
    mail_send(conn, row["email"], subj, body, idem_key=key)


def create_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder=None)
    # Load config ONCE at startup. Missing env vars surface here, not on
    # the first real request.
    app.config["TERMINE_CONFIG"] = load_config()

    @app.context_processor
    def _template_helpers():
        # Build the language-switch URL by preserving the current query string
        # and overriding only `lang`. A bare `?lang=xx` would drop every other
        # param — most importantly the admin `?token=`, which then 401s, and
        # the form's `city` / `confirmed` / `subscribe_error`.
        def switch_lang_url(target_lang: str) -> str:
            args = request.args.to_dict(flat=True)
            args["lang"] = target_lang
            return f"{request.path}?{urlencode(args)}"
        return {"switch_lang_url": switch_lang_url}

    @app.route("/healthz")
    def healthz():
        cfg = app.config["TERMINE_CONFIG"]
        conn = connect(cfg.db_path)
        conn.execute("SELECT 1").fetchone()
        return "ok", 200

    @app.route("/")
    def index():
        lang = request.args.get("lang", "de")
        if lang not in ("de", "en"):
            lang = "de"
        city = request.args.get("city", "leipzig")
        # `confirmed=pending` / `subscribe_error=mail` are set by the /subscribe
        # redirect so the form can show a "check your inbox" banner or a
        # retryable error instead of silently re-rendering.
        confirmed = request.args.get("confirmed")
        error = request.args.get("subscribe_error")
        catalog = load_catalog(city)
        return render_template("form.html",
                               lang=lang,
                               city=city,
                               confirmed=confirmed,
                               error=error,
                               appointment_types=catalog.appointment_types_for(lang),
                               locations=catalog.locations_for(lang),
                               kofi_url=app.config["TERMINE_CONFIG"].kofi_url)

    @app.route("/subscribe", methods=["POST"])
    def subscribe():
        # 1. honeypot
        if request.form.get("website", ""):
            return ("", 200)
        cfg = app.config["TERMINE_CONFIG"]
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            return ("Invalid email", 400)
        # 2. per-IP rate limit (in-memory, soft)
        if not GLOBAL_IP_LIMITER.hit(f"ip:{ip}",
                                     cfg.subscribe_ratelimit_per_ip_per_hour,
                                     3600):
            return ("Rate limit exceeded", 429)
        # 3. per-email rate limit (DB-backed, hard - shared across workers)
        conn_for_check = connect(cfg.db_path)
        if not email_rate_limit_ok(conn_for_check, email,
                                   cfg.subscribe_ratelimit_per_email_per_day):
            return ("Rate limit exceeded", 429)
        # 4. parse filter from form
        lang = request.form.get("lang", "de")
        city = request.form.get("city", "leipzig")
        atype = request.form.get("appointment_type", "").strip()
        if not atype:
            return ("Missing appointment_type", 400)
        all_locs = request.form.get("all_locations") == "1"
        loc_list = request.form.getlist("locations")
        locations = "all" if all_locs or not loc_list else loc_list
        weekdays = [int(d) for d in request.form.getlist("weekdays") if d.isdigit()]
        if not weekdays:
            weekdays = [1, 2, 3, 4, 5, 6, 7]
        ts = request.form.get("time_start", "00:00")
        te = request.form.get("time_end", "23:59")
        f = Filter(
            appointment_types=[atype],
            locations=locations,
            weekdays=weekdays,
            time_window_start=_parse_hhmm(ts),
            time_window_end=_parse_hhmm(te),
        )
        # 5. plan-cap overflow check + insert atomically (spec 3.2.6).
        conn = connect(cfg.db_path)
        with transaction(conn):
            existing = [(s.city, s.sub_filter) for s in active_subscriptions(conn)]
            if would_exceed_cap(existing, city, f,
                                max_plans_per_city=cfg.max_plans_per_city):
                msg = (("Aktuell ist die Warteliste voll. "
                        "Bitte in ein paar Tagen erneut versuchen.")
                       if lang == "de"
                       else "The wait-list is currently full. Please try again in a few days.")
                return (msg, 503)
            sub_id = insert_pending(conn, email=email, city=city,
                                    language=lang, filter_=f,
                                    ttl_days=cfg.subscription_ttl_days)
        # The confirmation email is the ONLY way the user can complete signup,
        # so — unlike the manage-link email in /confirm — we must NOT claim
        # success if it fails. Surface a retryable error instead of a 500 and
        # drop the orphaned pending row so it can't linger unconfirmable.
        try:
            _send_confirmation_email(conn, sub_id, email, lang, cfg)
        except Exception:
            log.exception("confirmation email failed for sub %s", sub_id)
            soft_delete(conn, sub_id)
            return redirect("/?subscribe_error=mail")
        return redirect("/?confirmed=pending")

    @app.route("/confirm/<token>")
    def confirm_route(token):
        cfg = app.config["TERMINE_CONFIG"]
        try:
            sub_id = verify(token, "confirm",
                            primary=cfg.token_secret_primary,
                            previous=cfg.token_secret_previous)
        except InvalidToken:
            return ("Invalid token", 400)
        conn = connect(cfg.db_path)
        confirm(conn, sub_id)
        row = conn.execute("SELECT language FROM subscriptions WHERE id=?",
                           (sub_id,)).fetchone()
        lang = row["language"] if row else "de"
        # The management-link email is a convenience, NOT part of confirmation.
        # The subscription is already confirmed above (autocommit), so a
        # mail-provider failure must never turn this into a 500 — log it and
        # still show the user their success page.
        try:
            _send_manage_link_email(conn, sub_id, cfg)
        except Exception:
            log.exception(
                "manage-link email failed for sub %s; confirmation still succeeded",
                sub_id,
            )
        return render_template("confirmed.html", lang=lang,
                               kofi_url=cfg.kofi_url), 200

    @app.route("/unsubscribe/<token>")
    def unsubscribe_route(token):
        cfg = app.config["TERMINE_CONFIG"]
        try:
            sub_id = verify(token, "unsubscribe",
                            primary=cfg.token_secret_primary,
                            previous=cfg.token_secret_previous)
        except InvalidToken:
            return ("Invalid token", 400)
        conn = connect(cfg.db_path)
        soft_delete(conn, sub_id)
        return ("Unsubscribed.", 200)

    @app.route("/manage/<token>", methods=["GET", "POST"])
    def manage_route(token):
        cfg = app.config["TERMINE_CONFIG"]
        try:
            sub_id = verify(token, "manage",
                            primary=cfg.token_secret_primary,
                            previous=cfg.token_secret_previous)
        except InvalidToken:
            return ("Invalid token", 400)
        conn = connect(cfg.db_path)
        if request.method == "POST":
            atype = request.form.get("appointment_type", "").strip()
            if not atype:
                return ("Missing appointment_type", 400)
            all_locs = request.form.get("all_locations") == "1"
            loc_list = request.form.getlist("locations")
            locations = "all" if all_locs or not loc_list else loc_list
            weekdays = [int(d) for d in request.form.getlist("weekdays") if d.isdigit()] or [1, 2, 3, 4, 5, 6, 7]
            ts = request.form.get("time_start", "00:00")
            te = request.form.get("time_end", "23:59")
            f = Filter(appointment_types=[atype], locations=locations,
                       weekdays=weekdays,
                       time_window_start=_parse_hhmm(ts),
                       time_window_end=_parse_hhmm(te))
            conn.execute("UPDATE subscriptions SET filters_json=? WHERE id=?",
                         (f.to_json(), sub_id))
            return ("Updated.", 200)
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row or row["deleted_at"] is not None:
            return ("Subscription not found", 404)
        catalog = load_catalog(row["city"])
        lang = row["language"]
        return render_template("manage.html",
                               lang=lang, city=row["city"],
                               appointment_types=catalog.appointment_types_for(lang),
                               locations=catalog.locations_for(lang), token=token,
                               current=Filter.from_json(row["filters_json"]))

    @app.route("/renew/<token>")
    def renew_route(token):
        cfg = app.config["TERMINE_CONFIG"]
        try:
            sid = verify(token, "renew",
                         primary=cfg.token_secret_primary,
                         previous=cfg.token_secret_previous)
        except InvalidToken:
            return ("Invalid token", 400)
        conn = connect(cfg.db_path)
        conn.execute(
            "UPDATE subscriptions SET expires_at=datetime('now', ?) "
            "WHERE id=? AND deleted_at IS NULL",
            (f"+{cfg.subscription_ttl_days} days", sid),
        )
        return ("Subscription renewed.", 200)

    @app.route("/go/<slot_token>")
    def go_route(slot_token):
        cfg = app.config["TERMINE_CONFIG"]
        conn = connect(cfg.db_path)
        row = conn.execute(
            "SELECT upstream_url FROM slots_cache WHERE slot_token=?",
            (slot_token,),
        ).fetchone()
        if not row:
            return ("This appointment link has expired.", 410)
        return redirect(row["upstream_url"], code=302)

    @app.route("/admin")
    def admin_route():
        cfg = app.config["TERMINE_CONFIG"]
        token = (request.args.get("token") or
                 (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()))
        # Hash both sides to equal length first - `hmac.compare_digest`
        # short-circuits on length mismatch, leaking the secret's length.
        import hmac as _hmac
        import hashlib as _hl
        provided = _hl.sha256(token.encode("utf-8")).hexdigest()
        expected = _hl.sha256(cfg.admin_token.encode("utf-8")).hexdigest()
        if not _hmac.compare_digest(provided, expected):
            return ("Unauthorized", 401)
        from app.admin import stats
        conn = connect(cfg.db_path)
        # Admin is an internal, English-only stats page — hide the (no-op)
        # DE/EN switcher that base.html otherwise renders.
        return render_template("admin.html", stats=stats(conn),
                               show_lang_switcher=False)

    @app.route("/datenschutz")
    def datenschutz_route():
        return render_template("datenschutz.html", lang=request.args.get("lang", "de"))

    @app.route("/impressum")
    def impressum_route():
        return render_template("impressum.html", lang=request.args.get("lang", "de"))

    return app

# NOTE: do NOT instantiate `app = create_app()` at module level. Doing so
# calls load_config() at import time, which raises KeyError if any env var
# is missing - including during test collection, where fixtures haven't
# yet had a chance to monkeypatch.setenv(). Gunicorn supports the
# application-factory pattern directly: `gunicorn app.web:create_app()`.
