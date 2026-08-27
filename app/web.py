from __future__ import annotations
import hashlib
import ipaddress
import logging
import os
import re
import time as time_mod
from collections import Counter
from datetime import time as time_cls
from urllib.parse import urlencode
from html import escape
from pathlib import Path
from flask import (Flask, Response, request, render_template, redirect,
                   send_from_directory)
from app.config import load_config, ttl_days_for
from app.db import connect, transaction
from app.catalog import (load_catalog, available_cities, booking_start_url,
                         city_display_name, CatalogError)
from app.models import Filter
from app.repo import (insert_pending, active_subscriptions, confirm,
                      soft_delete, suppression_reason, clear_delivery_block)
from app.ratelimit import GLOBAL_IP_LIMITER, email_rate_limit_ok
from app.tokens import sign, verify, InvalidToken
from app.planning import would_exceed_cap
from app.mail import send as mail_send, _idem_key
from app.webhooks import (PARSERS, apply_events, check_secret,
                          verify_sweego_signature)

log = logging.getLogger(__name__)

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/).
# CF-Connecting-IP is only trustworthy when the request actually arrived
# through Cloudflare; from anyone else it's a client-chosen header.
_CLOUDFLARE_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20",
    "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]


def _is_cloudflare(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _CLOUDFLARE_NETS)


def _client_ip() -> str:
    """Real client IP for rate limiting.

    Caddy (no trusted_proxies configured) discards any client-supplied
    X-Forwarded-For and sets it to the actual peer address, so XFF here is
    the trustworthy immediate peer. Behind Cloudflare that peer is a CF edge
    IP shared by many visitors, so prefer CF-Connecting-IP — but only when
    the peer really is Cloudflare, otherwise the header is spoofable.
    """
    peer = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    peer = peer.split(",")[0].strip()
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip and _is_cloudflare(peer):
        return cf_ip
    return peer


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
        "de": ("Weiter geht's", "Wir suchen weiter",
               "Alles klar — du bekommst weiterhin Benachrichtigungen über "
               "freie Termine. Kurz vor Ablauf fragen wir wieder nach, ob du "
               "noch suchst."),
        "en": ("Still looking", "We'll keep looking",
               "Got it — you'll keep receiving notifications about available "
               "appointments. Shortly before they would stop, we'll ask again "
               "whether you're still looking."),
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
    # Shown when someone signs up with an address we stopped mailing after a
    # spam complaint. Without it the sign-up succeeds, the confirmation is
    # dropped by the suppression list, and the page still says "check your
    # inbox" — a silent hole with no way out. Bounce suppressions never reach
    # here: those lift on sign-up (repo.clear_delivery_block).
    "address_blocked": {
        "kind": "error",
        "de": ("Versand gestoppt", "An diese Adresse senden wir keine E-Mails mehr",
               "Eine frühere E-Mail an diese Adresse wurde als Spam gemeldet, "
               "daher haben wir den Versand dorthin eingestellt. Wenn du den "
               "Dienst wieder nutzen möchtest, schreib uns kurz — wir schalten "
               "die Adresse dann wieder frei."),
        "en": ("Sending stopped", "We no longer send email to this address",
               "An earlier email to this address was reported as spam, so we "
               "stopped sending to it. If you'd like to use the service again, "
               "drop us a line and we'll turn it back on."),
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
    "contact_sent": {
        "kind": "success",
        "de": ("Gesendet", "Nachricht ist raus",
               "Danke für deine Nachricht — sie wurde zugestellt. Falls eine "
               "Antwort nötig ist, melde ich mich an der angegebenen "
               "E-Mail-Adresse."),
        "en": ("Sent", "Your message is on its way",
               "Thanks for getting in touch — your message has been "
               "delivered. If a reply is needed, I'll write to the email "
               "address you provided."),
    },
    "contact_missing": {
        "kind": "error",
        "de": ("Nachricht fehlt", "Bitte schreib noch etwas dazu",
               "Es wurde keine Nachricht eingegeben. Bitte gehe zurück und "
               "beschreibe kurz dein Anliegen."),
        "en": ("Message missing", "Please add a message",
               "No message was entered. Please go back and describe your "
               "request briefly."),
    },
    "contact_failed": {
        "kind": "error",
        "de": ("Nicht gesendet", "Das hat leider nicht geklappt",
               "Die Nachricht konnte gerade nicht zugestellt werden. Bitte "
               "versuche es in ein paar Minuten erneut oder schreibe direkt "
               "eine E-Mail."),
        "en": ("Not sent", "That didn't go through",
               "Your message couldn't be delivered just now. Please try again "
               "in a few minutes, or send an email directly."),
    },
    "consent_required": {
        "kind": "error",
        "de": ("Einwilligung fehlt", "Für dieses Anliegen fehlt noch deine Einwilligung",
               "Das gewählte Anliegen gehört zu einer besonders geschützten "
               "Kategorie (Art. 9 DSGVO). Dafür brauchen wir deine "
               "ausdrückliche Einwilligung — bitte gehe zurück und setze das "
               "zusätzliche Häkchen."),
        "en": ("Consent missing", "This appointment type needs your explicit consent",
               "The appointment type you chose falls into a special category "
               "under Art. 9 GDPR. We need your explicit consent for it — "
               "please go back and tick the additional box."),
    },
    "unknown_city": {
        "kind": "error",
        "de": ("Stadt unbekannt", "Diese Stadt gibt es hier nicht",
               "Die Anmeldung nennt eine Stadt, die wir nicht anbieten. Bitte "
               "wähle sie auf der Startseite aus."),
        "en": ("Unknown city", "We don't cover that city",
               "The sign-up named a city we don't offer. Please pick one from "
               "the home page."),
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
    "unknown_type": {
        "kind": "error",
        "de": ("Anliegen unbekannt", "Dieses Anliegen gibt es hier nicht",
               "Das gewählte Anliegen wird für diese Stadt nicht angeboten. "
               "Bitte gehe zurück und wähle eines aus der Liste."),
        "en": ("Unknown appointment type", "We don't offer that appointment type",
               "The appointment type you chose isn't offered for this city. "
               "Please go back and pick one from the list."),
    },
    "unknown_location": {
        "kind": "error",
        "de": ("Standort unbekannt", "Diesen Standort gibt es hier nicht",
               "Mindestens einer der gewählten Standorte wird für diese Stadt "
               "nicht angeboten. Bitte gehe zurück und wähle aus der Liste."),
        "en": ("Unknown location", "We don't offer that location",
               "At least one of the locations you chose isn't offered for "
               "this city. Please go back and pick from the list."),
    },
    "invalid_time": {
        "kind": "error",
        "de": ("Zeitfenster ungültig", "Bitte überprüfe das Zeitfenster",
               "Die Uhrzeiten müssen im Format HH:MM angegeben sein, und der "
               "Beginn darf nicht nach dem Ende liegen. Bitte gehe zurück und "
               "korrigiere die Angaben."),
        "en": ("Invalid time window", "Please check the time window",
               "Times must be given as HH:MM, and the start can't be after "
               "the end. Please go back and correct them."),
    },
}


# Projects whose Impressum links here for the § 5 DDG second contact channel.
# The slug arrives as ?projekt=… so the message says which site it came from;
# an unknown or missing slug is fine and just renders the picker unselected.
_CONTACT_PROJECTS: dict[str, str] = {
    "buergerwecker": "Bürgerwecker",
    "papamap": "PapaMap",
    "zapfkompass": "Zapfkompass",
}

_CONTACT_NAME_MAX = 200
_CONTACT_MESSAGE_MAX = 5000

# Deliberately conservative: on the contact form the submitted address becomes
# a Reply-To header, so reject anything with whitespace, control characters,
# angle brackets or a bare domain rather than trusting the provider to sanitize
# it. Turning away a handful of exotic-but-valid addresses is the right trade
# here — they can still use the plain mailto: link on the same page.
#
# Sign-up uses the same rule. It used to accept anything containing an "@",
# which let `subscriber@example-com` through on 2026-07-24 — a missing dot, so an
# undeliverable domain. Mailjet rejects the whole batch such an address lands
# in, and that sign-up never got its confirmation mail.
_EMAIL_RE = re.compile(r"[^@\s<>,;\"]+@[^@\s<>,;\"]+\.[A-Za-z]{2,}")


# The tenant a bare buergerwecker.de lands on.
_DEFAULT_CITY = "leipzig"

# The pages a search engine or a link preview may point at. Everything else —
# /manage/<token>, /go/<slot_token>, /admin — carries a token or private data
# in its URL, so those get noindex and a canonical pointing at the site root
# instead of at themselves. Keyed on endpoint rather than path because the
# tenant pages are now /<city> and would be indistinguishable from a token.
_PUBLIC_ENDPOINTS = frozenset(("index", "city_page", "impressum_route",
                               "datenschutz_route", "kontakt_route"))

_OG_DEFAULT_TITLE = {
    "de": "Bürgerwecker – nie wieder freie Bürgerbüro-Termine verpassen",
    "en": "Bürgerwecker – never miss a free Amt appointment",
}
# City first: it is the word that distinguishes this page from the other 28,
# and titles are truncated from the right in a result list.
_OG_CITY_TITLE = {
    "de": "{city}: freie Termine beim Amt – Bürgerwecker",
    "en": "{city}: free appointment slots – Bürgerwecker",
}
_OG_DEFAULT_DESC = {
    "de": ("Wir gucken für dich nach freien Terminen und schicken dir eine "
           "E-Mail, sobald einer frei wird. Kostenlos, ohne Konto. Buchen "
           "musst du selbst."),
    "en": ("We watch the city's booking site for you and send an email as "
           "soon as a slot opens up. Free, no account. You book it yourself."),
}
_OG_CITY_DESC = {
    "de": ("Wir gucken für dich nach freien Terminen in {city} und schicken "
           "dir eine E-Mail, sobald einer frei wird. Kostenlos, ohne Konto. "
           "Buchen musst du selbst."),
    "en": ("We watch {city}'s booking site for you and send an email as soon "
           "as a slot opens up. Free, no account. You book it yourself."),
}


def _parse_hhmm(s: str) -> time_cls:
    """'HH:MM' (a trailing ':SS' is tolerated) → time. Raises ValueError on
    anything else, including out-of-range hours; the caller turns that into a
    400 rather than letting it surface as a 500."""
    parts = (s or "").strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"not HH:MM: {s!r}")
    return time_cls(int(parts[0]), int(parts[1]))


class _FormError(Exception):
    """A rejected sign-up/manage form; `key` names the result page."""
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _filter_from_form(form, catalog) -> tuple[Filter, bool]:
    """The Filter a sign-up or manage form asks for, validated against the
    tenant's catalog. Returns (filter, is_sensitive); raises _FormError.

    Every id has to be one the catalog offers. The form only ever posts those,
    so a miss is a hand-built request — and it used to be stored anyway: an
    unknown appointment_type became a PollPlan the poller sent upstream every
    cycle, interpolated raw into the vendor's request, and each distinct junk
    value counted toward MAX_PLANS_PER_CITY until real visitors were told the
    wait-list was full.
    """
    atype = form.get("appointment_type", "").strip()
    if not atype:
        raise _FormError("missing_type")
    if atype not in catalog.appointment_types.values():
        raise _FormError("unknown_type")
    # Special-category services (Art. 9 GDPR) need the separate explicit
    # consent on top of the double opt-in. Enforced here rather than in the
    # form because the box is hidden by script while an ordinary service is
    # selected — and because a POST need never have rendered the page.
    sensitive = catalog.is_sensitive(atype)
    if sensitive and form.get("consent_special") != "1":
        raise _FormError("consent_required")
    all_locs = form.get("all_locations") == "1"
    loc_list = form.getlist("locations")
    if not all_locs and loc_list:
        known = set(catalog.locations.values())
        if any(loc not in known for loc in loc_list):
            raise _FormError("unknown_location")
    locations = "all" if all_locs or not loc_list else loc_list
    weekdays = [int(d) for d in form.getlist("weekdays")
                if d.isdigit() and 1 <= int(d) <= 7]
    if not weekdays:
        weekdays = [1, 2, 3, 4, 5, 6, 7]
    try:
        start = _parse_hhmm(form.get("time_start") or "00:00")
        end = _parse_hhmm(form.get("time_end") or "23:59")
    except ValueError:
        raise _FormError("invalid_time") from None
    if start > end:
        # An empty window can never match; nobody means to ask for one.
        raise _FormError("invalid_time")
    return Filter(
        appointment_types=[atype],
        locations=locations,
        weekdays=weekdays,
        time_window_start=start,
        time_window_end=end,
        max_days_ahead=_parse_max_days(form.get("max_days_ahead")),
    ), sensitive


def _parse_max_days(raw: str | None) -> int | None:
    """Form value for 'only slots within the next N days'; ''/invalid → no limit."""
    raw = (raw or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def _send_confirmation_email(conn, sub_id: int, email: str, lang: str,
                             city: str, cfg) -> bool:
    """Try to send the confirmation now. Returns True if delivered, False if
    deferred (quota exhausted) — the sign-up stays pending and the poller's
    retry pass sends it later (e.g. next day once quota resets)."""
    from app.confirmations import send_confirmation_now
    return send_confirmation_now(conn, sub_id, email, lang, city, cfg)


def _send_manage_link_email(conn, sub_id: int, cfg) -> None:
    """Sends a separate email with the /manage link - NEVER in digests."""
    row = conn.execute(
        "SELECT email, language, city FROM subscriptions WHERE id=?",
        (sub_id,),
    ).fetchone()
    if not row:
        return
    tok = sign(sub_id, "manage",
               primary=cfg.token_secret_primary,
               previous=cfg.token_secret_previous)
    url = f"{cfg.public_base_url}/manage/{tok}"
    city_name = city_display_name(row["city"], row["language"])
    suffix = f" ({city_name})" if city_name else ""
    if row["language"] == "de":
        body = (f"Dein Verwaltungs-Link: {url}\nMit diesem Link kannst du deine "
                f"Einstellungen jederzeit ändern oder dich abmelden.")
        subj = f"Verwaltungs-Link{suffix}"
    else:
        body = (f"Your management link: {url}\nUse it any time to change your "
                f"settings or unsubscribe.")
        subj = f"Management link{suffix}"
    key = _idem_key(sub_id, [], f"manage-link-{sub_id}")
    mail_send(conn, row["email"], subj, body, idem_key=key)


def _office_label(cat, cname: str, lang: str, fallback: str) -> str:
    """Short Amt name for a tenant: display.json `office`, else the tenant
    label with its redundant "<Stadt>: " prefix stripped."""
    office = cat.display_text("office", lang)
    if office:
        return office
    label = cat.display_text("label", lang) or fallback
    prefix = f"{cname}: "
    return label[len(prefix):] if label.startswith(prefix) else label


def _tenant_switcher(city: str | None, lang: str):
    """(other-city entries, sibling Ämter of the current city) for the form page.

    `city=None` means there is no current tenant — nothing is excluded and the
    first element is simply every city. That is what the picker at / renders.

    Two levels, because a city can now offer many Ämter: Münster alone has
    seven. The switcher lists one entry per *city* — a single-tenant city stays
    a plain link, a multi-tenant city carries its Ämter as sub-links — and the
    Ämter of the city being viewed get their own row above it, which is where
    someone who landed on Münster's Bürgeramt looks for the Standesamt.

    Entries are (city_name, url_or_None, [(office_label, url), …]); the url is
    None exactly when the city has several tenants and no single target.
    """
    def url_for_tenant(slug: str) -> str:
        return f"/{slug}" + ("?lang=en" if lang == "en" else "")

    tenants = []
    for other in available_cities():
        try:
            ocat = load_catalog(other)
        except CatalogError:
            # An incomplete tenant dir (e.g. a scaffold with only a
            # scraper_config.json) must not take down every tenant's page.
            continue
        cname = ocat.display_text("city_name", lang) or other
        tenants.append((cname, ocat, other))
    tenants_per_city = Counter(cname for cname, _, _ in tenants)
    current_city_name = next((cname for cname, _, slug in tenants if slug == city), None)

    by_city: dict[str, list[tuple[str, str]]] = {}
    sibling_offices: list[tuple[str, str | None]] = []
    for cname, ocat, slug in tenants:
        office = _office_label(ocat, cname, lang, slug)
        if cname == current_city_name and tenants_per_city[cname] > 1:
            # The current city's own Ämter get the dedicated row instead; the
            # tenant being viewed is listed too, unlinked, so the row reads as
            # a picker rather than a list of somewhere-else.
            sibling_offices.append((office, None if slug == city else url_for_tenant(slug)))
        if slug == city:
            continue
        by_city.setdefault(cname, []).append((office, url_for_tenant(slug)))

    other_cities = []
    for cname in sorted(by_city, key=str.casefold):
        if cname == current_city_name:
            continue  # already covered by the sibling row
        offices = sorted(by_city[cname], key=lambda pair: pair[0].casefold())
        if tenants_per_city[cname] > 1:
            other_cities.append((cname, None, offices))
        else:
            other_cities.append((cname, offices[0][1], []))
    sibling_offices.sort(key=lambda pair: pair[0].casefold())
    return other_cities, sibling_offices


def _is_tenant(city: str | None) -> bool:
    """True only for a slug we actually serve.

    The gate for every request-supplied city that ends up in a URL or a
    subscription row. load_catalog validates the slug's shape; this also
    requires the tenant to exist.
    """
    if not city:
        return False
    try:
        load_catalog(city)
    except CatalogError:
        return False
    return True


def _offered_sensitive(catalog) -> list[str]:
    """Uuids of this tenant's *offered* special-category services.

    Empty for every ordinary tenant, which is what keeps the extra consent box
    off their forms. Handed to the template as JSON so the box can be hidden
    again while an ordinary service is selected — enforcement is server-side,
    this is only so the page doesn't ask for consent it doesn't need.
    """
    return sorted(u for u in catalog.appointment_types.values()
                  if catalog.is_sensitive(u))


def _stamp_meta_now(conn, key: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, CURRENT_TIMESTAMP) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP",
        (key,),
    )


def _bump_meta_counter(conn, key: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, '1') "
        "ON CONFLICT (key) DO UPDATE SET "
        "value = CAST(CAST(meta.value AS INTEGER) + 1 AS TEXT), "
        "updated_at = CURRENT_TIMESTAMP",
        (key,),
    )


def _stamp_webhook_seen(conn, provider: str) -> None:
    """Remember when this provider last reached us, for any event at all.

    The failure mode a bounce webhook has is silence: a wrong URL, a rotated
    secret or a dashboard toggle turns the feedback loop off, everything keeps
    appearing to work, and the first symptom is a damaged sending reputation
    weeks later. /admin grades this timestamp so the silence is visible."""
    try:
        _stamp_meta_now(conn, f"last_webhook_at_{provider}")
    except Exception:
        log.exception("failed to stamp webhook receipt")

def _count_webhook_error(cfg, provider: str) -> None:
    """Record that a provider posted something we could not read.

    The endpoint answers 200 to a bad payload so the provider does not disable
    it, which means nothing else would ever surface a parser that has gone
    stale against a provider's format change. This counter is what /admin
    reads."""
    try:
        _bump_meta_counter(connect(cfg.db_path), f"webhook_errors_{provider}")
    except Exception:
        log.exception("failed to record webhook parse error")

def _count_webhook_rejection(cfg, provider: str) -> None:
    """Record a post that carried the right URL secret but a bad signature.

    Only Sweego signs, and a signature mismatch is the one failure that no
    other measure can tell from silence: the dashboard is posting, the URL is
    right, and the delivery is thrown away with a 403 that goes nowhere. A
    misread `SWEEGO_WEBHOOK_SECRET` — the docs never settled its encoding, see
    `webhooks._signing_keys` — would look exactly like a webhook nobody wired
    up, for as long as nobody compared the provider's own delivery log with
    /admin. The count and the stamp let /admin say "rejected" instead of
    "never reported", and the ops summary raise it while it is fresh.

    A wrong *URL* secret is deliberately not counted here: that path is
    reachable by anyone who guesses a provider name, and a scanner must not be
    able to paint a red line on the dashboard."""
    try:
        conn = connect(cfg.db_path)
        _bump_meta_counter(conn, f"webhook_rejected_{provider}")
        _stamp_meta_now(conn, f"last_webhook_rejected_at_{provider}")
    except Exception:
        log.exception("failed to record webhook rejection")

def create_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder=None)
    # Load config ONCE at startup. Missing env vars surface here, not on
    # the first real request.
    app.config["TERMINE_CONFIG"] = load_config()

    @app.after_request
    def _privacy_headers(resp):
        # The URL of a sign-up page names the Amt (?city=muenster-…), which for
        # a special-category tenant is itself the sensitive fact. Without this,
        # every outbound click — the Ko-fi link, the source-code link, the
        # /go/<city> redirect to the city's booking page — would hand that URL
        # to the destination in the Referer header.
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

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

        # Defaults for the link-preview tags in base.html. A route that knows
        # better — the form page knows its city — passes og_title/og_description
        # to render_template, and those win: Flask re-applies the explicit
        # context after the context processors.
        lang = request.args.get("lang")
        lang = lang if lang in ("de", "en") else "de"
        base = app.config["TERMINE_CONFIG"].public_base_url.rstrip("/")
        indexable = request.endpoint in _PUBLIC_ENDPOINTS
        path = request.path if indexable else "/"
        # Each language version canonicalises to itself and declares the other
        # as its alternate. Without the ?lang=en on the English canonical,
        # Google folds the two into the German one and drops the English page.
        suffix = "?lang=en" if lang == "en" else ""
        return {"switch_lang_url": switch_lang_url,
                "og_title": _OG_DEFAULT_TITLE[lang],
                "og_description": _OG_DEFAULT_DESC[lang],
                "og_url": f"{base}{path}{suffix}",
                "canonical_url": f"{base}{path}{suffix}",
                "alternate_de": f"{base}{path}",
                "alternate_en": f"{base}{path}?lang=en",
                "indexable": indexable,
                "og_image": f"{base}/og-image.png",
                # The FAQ promises the per-subscriber daily cap in words, so
                # it reads the configured number rather than hard-coding one.
                "digest_cap": app.config["TERMINE_CONFIG"]
                .max_digests_per_subscriber_per_day}

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

    @app.route("/og-image.png")
    def og_image_route():
        # The one static file the app serves (Flask's own static folder stays
        # off). Crawlers refetch it rarely, so cache it for a week.
        return send_from_directory(Path(__file__).resolve().parent / "static",
                                   "og-image.png", max_age=604800)

    @app.route("/sitemap.xml")
    def sitemap():
        """Every indexable URL, both languages, cross-declared with hreflang.

        Cloudflare serves this zone's robots.txt from its own managed content,
        so this file cannot be advertised there — submit it in Search Console.
        """
        base = app.config["TERMINE_CONFIG"].public_base_url.rstrip("/")
        # Skip a tenant whose catalog doesn't load — a scaffold directory with
        # only a scraper_config.json is a 404, and a sitemap full of those is
        # how a site teaches a crawler to distrust it.
        slugs = []
        for slug in sorted(available_cities()):
            try:
                load_catalog(slug)
            except CatalogError:
                continue
            slugs.append(slug)
        paths = ["/"] + [f"/{slug}" for slug in slugs] \
            + ["/impressum", "/datenschutz", "/kontakt"]
        urls = []
        for path in paths:
            de, en = f"{base}{path}", f"{base}{path}?lang=en"
            for loc, alt_self in ((de, "de"), (en, "en")):
                urls.append(
                    f"  <url>\n    <loc>{escape(loc)}</loc>\n"
                    f'    <xhtml:link rel="alternate" hreflang="de" href="{escape(de)}"/>\n'
                    f'    <xhtml:link rel="alternate" hreflang="en" href="{escape(en)}"/>\n'
                    f'    <xhtml:link rel="alternate" hreflang="x-default" href="{escape(de)}"/>\n'
                    f"    <priority>{'0.8' if alt_self == 'de' else '0.5'}</priority>\n"
                    f"  </url>")
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
                '        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
                + "\n".join(urls) + "\n</urlset>\n")
        return Response(body, mimetype="application/xml")

    @app.route("/healthz")
    def healthz():
        cfg = app.config["TERMINE_CONFIG"]
        conn = connect(cfg.db_path)
        conn.execute("SELECT 1").fetchone()
        return "ok", 200

    @app.route("/webhooks/<provider>/<secret>", methods=["POST"])
    def webhook_route(provider, secret):
        """Delivery feedback from a mail provider: bounces, spam complaints,
        unsubscribes. See `app.webhooks` for why this exists.

        Answers 200 to anything it accepted, including payloads it understood
        and had nothing to do with. Providers disable an endpoint that keeps
        failing, and a retry cannot fix a body we could not parse, so a bad
        payload is counted and logged rather than rejected — the count is on
        /admin so it cannot rot unnoticed.
        """
        cfg = app.config["TERMINE_CONFIG"]
        parser = PARSERS.get(provider)
        if parser is None:
            return "unknown provider", 404
        if not cfg.webhook_secret:
            # Not configured. Say so rather than 404: a silent 404 is
            # indistinguishable from a typo'd URL in a provider dashboard.
            return "webhooks not configured", 503
        if not check_secret(secret, cfg.webhook_secret):
            return "forbidden", 403
        body = request.get_data()
        if provider == "sweego" and cfg.sweego_webhook_secret:
            if not verify_sweego_signature(
                    webhook_id=request.headers.get("webhook-id", ""),
                    timestamp=request.headers.get("webhook-timestamp", ""),
                    signature=request.headers.get("webhook-signature", ""),
                    body=body, secret=cfg.sweego_webhook_secret):
                _count_webhook_rejection(cfg, provider)
                log.warning("webhook %s: bad signature on %d bytes — "
                            "SWEEGO_WEBHOOK_SECRET does not match the "
                            "dashboard", provider, len(body))
                return "bad signature", 403
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            _count_webhook_error(cfg, provider)
            log.warning("webhook %s: unparseable payload (%d bytes)",
                        provider, len(body))
            return "", 200
        events = parser(payload)
        # A body that is valid JSON in a shape the parser does not recognise is
        # the stale-parser case this counter exists for, and it is the quiet
        # one: the payload check above only catches JSON that does not parse,
        # while a provider changing its envelope still posts perfectly good
        # JSON. Nothing else would surface it — the receipt is stamped either
        # way, so /admin would show a healthy, talkative webhook that has
        # learned nothing for weeks. An empty body is not this: `[]` is a
        # provider with nothing to report, not a format we misread.
        if payload and not events:
            _count_webhook_error(cfg, provider)
            log.warning("webhook %s: no events parsed from %d bytes — "
                        "the payload format may have changed", provider,
                        len(body))
        conn = connect(cfg.db_path)
        _stamp_webhook_seen(conn, provider)
        result = apply_events(
            conn, events,
            soft_bounce_threshold=cfg.soft_bounce_suppress_threshold)
        if result.suppressed or result.unsubscribed:
            log.info("webhook %s: suppressed=%d unsubscribed=%d",
                     provider, result.suppressed, result.unsubscribed)
        return "", 200

    @app.route("/")
    def index():
        lang = request.args.get("lang", "de")
        if lang not in ("de", "en"):
            lang = "de"
        # `confirmed=pending` / `subscribe_error=mail` are set by the /subscribe
        # redirect so the form can show a "check your inbox" banner or a
        # retryable error instead of silently re-rendering.
        confirmed = request.args.get("confirmed")
        error = request.args.get("subscribe_error")
        # `/?city=x` was the tenant URL until 2026-08-04 and is all over Reddit,
        # the press article and already-sent emails. Send it to /x for good so
        # the search engines fold the two together instead of ranking neither.
        city = request.args.get("city")
        if city is not None:
            # Only ever redirect to a tenant we actually have. `city` is user
            # input and lands in a Location header: "/" + "/evil.example.com"
            # is "//evil.example.com", a protocol-relative URL that browsers
            # follow off-site. Anything unknown falls through to city_page,
            # which 404s with the picker.
            if not _is_tenant(city):
                return city_page(city)
            args = request.args.to_dict(flat=True)
            args.pop("city")
            query = f"?{urlencode(args)}" if args else ""
            return redirect(f"/{city}{query}", code=301)
        # No city chosen: show the picker, not one city's form. Leipzig was the
        # default for the first 28 launches, which meant every visitor from the
        # other 28 cities — and every link preview of the bare domain — was
        # told about Leipzig.
        all_cities, _ = _tenant_switcher(None, lang)
        return render_template("cities.html", lang=lang, cities=all_cities,
                               confirmed=confirmed, error=error,
                               kofi_url=app.config["TERMINE_CONFIG"].kofi_url)

    @app.route("/<city>")
    def city_page(city):
        """A tenant's sign-up form. Werkzeug ranks static rules above this one,
        so /impressum and friends keep their own handlers."""
        lang = request.args.get("lang", "de")
        if lang not in ("de", "en"):
            lang = "de"
        confirmed = request.args.get("confirmed")
        error = request.args.get("subscribe_error")
        try:
            catalog = load_catalog(city)
        except CatalogError:
            # Not a tenant. 404 rather than redirect to /, so a mistyped or
            # retired city is not a soft 404 in the index — but render the
            # picker with it, because the useful next step is choosing a city.
            all_cities, _ = _tenant_switcher(None, lang)
            return render_template("cities.html", lang=lang, cities=all_cities,
                                   unknown_city=city,
                                   kofi_url=app.config["TERMINE_CONFIG"].kofi_url), 404
        other_cities, sibling_offices = _tenant_switcher(city, lang)
        # A shared city link should preview as that city. The Amt deliberately
        # stays out of it: for a special-category tenant the office name is the
        # sensitive fact, and it would end up on the card of whoever posts it.
        city_name = catalog.display_text("city_name", lang)
        og = {}
        if city_name:
            og = {"og_title": _OG_CITY_TITLE[lang].format(city=city_name),
                  "og_description": _OG_CITY_DESC[lang].format(city=city_name)}
        return render_template("form.html",
                               lang=lang,
                               city=city,
                               **og,
                               confirmed=confirmed,
                               error=error,
                               heading=catalog.display_text("heading", lang),
                               city_name=city_name,
                               note=catalog.display_text("note", lang),
                               other_cities=other_cities,
                               sibling_offices=sibling_offices,
                               appointment_types=catalog.appointment_types_for(lang),
                               locations=catalog.locations_for(lang),
                               service_locations=catalog.service_locations,
                               sensitive_services=_offered_sensitive(catalog),
                               sensitive_ttl_days=app.config["TERMINE_CONFIG"]
                               .sensitive_subscription_ttl_days,
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
        ip = _client_ip()
        email = request.form.get("email", "").strip().lower()
        if not _EMAIL_RE.fullmatch(email):
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
        # 3b. Addresses we stopped mailing after a spam complaint. That is the
        # person's own verdict and a form submission is not their word for it,
        # so lifting it needs a human — but say so, instead of accepting the
        # sign-up and dropping the confirmation into the suppression list while
        # the page claims it was sent. Placed after both rate limits: the
        # answer differs per address, and probing for it should cost the same
        # as probing for anything else. Other blocks (a bounce, a run of
        # provider rejections) are lifted further down, once the sign-up is
        # real — see the insert.
        if suppression_reason(conn_for_check, email) == "complaint":
            return _result_page("address_blocked", lang, status=403,
                                action_url=f"/kontakt?lang={lang}",
                                action_label=("Kontakt" if lang == "de"
                                              else "Contact us"))
        # 4. parse filter from form
        city = request.form.get("city", _DEFAULT_CITY)
        # An unknown city used to be stored anyway — a subscription no poller
        # would ever match — and since the redirect below builds "/{city}", it
        # also let a posted form choose the Location header.
        try:
            catalog = load_catalog(city) if city else None
        except CatalogError:
            catalog = None
        if catalog is None:
            return _result_page("unknown_city", lang, status=400)
        try:
            f, sensitive = _filter_from_form(request.form, catalog)
        except _FormError as err:
            return _result_page(err.key, lang, status=400)
        # 5. plan-cap overflow check + insert atomically (spec 3.2.6).
        conn = connect(cfg.db_path)
        with transaction(conn):
            existing = [(s.city, s.sub_filter) for s in active_subscriptions(conn)]
            if would_exceed_cap(existing, city, f,
                                max_plans_per_city=cfg.max_plans_per_city):
                return _result_page("waitlist_full", lang, status=503)
            sub_id = insert_pending(conn, email=email, city=city,
                                    language=lang, filter_=f,
                                    ttl_days=ttl_days_for(cfg, sensitive),
                                    consent_special=sensitive)
            # A bounce, or a run of provider rejections, only ever claimed the
            # mailbox was broken *then*; a completed sign-up is the evidence it
            # works now, so the block lifts and the confirmation below is
            # allowed out. If the mailbox really is still broken, one bounce
            # re-suppresses it.
            #
            # Deliberately here and not next to the complaint check above:
            # this is a write, and up there it ran on any POST that carried an
            # e-mail — no valid city, no appointment type, and no rate limit
            # that bites, because the per-address one counts subscription rows
            # and a rejected form creates none. That let anyone reset the
            # delivery block, and the `email_failures` counter with it, for an
            # address they do not own, as often as they liked. Tied to the
            # insert it can only ever be a side effect of a real sign-up.
            clear_delivery_block(conn, email)
        # Try to send the confirmation now. If the daily email quota is
        # exhausted (or the send errors), we KEEP the pending sign-up and the
        # poller's retry pass sends the confirmation on a later cycle — so the
        # registration is never lost. Only the message differs: "check your
        # inbox" vs "it may arrive tomorrow". No soft-delete, no lockout.
        try:
            delivered = _send_confirmation_email(conn, sub_id, email, lang,
                                                 city, cfg)
        except Exception:
            log.exception("confirmation email errored for sub %s; will retry", sub_id)
            delivered = False
        # Carry the city and language back, or the banner would land on the
        # picker in German no matter who just signed up for what.
        args = {"confirmed": "pending" if delivered else "queued"}
        if lang == "en":
            args["lang"] = lang
        return redirect(f"/{city}?{urlencode(args)}")

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

    # POST is the RFC 8058 one-click unsubscribe mail clients send to the
    # List-Unsubscribe URL; GET is the human clicking the link in the body.
    @app.route("/unsubscribe/<token>", methods=["GET", "POST"])
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
            owner = conn.execute(
                "SELECT city, language FROM subscriptions "
                "WHERE id=? AND deleted_at IS NULL",
                (sub_id,)).fetchone()
            if not owner:
                return _result_page("not_found",
                                    request.args.get("lang", "de"), status=404)
            lang = owner["language"]
            try:
                catalog = load_catalog(owner["city"])
            except CatalogError:
                return _result_page("unknown_city", lang, status=400)
            # Editing a filter is a second way to pick a service, so it gets
            # the same validation and Art. 9 consent gate as sign-up.
            try:
                f, sensitive = _filter_from_form(request.form, catalog)
            except _FormError as err:
                return _result_page(err.key, lang, status=400)
            with transaction(conn):
                # The same cap check as /subscribe, minus this subscription's
                # own current plan, which the new filter replaces. Without it
                # the manage form was a way past the wait-list: a sign-up for
                # a plan already being polled, then an edit to any other.
                existing = [(s.city, s.sub_filter)
                            for s in active_subscriptions(conn) if s.id != sub_id]
                if would_exceed_cap(existing, owner["city"], f,
                                    max_plans_per_city=cfg.max_plans_per_city):
                    return _result_page("waitlist_full", lang, status=503)
                # Clear the cadence state along with the filter: both signals
                # were measured against the OLD filter, and keeping them would
                # leave someone who just narrowed a firehose down to one scarce
                # office stuck on the slow cadence their previous filter earned.
                conn.execute("UPDATE subscriptions SET filters_json=?, "
                             "last_match_count=NULL, consecutive_digests=0 "
                             "WHERE id=?",
                             (f.to_json(), sub_id))
                from app.repo import set_special_consent
                set_special_consent(conn, sub_id, sensitive)
                if sensitive:
                    # Switching into a special-category service also pulls the
                    # expiry in to the shorter retention — never pushes it out,
                    # so this can't be used to extend an ordinary subscription.
                    short = ttl_days_for(cfg, True)
                    conn.execute(
                        "UPDATE subscriptions SET expires_at=datetime('now', ?) "
                        "WHERE id=? AND expires_at > datetime('now', ?)",
                        (f"+{short} days", sub_id, f"+{short} days"),
                    )
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
                               sensitive_services=_offered_sensitive(catalog),
                               sensitive_ttl_days=cfg.sensitive_subscription_ttl_days,
                               consent_special=("consent_special_at" in row.keys()
                                                and row["consent_special_at"] is not None),
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
        row = conn.execute(
            "SELECT language, consent_special_at, deleted_at "
            "FROM subscriptions WHERE id=?",
            (sid,)).fetchone()
        lang = request.args.get("lang") or (row["language"] if row else "de")
        # An expired subscription is only paused: it stays renewable for
        # EXPIRED_GRACE_DAYS. Once housekeeping has deleted it the link is
        # still cryptographically valid but there is nothing to revive, and
        # a "renewed" page over a 0-row UPDATE would be a lie.
        if row is None or row["deleted_at"] is not None:
            return _result_page("not_found", lang, status=404)
        # A special-category subscription renews for its own shorter term —
        # otherwise the renewal link would quietly promote it to the
        # ordinary one.
        ttl = ttl_days_for(cfg, row["consent_special_at"] is not None)
        # reminder_sent_at is the once-per-term latch of the still-looking
        # check-in. Without clearing it here a renewed subscription would
        # never be asked again and would expire silently at the end of the
        # next term.
        conn.execute(
            "UPDATE subscriptions SET expires_at=datetime('now', ?), "
            "reminder_sent_at=NULL WHERE id=? AND deleted_at IS NULL",
            (f"+{ttl} days", sid),
        )
        return _result_page("renewed", lang)

    @app.route("/go/sub/<token>")
    def go_sub_route(token):
        """Booking link for a special-category subscription.

        `/go/<city>` names the Amt in the URL, which for a single-purpose Amt
        gives away exactly what the redacted digest withholds. This variant
        carries a signed subscription id only and resolves the tenant here.
        """
        cfg = app.config["TERMINE_CONFIG"]
        lang = "en" if request.args.get("lang") == "en" else "de"
        try:
            sid = verify(token, "goto",
                         primary=cfg.token_secret_primary,
                         previous=cfg.token_secret_previous)
        except InvalidToken:
            return _result_page("invalid_token", lang, status=400)
        conn = connect(cfg.db_path)
        row = conn.execute(
            "SELECT city FROM subscriptions WHERE id=? AND deleted_at IS NULL",
            (sid,)).fetchone()
        if not row:
            return _result_page("not_found", lang, status=404)
        try:
            scfg = load_catalog(row["city"]).scraper_config
        except CatalogError:
            return _result_page("link_expired", lang, status=410)
        return redirect(booking_start_url(scfg, lang), code=302)

    @app.route("/go/<slot_token>")
    def go_route(slot_token):
        cfg = app.config["TERMINE_CONFIG"]
        # City-level link (current emails): /go/<city> — no colon. Resolved
        # from the catalog at click time so it never expires and survives an
        # upstream base-URL change. Tokens with a colon are per-slot links
        # from old emails, served from slots_cache until housekeeping prunes
        # them (per-slot deep links turned out not to work upstream — the
        # booking flow is session-bound; see catalog.booking_start_url).
        if ":" not in slot_token:
            lang = "en" if request.args.get("lang") == "en" else "de"
            if not re.fullmatch(r"[a-z0-9-]+", slot_token):
                return _result_page("link_expired", lang, status=410)
            try:
                scfg = load_catalog(slot_token).scraper_config
                return redirect(booking_start_url(scfg, lang), code=302)
            except CatalogError:
                return _result_page("link_expired", lang, status=410)
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
        from datetime import datetime as _dt
        from app.admin import stats, summary_anomalies
        conn = connect(cfg.db_path)
        s = stats(conn, cfg)
        # Admin is an internal, English-only stats page — hide the (no-op)
        # DE/EN switcher that base.html otherwise renders.
        return render_template("admin.html", stats=s,
                               anomalies=summary_anomalies(s, now=_dt.utcnow()),
                               show_lang_switcher=False)

    @app.route("/datenschutz")
    def datenschutz_route():
        cfg = app.config["TERMINE_CONFIG"]
        # The retention periods on the page are the configured ones, so a
        # changed .env cannot leave the privacy notice promising the old term.
        return render_template("datenschutz.html", lang=request.args.get("lang", "de"),
                               ttl_days=ttl_days_for(cfg, False),
                               sensitive_ttl_days=ttl_days_for(cfg, True),
                               grace_days=cfg.expired_grace_days)

    @app.route("/impressum")
    def impressum_route():
        return render_template("impressum.html", lang=request.args.get("lang", "de"))

    @app.route("/kontakt", methods=["GET", "POST"])
    def kontakt_route():
        """§ 5 DDG second contact channel, shared by all three sites.

        DDG requires a means of "unmittelbare Kommunikation" alongside the
        email address; ECJ C-298/07 established that a web form satisfies
        this and that a phone number is not required. PapaMap is a static
        site with no backend of its own, so all three Impressums link here
        and pass ?projekt= to say which site the message concerns.
        """
        cfg = app.config["TERMINE_CONFIG"]
        lang = request.values.get("lang", "de")
        if lang not in ("de", "en"):
            lang = "de"
        projekt = request.values.get("projekt", "")
        if projekt not in _CONTACT_PROJECTS:
            projekt = ""

        if request.method == "GET":
            return render_template("kontakt.html", lang=lang, projekt=projekt,
                                   projects=_CONTACT_PROJECTS,
                                   kofi_url=cfg.kofi_url)

        # Honeypot: a field hidden from humans. Bots fill it, so answer 200
        # without sending — a 4xx would tell the bot to retry differently.
        if request.form.get("website", ""):
            return ("", 200)
        email = request.form.get("email", "").strip()
        # Stricter than /subscribe's "@" check: this address is echoed into a
        # Reply-To header, so anything with whitespace, control characters or
        # a missing domain is rejected rather than handed to the provider.
        if not _EMAIL_RE.fullmatch(email):
            return _result_page("invalid_email", lang, status=400)
        message = request.form.get("message", "").strip()
        if not message:
            return _result_page("contact_missing", lang, status=400)
        if not GLOBAL_IP_LIMITER.hit(f"contact:{_client_ip()}",
                                     cfg.contact_ratelimit_per_ip_per_hour,
                                     3600):
            return _result_page("rate_limited", lang, status=429)

        name = request.form.get("name", "").strip()[:_CONTACT_NAME_MAX]
        message = message[:_CONTACT_MESSAGE_MAX]
        label = _CONTACT_PROJECTS.get(projekt, "unbekannt")
        subject = f"[Kontakt] {label}: {name or email}"
        body = (f"Projekt: {label}\n"
                f"Name: {name or '—'}\n"
                f"E-Mail: {email}\n"
                f"Sprache: {lang}\n\n"
                f"{message}\n")
        # Bucket the idempotency key by 10-minute window so an impatient
        # double-submit of the same text doesn't arrive twice, while a
        # genuinely new message later still gets through.
        bucket = int(time_mod.time() // 600)
        idem = hashlib.sha256(
            f"contact|{email}|{message}|{bucket}".encode("utf-8")).hexdigest()
        conn = connect(cfg.db_path)
        try:
            # reply_to overrides REPLY_TO_EMAIL for this message only, so
            # hitting reply answers the visitor instead of our own mailbox.
            mail_send(conn, cfg.developer_email, subject, body, idem_key=idem,
                      reply_to=email)
        except Exception:
            log.exception("contact form delivery failed for %s", email)
            return _result_page("contact_failed", lang, status=502)
        return _result_page("contact_sent", lang)

    return app

# NOTE: do NOT instantiate `app = create_app()` at module level. Doing so
# calls load_config() at import time, which raises KeyError if any env var
# is missing - including during test collection, where fixtures haven't
# yet had a chance to monkeypatch.setenv(). Gunicorn supports the
# application-factory pattern directly: `gunicorn app.web:create_app()`.
