"""Confirmation-email delivery for pending sign-ups.

Confirmation emails go through the same quota-aware batch path as digests, so
when the daily email quota is exhausted a sign-up is NOT lost: its pending row
stays valid and `send_pending_confirmations` re-sends the confirmation on a
later poll cycle (i.e. the next day once quota resets). `confirmation_sent_at`
marks a sign-up as done so it isn't re-sent.
"""
from __future__ import annotations
import sqlite3
from app.mail import send_batch, Outgoing, _idem_key
from app.repo import set_confirmation_sent, pending_confirmations
from app.tokens import sign


def build_confirmation(sub_id: int, email: str, lang: str, cfg) -> Outgoing:
    tok = sign(sub_id, "confirm",
               primary=cfg.token_secret_primary,
               previous=cfg.token_secret_previous)
    url = f"{cfg.public_base_url}/confirm/{tok}"
    if lang == "en":
        subject, body = "Confirmation needed", f"Please confirm your subscription: {url}"
    else:
        subject, body = "Bestätigung benötigt", f"Bitte bestätige dein Abonnement: {url}"
    # Stable per-subscription key: a deferred send and its later retry share it,
    # so the idempotency layer never double-sends a confirmation.
    return Outgoing(to=email, subject=subject, body=body,
                    idem_key=_idem_key(sub_id, [], f"confirm-{sub_id}"))


def send_confirmation_now(conn: sqlite3.Connection, sub_id: int, email: str,
                          lang: str, cfg) -> bool:
    """Try to send this sign-up's confirmation immediately. Returns True if it
    went out, False if it was deferred (quota exhausted) — in which case the
    pending row stays put and `send_pending_confirmations` retries it later."""
    item = build_confirmation(sub_id, email, lang, cfg)
    result = send_batch(conn, [item], cfg)
    if item.idem_key in result.delivered:
        set_confirmation_sent(conn, sub_id)
        return True
    return False


def send_pending_confirmations(conn: sqlite3.Connection, cfg, *,
                               max_age_days: int = 7) -> None:
    """Retry confirmation emails for sign-ups that never got one (quota was
    exhausted when they registered). Called once per poll cycle."""
    pending = pending_confirmations(conn, max_age_days=max_age_days)
    if not pending:
        return
    items = [build_confirmation(sub_id, email, lang, cfg)
             for (sub_id, email, lang) in pending]
    key_to_sub = {item.idem_key: sub_id
                  for item, (sub_id, _e, _l) in zip(items, pending)}
    result = send_batch(conn, items, cfg)
    for idem_key in result.delivered:
        set_confirmation_sent(conn, key_to_sub[idem_key])
