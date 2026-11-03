"""Confirmation-email delivery for pending sign-ups.

Only the poller sends them, once a cycle before the digests, through the same
quota-aware batch path, so they take pool room first and the digests get what
is left. The sign-up request just saves the row: a web request
never waits on a mail provider. When the quota is exhausted a sign-up is NOT
lost: its pending row stays valid and a later cycle sends it (i.e. the next
day once quota resets). `confirmation_sent_at` marks a sign-up as done so it
isn't re-sent.
"""
from __future__ import annotations
import sqlite3
from urllib.parse import urlsplit
from app.mail import send_batch, pool_usage, pool_room, Outgoing, _idem_key, _dead_addresses
from app.repo import set_confirmation_sent, pending_confirmations
from app.tokens import sign


_TEXT = {
    "de": {
        "subject": "Bitte bestätige deine Anmeldung bei Bürgerwecker",
        "signed_up_city": "du hast dich auf {host} für Terminbenachrichtigungen in {city} angemeldet.",
        "signed_up": "du hast dich auf {host} für Terminbenachrichtigungen angemeldet.",
        "body": (
            "Hallo,\n\n{signed_up}\n\n"
            "Klick auf diesen Link, um die Anmeldung zu bestätigen:\n\n{url}\n\n"
            "Erst danach bekommst du eine Mail, sobald ein passender Termin frei "
            "wird. Falls der Link nicht anklickbar ist, kopiere ihn in die "
            "Adresszeile deines Browsers.\n\n"
            "Wenn du dich nicht angemeldet hast, ignoriere diese Mail einfach.\n\n"
            "Bürgerwecker\n"),
    },
    "en": {
        "subject": "Please confirm your Bürgerwecker sign-up",
        "signed_up_city": "you signed up on {host} for appointment notifications in {city}.",
        "signed_up": "you signed up on {host} for appointment notifications.",
        "body": (
            "Hello,\n\n{signed_up}\n\n"
            "Click this link to confirm your sign-up:\n\n{url}\n\n"
            "Only then will you get an email as soon as a matching slot opens "
            "up. If the link is not clickable, copy it into your browser's "
            "address bar.\n\n"
            "If you did not sign up, just ignore this email.\n\n"
            "Bürgerwecker\n"),
    },
}


def build_confirmation(sub_id: int, email: str, lang: str, city: str,
                       cfg) -> Outgoing:
    from app.catalog import city_display_name
    tok = sign(sub_id, "confirm",
               primary=cfg.token_secret_primary,
               previous=cfg.token_secret_previous)
    url = f"{cfg.public_base_url}/confirm/{tok}"
    host = urlsplit(cfg.public_base_url).netloc or cfg.public_base_url
    city_name = city_display_name(city, lang)
    t = _TEXT["en" if lang == "en" else "de"]
    # Say who is writing, why, and that the link is the action. The old
    # one-liner ("Bitte bestätige dein Abonnement: <url>") read as a request
    # to answer, and people replied to it instead of clicking.
    subject = f"{t['subject']} ({city_name})" if city_name else t["subject"]
    signed_up = (t["signed_up_city"].format(host=host, city=city_name)
                 if city_name else t["signed_up"].format(host=host))
    body = t["body"].format(signed_up=signed_up, url=url)
    # Stable per-subscription key: a deferred send and its later retry share it,
    # so the idempotency layer never double-sends a confirmation.
    return Outgoing(to=email, subject=subject, body=body,
                    idem_key=_idem_key(sub_id, [], f"confirm-{sub_id}"))


def confirmation_room(conn: sqlite3.Connection, cfg) -> int | None:
    """How many confirmations may still go out before the mail pool's
    rolling-24h usage reaches MAIL_POOL_PRESSURE_PCT of its summed daily caps;
    None when no such line is set. Past it, sign-ups get only a reserve of
    CONFIRMATION_RESERVE_PER_DAY a day (an unconfirmed sign-up is usually lost
    for good, a held digest only goes out later). The reserve is the flood
    bound: a burst of sign-ups, real or scripted, takes at most that much of
    the digests' share a day. Capped by `mail.pool_room`, what the providers
    can take right now, so a pool the digests filled reads as held. A held sign-up stays pending and goes out once
    the window frees, within its 7-day retry window."""
    pct = getattr(cfg, "mail_pool_pressure_pct", 0) or 0
    if not pct:
        return None
    used, pool = pool_usage(conn, cfg)
    if not pool:
        return None
    reserve = getattr(cfg, "confirmation_reserve_per_day", 0) or 0
    sent = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE "
                        "confirmation_sent_at > datetime('now','-1 day')"
                        ).fetchone()[0]
    # Never more than the providers can take right now: digests may have
    # filled the pool, and a room send_batch would defer is a held sign-up.
    return min(pool_room(conn, cfg),
               max(0, pool * pct // 100 - used, reserve - sent))


def _queue(conn: sqlite3.Connection, cfg, max_age_days: int = 7) -> list:
    """Pending sign-ups a send can reach, oldest first. A dead address
    (send_batch skips it) would otherwise hold a slot at the head of the
    queue for its whole retry window."""
    dead = _dead_addresses(conn, cfg)
    return [p for p in pending_confirmations(conn, max_age_days=max_age_days)
            if p[1] not in dead]


def confirmation_held(conn: sqlite3.Connection, cfg, *, extra: int = 0) -> bool:
    """Whether the newest sign-up waits past the next cycle: the pressure
    line leaves less room than the queue ahead of it and itself. `extra`
    counts sign-ups not saved yet (the form asks for one about to be made)."""
    room = confirmation_room(conn, cfg)
    return room is not None and len(_queue(conn, cfg)) + extra > room


def send_pending_confirmations(conn: sqlite3.Connection, cfg, *,
                               max_age_days: int = 7) -> None:
    """Send the confirmation emails of sign-ups that have not had one, oldest
    first, no further than `confirmation_room`. Called once per poll cycle."""
    pending = _queue(conn, cfg, max_age_days)
    room = confirmation_room(conn, cfg)
    if room is not None:
        pending = pending[:room]
    if not pending:
        return
    items = [build_confirmation(sub_id, email, lang, city, cfg)
             for (sub_id, email, lang, city) in pending]
    key_to_sub = {item.idem_key: sub_id
                  for item, (sub_id, _e, _l, _c) in zip(items, pending)}
    result = send_batch(conn, items, cfg)
    for idem_key in result.delivered:
        set_confirmation_sent(conn, key_to_sub[idem_key])
