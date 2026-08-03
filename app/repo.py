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
