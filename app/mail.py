from __future__ import annotations
import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
import requests

class MailFailed(Exception):
    pass

def _idem_key(subscription_id: int, slot_hashes: list[str], cycle_id: str) -> str:
    payload = f"{subscription_id}|{','.join(sorted(slot_hashes))}|{cycle_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _mailjet_message(to: str, subject: str, body: str) -> dict:
    """One entry of Mailjet's v3.1 `Messages` array (shared by single + batch)."""
    message = {
        "From": {"Email": os.environ["MAILJET_FROM_EMAIL"],
                 "Name":  os.environ["MAILJET_FROM_NAME"]},
        "To":   [{"Email": to}],
        "Subject":  subject,
        "TextPart": body,
        "Headers":  {
            "List-Unsubscribe": _list_unsub_header(to),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    # From is the validated sending subdomain; Reply-To (optional) routes
    # replies to a real mailbox so the From address can be a subdomain that
    # doesn't itself receive mail.
    reply_to = os.environ.get("REPLY_TO_EMAIL")
    if reply_to:
        message["ReplyTo"] = {"Email": reply_to}
    return message

def _resend_email(to: str, subject: str, body: str) -> dict:
    """One Resend email object (shared by single `/emails` + `/emails/batch`)."""
    payload = {
        "from": f"{os.environ['MAILJET_FROM_NAME']} <{os.environ['MAILJET_FROM_EMAIL']}>",
        "to": [to],
        "subject": subject,
        "text": body,
        "headers": {
            "List-Unsubscribe": _list_unsub_header(to),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    reply_to = os.environ.get("REPLY_TO_EMAIL")
    if reply_to:
        payload["reply_to"] = reply_to
    return payload

def _call_mailjet(to: str, subject: str, body: str) -> Any:
    return requests.post(
        "https://api.mailjet.com/v3.1/send",
        auth=(os.environ["MAILJET_API_KEY"], os.environ["MAILJET_API_SECRET"]),
        json={"Messages": [_mailjet_message(to, subject, body)]},
        timeout=30,
    )

def _call_resend(to: str, subject: str, body: str) -> Any:
    return requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json=_resend_email(to, subject, body),
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


# --------------------------------------------------------------------------
# Batched, quota-aware delivery (notification digests).
#
# Free-tier providers cap total sends (Resend ~100/day, Mailjet ~10/hour), so
# a notification burst must (a) be sent in as few HTTP calls as possible and
# (b) stop before a provider's cap to avoid account blocks. `send_batch` packs
# recipients into provider batch calls, sends only within each provider's
# remaining rolling-window quota, and DEFERS the rest (releasing their
# idempotency claims so a later cycle retries them).
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Outgoing:
    to: str
    subject: str
    body: str
    idem_key: str

@dataclass
class BatchResult:
    delivered: set[str] = field(default_factory=set)  # idem_keys actually sent
    deferred: int = 0                                  # left for a later cycle
    sent_by_provider: dict[str, int] = field(default_factory=dict)

def _call_mailjet_batch(items: list[Outgoing]) -> bool:
    resp = requests.post(
        "https://api.mailjet.com/v3.1/send",
        auth=(os.environ["MAILJET_API_KEY"], os.environ["MAILJET_API_SECRET"]),
        json={"Messages": [_mailjet_message(i.to, i.subject, i.body) for i in items]},
        timeout=60,
    )
    return resp.status_code < 400

def _call_resend_batch(items: list[Outgoing]) -> bool:
    resp = requests.post(
        "https://api.resend.com/emails/batch",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json=[_resend_email(i.to, i.subject, i.body) for i in items],
        timeout=60,
    )
    return resp.status_code < 400

def _window_used(conn: sqlite3.Connection, provider: str, window_seconds: int) -> int:
    """Count emails a provider actually sent within the last `window_seconds`.
    Reads sent_idempotency (14-day retention covers our day/hour windows)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sent_idempotency "
        "WHERE provider = ? AND sent_at > datetime('now', ?)",
        (provider, f"-{window_seconds} seconds"),
    ).fetchone()
    return row["n"] if row else 0

def _providers(cfg) -> list[tuple]:
    """Ordered (name, send_fn, batch_size, [(limit, window_seconds), ...]).

    Order follows cfg.email_provider_order (default Mailjet-first, so Mailjet's
    account sees the notification traffic — the prerequisite for getting its
    new-sender throttle lifted; Resend absorbs whatever exceeds Mailjet's
    hourly allowance). Resend is skipped when no API key is configured. Each
    provider's window usage already includes transactional emails sent via
    `send()`, since those are recorded under the same provider name.
    """
    # Mailjet is bounded by BOTH its hourly cap (the new-sender warm-up
    # throttle) and its daily cap (free tier = 200/day); _headroom takes the
    # tighter of the two. Resend's free tier is a flat daily cap.
    available = {
        "mailjet": ("mailjet", _call_mailjet_batch, 50,
                    [(cfg.mailjet_hourly_quota, 3600),
                     (cfg.mailjet_daily_quota, 86400)]),
        "resend": ("resend", _call_resend_batch, 100,
                   [(cfg.resend_daily_quota, 86400)]),
    }
    order = getattr(cfg, "email_provider_order", ("mailjet", "resend"))
    specs: list[tuple] = []
    for name in order:
        spec = available.get(name)
        if spec is None:
            continue
        if name == "resend" and not os.environ.get("RESEND_API_KEY"):
            continue
        specs.append(spec)
    return specs

def _headroom(conn: sqlite3.Connection, limits: list[tuple], provider: str) -> int:
    room = None
    for limit, window in limits:
        avail = limit - _window_used(conn, provider, window)
        room = avail if room is None else min(room, avail)
    return max(0, room if room is not None else 0)

def send_batch(conn: sqlite3.Connection, items: list[Outgoing], cfg) -> BatchResult:
    """Send `items` within provider quotas, batched. Returns what was delivered.

    Claims each idempotency row first (INSERT OR IGNORE); already-claimed keys
    are skipped as already-sent. Newly-claimed items are packed into provider
    batch calls up to each provider's remaining rolling-window quota. A chunk
    that fails at the HTTP level has its claims released and falls through to
    the next provider. Anything past the combined quota is deferred: its claim
    is released so a later cycle re-sends it (fresh cycle_id ⇒ fresh idem_key).
    """
    from app.db import transaction
    result = BatchResult()
    pending: list[Outgoing] = []
    # Claim all idempotency rows in ONE transaction. In autocommit each INSERT
    # would fsync separately — fatal when a popular slot matches tens of
    # thousands of subscribers (that many fsyncs would overrun the cycle).
    with transaction(conn):
        for it in items:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sent_idempotency (idem_key, provider) "
                "VALUES (?, 'pending')",
                (it.idem_key,),
            )
            if cur.rowcount == 1:
                pending.append(it)
            # rowcount 0 → already sent/claimed by an earlier cycle: skip.

    remaining = list(pending)
    for name, send_fn, batch_size, limits in _providers(cfg):
        if not remaining:
            break
        room = _headroom(conn, limits, name)
        while remaining and room > 0:
            take = min(batch_size, room, len(remaining))
            chunk = remaining[:take]
            try:
                ok = send_fn(chunk)
            except Exception:
                ok = False
            if not ok:
                # Provider errored: stop using it and leave these items claimed
                # (still 'pending') so the next provider can send them. If every
                # provider fails, the trailing deferral block releases them.
                break
            with transaction(conn):
                conn.executemany(
                    "UPDATE sent_idempotency SET provider=?, sent_at=CURRENT_TIMESTAMP "
                    "WHERE idem_key=?",
                    [(name, c.idem_key) for c in chunk],
                )
            for c in chunk:
                result.delivered.add(c.idem_key)
            result.sent_by_provider[name] = result.sent_by_provider.get(name, 0) + take
            remaining = remaining[take:]
            room -= take

    if remaining:
        # Over quota (or every provider failed): defer. Release the claims so
        # the next cycle can retry — do NOT mark seen; the caller must skip
        # recording seen_slots for these so they resurface. One transaction so
        # the release is a single fsync, not one per deferred item.
        with transaction(conn):
            conn.executemany("DELETE FROM sent_idempotency WHERE idem_key=?",
                             [(c.idem_key,) for c in remaining])
        result.deferred = len(remaining)
    return result

def maybe_quota_alert(conn: sqlite3.Connection, cfg, *, deferred: int) -> None:
    """Email the developer when daily send volume nears the free-tier cap, or
    when notifications had to be deferred for lack of quota. Rate-limited to
    once per 24h via meta. This is the signal to upgrade to a paid plan."""
    used = _window_used(conn, "resend", 86400)
    threshold = cfg.resend_daily_quota * cfg.quota_alert_threshold_pct / 100
    if deferred == 0 and used < threshold:
        return
    if not cfg.developer_email:
        return
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_quota_alert_at'"
    ).fetchone()
    if row:
        try:
            if datetime.utcnow() - datetime.fromisoformat(row["value"]) < timedelta(hours=24):
                return
        except ValueError:
            pass
    pct = round(used / cfg.resend_daily_quota * 100) if cfg.resend_daily_quota else 0
    subject = "[termine-notifier] email quota running low"
    body = (
        f"Resend usage in the last 24h: {used}/{cfg.resend_daily_quota} ({pct}%).\n"
        f"Notifications deferred this cycle for lack of quota: {deferred}.\n\n"
        "Subscribers may be going un-notified. Consider upgrading to a paid "
        "email plan (e.g. Resend Pro) and raising RESEND_DAILY_QUOTA / "
        "MAILJET_HOURLY_QUOTA accordingly."
    )
    try:
        send(conn, cfg.developer_email, subject, body,
             idem_key=_idem_key(0, [], f"quota-alert-{datetime.utcnow().date()}"))
    except Exception:
        # Alerting must never break a delivery cycle.
        return
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_quota_alert_at', ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP",
        (datetime.utcnow().isoformat(),),
    )
