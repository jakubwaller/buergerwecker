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


# Localized copy for the standalone result/status pages (unsubscribe, manage
# update, renew, expired links, errors). Each entry: kind (notice style) plus
# a (badge, heading, message) triple per language. Routes render these through
# templates/result.html so they share the styled card layout instead of
# returning a bare string the browser shows top-left.
_RESULT_MESSAGES: dict[str, dict] = {
    "unsubscribed": {
        "kind": "success",
        "de": ("Abgemeldet", "Schade, dass du gehst",
               "Du bist erfolgreich abgemeldet und erhältst keine weiteren "
               "Termin-Benachrichtigungen mehr. Falls du es dir anders "
               "überlegst, kannst du dich jederzeit wieder anmelden."),
        "en": ("Unsubscribed", "Sorry to see you go",
               "You've been unsubscribed and won't receive any more "
               "appointment notifications. If you change your mind, you can "
               "sign up again any time."),
    },
    "updated": {
        "kind": "success",
        "de": ("Gespeichert", "Einstellungen aktualisiert",
               "Deine Filter wurden gespeichert. Wir benachrichtigen dich ab "
               "sofort nach den neuen Kriterien."),
        "en": ("Saved", "Settings updated",
               "Your filters have been saved. We'll notify you based on your "
               "new criteria from now on."),
    },
    "renewed": {
        "kind": "success",
        "de": ("Verlängert", "Abo verlängert",
               "Dein Abonnement wurde verlängert. Du erhältst weiterhin "
               "Benachrichtigungen über freie Termine."),
        "en": ("Renewed", "Subscription renewed",
               "Your subscription has been renewed. You'll keep receiving "
               "notifications about available appointments."),
    },
    "link_expired": {
        "kind": "error",
        "de": ("Link abgelaufen", "Dieser Termin-Link ist abgelaufen",
               "Freie Termine sind oft innerhalb von Sekunden vergeben. Schau "
               "am besten direkt auf der offiziellen Seite der Stadt nach, ob "
               "noch etwas frei ist."),
        "en": ("Link expired", "This appointment link has expired",
               "Free appointments are often taken within seconds. Please "
               "check directly on the city's official booking site to see "
               "what's still available."),
    },
    "invalid_token": {
        "kind": "error",
        "de": ("Ungültiger Link", "Dieser Link ist ungültig",
               "Der Link ist fehlerhaft oder nicht mehr gültig. Bitte "
               "verwende den aktuellen Link aus deiner E-Mail."),
        "en": ("Invalid link", "This link is invalid",
               "The link is malformed or no longer valid. Please use the most "
               "recent link from your email."),
    },
    "not_found": {
        "kind": "error",
        "de": ("Nicht gefunden", "Abonnement nicht gefunden",
               "Dieses Abonnement existiert nicht mehr. Möglicherweise hast du "
               "dich bereits abgemeldet."),
        "en": ("Not found", "Subscription not found",
               "This subscription no longer exists. You may have already "
               "unsubscribed."),
    },
    "invalid_email": {
        "kind": "error",
        "de": ("E-Mail ungültig", "Bitte überprüfe deine E-Mail-Adresse",
               "Die eingegebene E-Mail-Adresse scheint nicht gültig zu sein. "
               "Bitte gehe zurück und versuche es erneut."),
        "en": ("Invalid email", "Please check your email address",
               "The email address you entered doesn't look valid. Please go "
               "back and try again."),
    },
    "rate_limited": {
        "kind": "error",
        "de": ("Zu viele Anfragen", "Bitte versuche es später erneut",
               "Es wurden in kurzer Zeit zu viele Anmeldungen vorgenommen. "
               "Bitte warte einen Moment und versuche es dann noch einmal."),
        "en": ("Too many requests", "Please try again later",
               "Too many sign-ups were made in a short time. Please wait a "
               "moment and try again."),
    },
    "waitlist_full": {
        "kind": "error",
        "de": ("Warteliste voll", "Die Warteliste ist gerade voll",
               "Aktuell können keine neuen Anmeldungen aufgenommen werden. "
               "Bitte versuche es in ein paar Tagen noch einmal."),
        "en": ("Wait-list full", "The wait-list is currently full",
               "We can't take new sign-ups right now. Please try again in a "
               "few days."),
    },
    "missing_type": {
        "kind": "error",
        "de": ("Anliegen fehlt", "Bitte wähle ein Anliegen",
               "Es wurde kein Anliegen ausgewählt. Bitte gehe zurück und wähle "
               "die gewünschte Terminart."),
        "en": ("Appointment type missing", "Please choose an appointment type",
               "No appointment type was selected. Please go back and choose "
               "the type you need."),
    },
}


def _parse_hhmm(s: str) -> time_cls:
    h, m = s.split(":")
    return time_cls(int(h), int(m))


def _send_confirmation_email(conn, sub_id: int, email: str, lang: str, cfg) -> bool:
    """Try to send the confirmation now. Returns True if delivered, False if
    deferred (quota exhausted) — the sign-up stays pending and the poller's
    retry pass sends it later (e.g. next day once quota resets)."""
    from app.confirmations import send_confirmation_now
    return send_confirmation_now(conn, sub_id, email, lang, cfg)


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

    def _result_page(key: str, lang: str, *, status: int = 200,
                     action_url: str | None = None,
                     action_label: str | None = None):
        """Render a styled standalone result page (templates/result.html)."""
        if lang not in ("de", "en"):
            lang = "de"
        spec = _RESULT_MESSAGES[key]
        badge, heading, message = spec[lang]
        return render_template(
            "result.html",
            lang=lang,
            kind=spec["kind"],
            badge=badge,
            heading=heading,
            message=message,
            action_url=action_url,
            action_label=action_label,
            kofi_url=app.config["TERMINE_CONFIG"].kofi_url,
        ), status

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
        # Read the form language up front so every error below can render a
        # localized result page (the success paths redirect, so they don't
        # need it).
        lang = request.form.get("lang", "de")
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        email = request.form.get("email", "").strip().lower()
        if not email or "@" not in email:
            return _result_page("invalid_email", lang, status=400)
        # 2. per-IP rate limit (in-memory, soft)
        if not GLOBAL_IP_LIMITER.hit(f"ip:{ip}",
                                     cfg.subscribe_ratelimit_per_ip_per_hour,
                                     3600):
            return _result_page("rate_limited", lang, status=429)
        # 3. per-email rate limit (DB-backed, hard - shared across workers)
        conn_for_check = connect(cfg.db_path)
        if not email_rate_limit_ok(conn_for_check, email,
                                   cfg.subscribe_ratelimit_per_email_per_day):
            return _result_page("rate_limited", lang, status=429)
        # 4. parse filter from form
        city = request.form.get("city", "leipzig")
        atype = request.form.get("appointment_type", "").strip()
        if not atype:
            return _result_page("missing_type", lang, status=400)
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
                return _result_page("waitlist_full", lang, status=503)
            sub_id = insert_pending(conn, email=email, city=city,
                                    language=lang, filter_=f,
                                    ttl_days=cfg.subscription_ttl_days)
        # Try to send the confirmation now. If the daily email quota is
        # exhausted (or the send errors), we KEEP the pending sign-up and the
        # poller's retry pass sends the confirmation on a later cycle — so the
        # registration is never lost. Only the message differs: "check your
        # inbox" vs "it may arrive tomorrow". No soft-delete, no lockout.
        try:
            delivered = _send_confirmation_email(conn, sub_id, email, lang, cfg)
        except Exception:
            log.exception("confirmation email errored for sub %s; will retry", sub_id)
            delivered = False
        return redirect("/?confirmed=pending" if delivered
                        else "/?confirmed=queued")

    @app.route("/confirm/<token>")
    def confirm_route(token):
        cfg = app.config["TERMINE_CONFIG"]
        try:
            sub_id = verify(token, "confirm",
                            primary=cfg.token_secret_primary,
                            previous=cfg.token_secret_previous)
        except InvalidToken:
            return _result_page("invalid_token",
                                request.args.get("lang", "de"), status=400)
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
            return _result_page("invalid_token",
                                request.args.get("lang", "de"), status=400)
        conn = connect(cfg.db_path)
        row = conn.execute("SELECT language FROM subscriptions WHERE id=?",
                           (sub_id,)).fetchone()
        lang = request.args.get("lang") or (row["language"] if row else "de")
        soft_delete(conn, sub_id)
        return _result_page("unsubscribed", lang)

    @app.route("/manage/<token>", methods=["GET", "POST"])
    def manage_route(token):
        cfg = app.config["TERMINE_CONFIG"]
        try:
            sub_id = verify(token, "manage",
                            primary=cfg.token_secret_primary,
                            previous=cfg.token_secret_previous)
        except InvalidToken:
            return _result_page("invalid_token",
                                request.args.get("lang", "de"), status=400)
        conn = connect(cfg.db_path)
        if request.method == "POST":
            atype = request.form.get("appointment_type", "").strip()
            if not atype:
                row = conn.execute("SELECT language FROM subscriptions WHERE id=?",
                                   (sub_id,)).fetchone()
                return _result_page("missing_type",
                                    row["language"] if row else "de", status=400)
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
            row = conn.execute("SELECT language FROM subscriptions WHERE id=?",
                               (sub_id,)).fetchone()
            lang = row["language"] if row else "de"
            back_label = ("Zurück zu den Einstellungen" if lang == "de"
                          else "Back to your settings")
            return _result_page("updated", lang,
                                 action_url=f"/manage/{token}",
                                 action_label=back_label)
        row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
        if not row or row["deleted_at"] is not None:
            return _result_page("not_found", request.args.get("lang", "de"),
                                status=404)
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
            return _result_page("invalid_token",
                                request.args.get("lang", "de"), status=400)
        conn = connect(cfg.db_path)
        conn.execute(
            "UPDATE subscriptions SET expires_at=datetime('now', ?) "
            "WHERE id=? AND deleted_at IS NULL",
            (f"+{cfg.subscription_ttl_days} days", sid),
        )
        row = conn.execute("SELECT language FROM subscriptions WHERE id=?",
                           (sid,)).fetchone()
        lang = request.args.get("lang") or (row["language"] if row else "de")
        return _result_page("renewed", lang)

    @app.route("/go/<slot_token>")
    def go_route(slot_token):
        cfg = app.config["TERMINE_CONFIG"]
        conn = connect(cfg.db_path)
        row = conn.execute(
            "SELECT upstream_url FROM slots_cache WHERE slot_token=?",
            (slot_token,),
        ).fetchone()
        if not row:
            return _result_page("link_expired",
                                request.args.get("lang", "de"), status=410)
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
