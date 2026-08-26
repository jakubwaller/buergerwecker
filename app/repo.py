from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from app.models import Filter, Subscription

def insert_pending(conn: sqlite3.Connection, *, email: str, city: str,
                   language: str, filter_: Filter, ttl_days: int,
                   consent_special: bool = False) -> int:
    """Stage an unconfirmed sign-up.

    `consent_special` records the separate Art. 9(2)(a) consent a sensitive
    service needs. It is stamped here rather than at confirmation time because
    that is when it was actually given; the double opt-in on top is what makes
    it verifiable (Art. 7(1)).
    """
    expires_at = (datetime.utcnow() + timedelta(days=ttl_days)).isoformat()
    cur = conn.execute(
        "INSERT INTO subscriptions (email, city, language, filters_json, "
        "expires_at, consent_special_at) VALUES (?,?,?,?,?,?)",
        (email, city, language, filter_.to_json(), expires_at,
         datetime.utcnow().isoformat() if consent_special else None),
    )
    return cur.lastrowid


def set_special_consent(conn: sqlite3.Connection, sub_id: int,
                        given: bool) -> None:
    """Record (or clear) the Art. 9 consent on an existing subscription.

    Cleared when someone edits their filter back to an ordinary service: the
    consent covered that one selection, so keeping the stamp would overstate
    what they agreed to.
    """
    conn.execute(
        "UPDATE subscriptions SET consent_special_at=? WHERE id=?",
        (datetime.utcnow().isoformat() if given else None, sub_id),
    )

def confirm(conn: sqlite3.Connection, sub_id: int) -> None:
    conn.execute(
        "UPDATE subscriptions SET confirmed_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND confirmed_at IS NULL",
        (sub_id,),
    )

def soft_delete(conn: sqlite3.Connection, sub_id: int) -> None:
    conn.execute(
        "UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
        (sub_id,),
    )

def set_confirmation_sent(conn: sqlite3.Connection, sub_id: int) -> None:
    conn.execute(
        "UPDATE subscriptions SET confirmation_sent_at=CURRENT_TIMESTAMP WHERE id=?",
        (sub_id,),
    )

def pending_confirmations(conn: sqlite3.Connection, *,
                          max_age_days: int = 7) -> list[tuple[int, str, str, str]]:
    """Sign-ups still awaiting a confirmation email: unconfirmed, not deleted,
    no confirmation delivered yet, created within `max_age_days` (older ones are
    abandoned rather than retried forever). Oldest first for fair delivery."""
    rows = conn.execute(
        "SELECT id, email, language, city FROM subscriptions "
        "WHERE confirmed_at IS NULL AND deleted_at IS NULL "
        "AND confirmation_sent_at IS NULL "
        "AND created_at > datetime('now', ?) "
        "ORDER BY created_at",
        (f"-{max_age_days} days",),
    ).fetchall()
    return [(r["id"], r["email"], r["language"], r["city"]) for r in rows]

def _row_to_subscription(row: sqlite3.Row) -> Subscription:
    from datetime import datetime
    def _p(s): return datetime.fromisoformat(s) if s else None
    return Subscription(
        id=row["id"],
        email=row["email"],
        city=row["city"],
        language=row["language"],
        sub_filter=Filter.from_json(row["filters_json"]),
        created_at=_p(row["created_at"]),
        confirmed_at=_p(row["confirmed_at"]),
        last_notified_at=_p(row["last_notified_at"]),
        expires_at=_p(row["expires_at"]),
        reminder_sent_at=_p(row["reminder_sent_at"]),
        heartbeat_30d_at=_p(row["heartbeat_30d_at"]),
        heartbeat_60d_at=_p(row["heartbeat_60d_at"]),
        deleted_at=_p(row["deleted_at"]),
        last_match_count=(row["last_match_count"]
                          if "last_match_count" in row.keys() else None),
        consecutive_digests=(row["consecutive_digests"]
                             if "consecutive_digests" in row.keys() else 0) or 0,
    )

def active_subscriptions(conn: sqlite3.Connection) -> list[Subscription]:
    rows = conn.execute(
        "SELECT * FROM subscriptions "
        "WHERE confirmed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND expires_at > CURRENT_TIMESTAMP "
        "ORDER BY id"
    ).fetchall()
    return [_row_to_subscription(r) for r in rows]

def set_last_notified(conn: sqlite3.Connection, sub_id: int,
                      match_count: int | None = None) -> None:
    """Stamp a delivered digest. `match_count` is how many slots the filter
    matched in that cycle (seen ones included) — the adaptive rate limit reads
    it back next cycle. COALESCE keeps the previous measurement when a caller
    passes nothing, so an unmeasured send never resets a subscriber to the
    base interval."""
    conn.execute("UPDATE subscriptions SET last_notified_at=CURRENT_TIMESTAMP, "
                 "last_match_count=COALESCE(?, last_match_count), "
                 "consecutive_digests=consecutive_digests+1 WHERE id=?",
                 (match_count, sub_id))

def reset_digest_streak(conn: sqlite3.Connection, sub_id: int) -> None:
    """End a subscriber's unbroken run of digests — called when they were due
    for one and there was nothing to send."""
    conn.execute("UPDATE subscriptions SET consecutive_digests=0 WHERE id=?",
                 (sub_id,))

def record_seen_slot(conn: sqlite3.Connection, sub_id: int, slot_hash: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_slots (subscription_id, slot_hash) VALUES (?,?)",
        (sub_id, slot_hash),
    )

def has_seen_slot(conn: sqlite3.Connection, sub_id: int, slot_hash: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_slots WHERE subscription_id=? AND slot_hash=?",
        (sub_id, slot_hash),
    ).fetchone() is not None

def record_digest_delivery(conn: sqlite3.Connection, sub_id: int) -> None:
    """One delivered digest — what the per-subscriber daily cap counts."""
    conn.execute("INSERT INTO digest_deliveries (subscription_id) VALUES (?)",
                 (sub_id,))

def digests_in_window(conn: sqlite3.Connection, sub_id: int, *,
                      hours: int = 24) -> int:
    """Digests delivered to this subscriber in the last `hours` (rolling)."""
    return conn.execute(
        "SELECT COUNT(*) FROM digest_deliveries "
        "WHERE subscription_id=? AND sent_at > datetime('now', ?)",
        (sub_id, f"-{int(hours)} hours"),
    ).fetchone()[0]

def record_cap_hold(conn: sqlite3.Connection, sub_id: int) -> None:
    """This subscriber had a digest ready and the cap held it back today
    (UTC). Idempotent per day — a capped subscriber is re-evaluated every
    cycle, and the record is 'was held', not 'how many cycles'."""
    conn.execute("INSERT OR IGNORE INTO digest_cap_holds (day, subscription_id) "
                 "VALUES (date('now'), ?)", (sub_id,))

# --------------------------------------------------------------------------
# Suppression list (see the email_suppressions comment in app/db.py).
# --------------------------------------------------------------------------

def suppress_address(conn: sqlite3.Connection, email: str, *, reason: str,
                     provider: str | None = None,
                     detail: str | None = None) -> None:
    """Stop mailing `email` for good. Idempotent, and the FIRST reason wins:
    providers retry webhooks and a dead mailbox often reports twice, so
    re-suppressing must not rewrite why we stopped or when."""
    conn.execute(
        "INSERT INTO email_suppressions "
        "  (email, reason, provider, detail, suppressed_at, updated_at) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (email) DO UPDATE SET "
        "  reason        = COALESCE(email_suppressions.reason, excluded.reason), "
        "  provider      = COALESCE(email_suppressions.provider, excluded.provider), "
        "  detail        = COALESCE(email_suppressions.detail, excluded.detail), "
        "  suppressed_at = COALESCE(email_suppressions.suppressed_at, "
        "                           excluded.suppressed_at), "
        "  updated_at    = CURRENT_TIMESTAMP",
        (email, reason, provider, detail),
    )

def record_soft_bounce(conn: sqlite3.Connection, email: str, *,
                       threshold: int, provider: str | None = None,
                       detail: str | None = None) -> bool:
    """Count one temporary delivery failure. Returns True if this one crossed
    `threshold` and turned into a suppression.

    A soft bounce is a full mailbox or a greylisting receiver, so one is noise.
    A run of them is an address that never accepts mail, which damages the
    sending reputation exactly like a hard bounce does. `threshold <= 0`
    disables the escalation and only counts."""
    conn.execute(
        "INSERT INTO email_suppressions (email, soft_bounces, provider, updated_at) "
        "VALUES (?, 1, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT (email) DO UPDATE SET "
        "  soft_bounces = email_suppressions.soft_bounces + 1, "
        "  updated_at   = CURRENT_TIMESTAMP",
        (email, provider),
    )
    if threshold <= 0:
        return False
    row = conn.execute(
        "SELECT soft_bounces, reason FROM email_suppressions WHERE email=?",
        (email,),
    ).fetchone()
    if row and row["reason"] is None and row["soft_bounces"] >= threshold:
        suppress_address(conn, email, reason="soft_bounce", provider=provider,
                         detail=detail)
        return True
    return False

def clear_soft_bounces(conn: sqlite3.Connection, email: str) -> None:
    """A confirmed delivery means the transient trouble is over. Deliberately
    does NOT lift a suppression: a hard bounce or a spam complaint is not
    undone by a later message reaching the mailbox.

    Guarded by a read because this runs once per *delivered* message — the
    highest-volume event there is — and almost every one of them has nothing to
    clear. The lookup is an indexed point read; the write it avoids would be an
    fsync per delivered mail."""
    row = conn.execute(
        "SELECT 1 FROM email_suppressions "
        "WHERE email=? AND reason IS NULL AND soft_bounces > 0",
        (email,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        "UPDATE email_suppressions SET soft_bounces=0, updated_at=CURRENT_TIMESTAMP "
        "WHERE email=? AND reason IS NULL",
        (email,),
    )

def suppressed_addresses(conn: sqlite3.Connection) -> set[str]:
    return {r["email"] for r in conn.execute(
        "SELECT email FROM email_suppressions WHERE reason IS NOT NULL")}

def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_suppressions WHERE email=? AND reason IS NOT NULL",
        (email,),
    ).fetchone()
    return row is not None

def soft_delete_by_email(conn: sqlite3.Connection, email: str) -> int:
    """Delete every live subscription held by `email`. Returns how many.

    One person may hold several subscriptions and a bounce or a complaint is a
    verdict on the address, not on one of them."""
    cur = conn.execute(
        "UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP "
        "WHERE email=? AND deleted_at IS NULL",
        (email,),
    )
    return cur.rowcount or 0

def suppression_reason(conn: sqlite3.Connection, email: str) -> str | None:
    """Why `email` is suppressed, or None if it is mailable."""
    row = conn.execute(
        "SELECT reason FROM email_suppressions WHERE email=? AND reason IS NOT NULL",
        (email,),
    ).fetchone()
    return row["reason"] if row else None

def clear_delivery_block(conn: sqlite3.Connection, email: str) -> None:
    """Make a bounced address mailable again, and forget why it wasn't.

    Called when someone signs up with an address we had retired over delivery
    failures. A bounce only ever claimed the mailbox was broken *then*, and a
    person typing that address into the form is the evidence it is working now
    — so the right move is to try again rather than leave them in a silent hole
    where the confirmation mail is dropped and the page still says "check your
    inbox". If the mailbox really is still broken, one bounce re-suppresses it.

    Deliberately refuses to lift a complaint: that is a person telling their
    provider we are spam, a form submission is not their word for it, and
    lifting it is the one thing that has to go through a human.
    """
    conn.execute("DELETE FROM email_suppressions "
                 "WHERE email=? AND (reason IS NULL OR reason != 'complaint')",
                 (email,))
    conn.execute("DELETE FROM email_failures WHERE email=?", (email,))
