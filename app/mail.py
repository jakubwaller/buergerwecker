from __future__ import annotations
import hashlib
import os
import sqlite3
from typing import Any
import requests

class MailFailed(Exception):
    pass

def _idem_key(subscription_id: int, slot_hashes: list[str], cycle_id: str) -> str:
    payload = f"{subscription_id}|{','.join(sorted(slot_hashes))}|{cycle_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _call_mailjet(to: str, subject: str, body: str) -> Any:
    return requests.post(
        "https://api.mailjet.com/v3.1/send",
        auth=(os.environ["MAILJET_API_KEY"], os.environ["MAILJET_API_SECRET"]),
        json={"Messages": [{
            "From": {"Email": os.environ["MAILJET_FROM_EMAIL"],
                     "Name":  os.environ["MAILJET_FROM_NAME"]},
            "To":   [{"Email": to}],
            "Subject":  subject,
            "TextPart": body,
            "Headers":  {
                "List-Unsubscribe": _list_unsub_header(to),
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        }]},
        timeout=30,
    )

def _call_resend(to: str, subject: str, body: str) -> Any:
    return requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={
            "from": f"{os.environ['MAILJET_FROM_NAME']} <{os.environ['MAILJET_FROM_EMAIL']}>",
            "to": [to],
            "subject": subject,
            "text": body,
            "headers": {
                "List-Unsubscribe": _list_unsub_header(to),
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        },
        timeout=30,
    )

def _list_unsub_header(to_email: str) -> str:
    # Caller is expected to inject the actual unsubscribe URL via the
    # mail-template flow; this is a placeholder header until tied in.
    return f"<{os.environ.get('PUBLIC_BASE_URL', '')}/unsubscribe>"

def send(conn: sqlite3.Connection, to: str, subject: str, body: str,
         *, idem_key: str) -> None:
    """Send `body` to `to`. Idempotent on `idem_key`.

    Order: claim the idempotency row FIRST (atomic INSERT OR IGNORE), then
    attempt sends. If both providers fail the claim is rolled back so a
    retry can proceed. If the process dies between claim and successful
    send, the row remains with provider='pending' and the next call
    short-circuits — preventing a double-send on crash recovery.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO sent_idempotency (idem_key, provider) "
        "VALUES (?, 'pending')",
        (idem_key,),
    )
    if cur.rowcount == 0:
        return  # already claimed by an earlier call
    try:
        resp = _call_mailjet(to, subject, body)
        provider = "mailjet"
        # Fail over to Resend on ANY Mailjet error (4xx incl. 401/403 account
        # blocks, and 5xx/429), not just transient ones — a blocked Mailjet
        # account returns 401, and that's exactly when the fallback must engage.
        if resp.status_code >= 400 and os.environ.get("RESEND_API_KEY"):
            resp = _call_resend(to, subject, body)
            provider = "resend"
        if resp.status_code >= 400:
            raise MailFailed(f"provider failed; last status {resp.status_code}")
    except Exception:
        conn.execute("DELETE FROM sent_idempotency WHERE idem_key=?", (idem_key,))
        raise
    conn.execute(
        "UPDATE sent_idempotency SET provider=? WHERE idem_key=?",
        (provider, idem_key),
    )
