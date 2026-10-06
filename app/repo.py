from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from app.db import sql_ts
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
    expires_at = sql_ts(datetime.utcnow() + timedelta(days=ttl_days))
    cur = conn.execute(
        "INSERT INTO subscriptions (email, city, language, filters_json, "
        "expires_at, consent_special_at) VALUES (?,?,?,?,?,?)",
        (email, city, language, filter_.to_json(), expires_at,
         sql_ts(datetime.utcnow()) if consent_special else None),
    )
    return cur.lastrowid


def insert_push_subscription(conn: sqlite3.Connection, *, device_id: int,
                             city: str, language: str, filter_: Filter,
                             ttl_days: int, consent_special: bool = False) -> int:
    """An app subscription, live at once. There is no double opt-in: the OS
    permission prompt the app had to pass is the opt-in, and there is no
    address to verify. `email` is the '' sentinel (see the schema)."""
    now = sql_ts(datetime.utcnow())
    expires_at = sql_ts(datetime.utcnow() + timedelta(days=ttl_days))
    cur = conn.execute(
        "INSERT INTO subscriptions (email, device_id, city, language, "
        "filters_json, confirmed_at, expires_at, consent_special_at) "
        "VALUES ('', ?,?,?,?,?,?,?)",
        (device_id, city, language, filter_.to_json(), now, expires_at,
         now if consent_special else None),
    )
    return cur.lastrowid


def register_device(conn: sqlite3.Connection, *, platform: str, token: str,
                    secret_hash: str, language: str) -> int:
    """Create or revive the row for a push token. The same token coming back
    (reinstall, a re-run of the app's first launch) is the same device: it
    takes the new secret, loses any retirement and any remembered dead
    answer (the evidence clock starts over), and keeps its subscriptions.

    Every registration, the first or a repeat, leaves the device unverified
    and asks for a fresh verification push: whoever holds the new secret has
    to prove they are the phone that receives pushes for this token (a
    reinstall, or a phone that changed hands). Its existing subscriptions
    keep running, the phone still gets its pushes; only managing them is
    locked until the new code is posted back."""
    row = conn.execute(
        "INSERT INTO push_devices (platform, token, secret_hash, language, "
        "verify_requested_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT (platform, token) DO UPDATE SET "
        "secret_hash=excluded.secret_hash, language=excluded.language, "
        "retired_at=NULL, retire_reason=NULL, dead_since=NULL, "
        "last_seen_at=CURRENT_TIMESTAMP, verified_at=NULL, "
        "verify_requested_at=CURRENT_TIMESTAMP, verify_sent_at=NULL, "
        "verify_code_hash=NULL "
        "RETURNING id",
        (platform, token, secret_hash, language),
    ).fetchone()
    return row[0]


# A verification code is valid this long after it was requested.
VERIFY_WINDOW = "-1 day"


def devices_awaiting_verification(conn: sqlite3.Connection, *,
                                  device_ids: list[int] | None = None
                                  ) -> list[sqlite3.Row]:
    """Devices whose verification push was requested in the last 24 hours and
    has not been delivered yet (`id`, `language`)."""
    sql = ("SELECT id, language FROM push_devices WHERE verified_at IS NULL "
           "AND retired_at IS NULL AND verify_sent_at IS NULL "
           "AND verify_requested_at > datetime('now', ?)")
    params: list = [VERIFY_WINDOW]
    if device_ids is not None:
        if not device_ids:
            return []
        sql += f" AND id IN ({','.join('?' * len(device_ids))})"
        params += list(device_ids)
    return conn.execute(sql + " ORDER BY id", params).fetchall()


def set_verify_code(conn: sqlite3.Connection, device_id: int,
                    code_hash: str) -> None:
    conn.execute("UPDATE push_devices SET verify_code_hash=? WHERE id=?",
                 (code_hash, device_id))


def mark_verification_sent(conn: sqlite3.Connection,
                           device_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE push_devices SET verify_sent_at=CURRENT_TIMESTAMP WHERE id=?",
        [(d,) for d in device_ids])


def request_verification(conn: sqlite3.Connection, device_id: int) -> None:
    """A resend: the old code stops working at once, the next sender pass
    makes a new one."""
    conn.execute(
        "UPDATE push_devices SET verify_requested_at=CURRENT_TIMESTAMP, "
        "verify_sent_at=NULL, verify_code_hash=NULL WHERE id=?", (device_id,))


def resend_wait_seconds(conn: sqlite3.Connection, device_id: int) -> int:
    """Seconds until a verification resend is allowed again (0 = now): one a
    minute per device, read from the database so it holds across workers."""
    row = conn.execute(
        "SELECT 60 - (strftime('%s','now') - strftime('%s', "
        "verify_requested_at)) AS wait FROM push_devices WHERE id=?",
        (device_id,)).fetchone()
    return max(0, int(row["wait"])) if row and row["wait"] is not None else 0


def verify_device(conn: sqlite3.Connection, device_id: int, code: str) -> bool:
    """True and the device is verified when `code` is the one last pushed to
    it and was requested within 24 hours; False otherwise. The code is
    compared by hash, in constant time, and cleared once used."""
    import hmac
    from app.api import _hash
    row = conn.execute(
        "SELECT verify_code_hash, verify_requested_at > datetime('now', ?) "
        "AS fresh FROM push_devices WHERE id=?",
        (VERIFY_WINDOW, device_id)).fetchone()
    if row is None or not row["verify_code_hash"] or not row["fresh"]:
        return False
    if not hmac.compare_digest(row["verify_code_hash"], _hash(code)):
        return False
    conn.execute("UPDATE push_devices SET verified_at=CURRENT_TIMESTAMP, "
                 "verify_code_hash=NULL WHERE id=?", (device_id,))
    return True


def live_devices(conn: sqlite3.Connection,
                 device_ids: list[int]) -> dict[int, tuple[str, str]]:
    """{device_id: (platform, token)} for the ids that are not retired."""
    if not device_ids:
        return {}
    marks = ",".join("?" * len(device_ids))
    rows = conn.execute(
        f"SELECT id, platform, token FROM push_devices "
        f"WHERE id IN ({marks}) AND retired_at IS NULL",
        list(device_ids),
    ).fetchall()
    return {r["id"]: (r["platform"], r["token"]) for r in rows}


def retire_device(conn: sqlite3.Connection, device_id: int, reason: str) -> None:
    """The relay says the token is dead (APNs 410, FCM UNREGISTERED). The
    device's subscriptions end with it: nothing could reach them, and a
    reinstall registers afresh."""
    conn.execute(
        "UPDATE push_devices SET retired_at=CURRENT_TIMESTAMP, retire_reason=? "
        "WHERE id=? AND retired_at IS NULL",
        (reason, device_id),
    )
    conn.execute(
        "UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP "
        "WHERE device_id=? AND deleted_at IS NULL",
        (device_id,),
    )


def device_by_id(conn: sqlite3.Connection, device_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM push_devices WHERE id=?",
                        (device_id,)).fetchone()


def touch_device(conn: sqlite3.Connection, device_id: int) -> None:
    """The app was heard from: the device's 30-day purge clock restarts (see
    housekeeping._prune_push_devices). At most one write per hour per device,
    so a chatty app does not cost an fsync per API call."""
    conn.execute(
        "UPDATE push_devices SET last_seen_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND last_seen_at < datetime('now', '-1 hour')",
        (device_id,),
    )


def update_device(conn: sqlite3.Connection, device_id: int, *,
                  token: str | None = None, language: str | None = None) -> bool:
    """A rotated push token or a changed language. Returns False when
    another live row already holds that token on the same platform: that
    is a registration this install made itself (a token names one
    install), and the app re-registers rather than this call guessing
    which row to keep."""
    row = device_by_id(conn, device_id)
    if row is None:
        return False
    new_token = token if token is not None else row["token"]
    new_lang = language if language is not None else row["language"]
    if new_token != row["token"]:
        clash = conn.execute(
            "SELECT id FROM push_devices WHERE platform=? AND token=? AND id!=?",
            (row["platform"], new_token, device_id)).fetchone()
        if clash is not None:
            return False
    conn.execute(
        "UPDATE push_devices SET token=?, language=?, "
        "dead_since=CASE WHEN token=? THEN dead_since ELSE NULL END, "
        "last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
        (new_token, new_lang, new_token, device_id),
    )
    return True


def delete_device(conn: sqlite3.Connection, device_id: int) -> None:
    """"Delete my data" from the app: the device row and, by cascade, every
    subscription it holds, seen_slots and digest_deliveries included."""
    conn.execute("DELETE FROM push_devices WHERE id=?", (device_id,))


def subscriptions_for_device(conn: sqlite3.Connection,
                             device_id: int) -> list[Subscription]:
    """Every live (not deleted) subscription of a device, expired ones
    included: an expired one is paused and renewable, and the app shows it
    as such."""
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE device_id=? AND deleted_at IS NULL "
        "ORDER BY id", (device_id,)).fetchall()
    return [_row_to_subscription(r) for r in rows]


def live_subscription_count(conn: sqlite3.Connection, device_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE device_id=? "
        "AND deleted_at IS NULL", (device_id,)).fetchone()[0]


def renew_subscription(conn: sqlite3.Connection, sub_id: int,
                       ttl_days: int) -> bool:
    """Start a new term from now. reminder_sent_at is the once-per-term latch
    of the still-looking check-in; cleared here so the next term asks again
    instead of expiring silently. Returns False when there is nothing to
    renew (deleted or purged)."""
    cur = conn.execute(
        "UPDATE subscriptions SET expires_at=datetime('now', ?), "
        "reminder_sent_at=NULL WHERE id=? AND deleted_at IS NULL",
        (f"+{int(ttl_days)} days", sub_id),
    )
    return (cur.rowcount or 0) == 1


def set_special_consent(conn: sqlite3.Connection, sub_id: int,
                        given: bool) -> None:
    """Record (or clear) the Art. 9 consent on an existing subscription.

    Cleared when someone edits their filter back to an ordinary service: the
    consent covered that one selection, so keeping the stamp would overstate
    what they agreed to.
    """
    conn.execute(
        "UPDATE subscriptions SET consent_special_at=? WHERE id=?",
        (sql_ts(datetime.utcnow()) if given else None, sub_id),
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
        device_id=(row["device_id"] if "device_id" in row.keys() else None),
        consent_special=("consent_special_at" in row.keys()
                         and row["consent_special_at"] is not None),
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

def record_seen_slot(conn: sqlite3.Connection, sub_id: int, slot_hash: str,
                     best_time: str | None = None) -> None:
    """Record that `sub_id` was told about `slot_hash`.

    `best_time` is the slot's HH:MM under a key coarser than the slot (see
    models.SeenKey). A row that already exists is only touched when the new
    time is strictly earlier than the one it holds: the row then remembers the
    better time, and its sent_at moves to now because a genuinely better slot
    was just delivered. A same-or-later time, or no time at all, leaves the
    row alone — including a row whose best_time is NULL, which means "told at
    an unknown time" and must keep suppressing the whole day.
    """
    conn.execute(
        "INSERT INTO seen_slots (subscription_id, slot_hash, best_time) "
        "VALUES (?,?,?) "
        "ON CONFLICT (subscription_id, slot_hash) DO UPDATE SET "
        "  best_time=excluded.best_time, sent_at=CURRENT_TIMESTAMP "
        "WHERE excluded.best_time < seen_slots.best_time",
        (sub_id, slot_hash, best_time),
    )

def has_seen_slot(conn: sqlite3.Connection, sub_id: int, slot_hash: str,
                  at: str | None = None) -> bool:
    """Has `sub_id` already been told about `slot_hash` — or, with `at` (the
    slot's HH:MM under a day key), about this key at `at` or an earlier time?

    A slot strictly earlier than the recorded best_time is news: the earliest
    slot only moves *back* when a cancellation opens a better one. Same or
    later is the same inventory seen again. A row without a best_time was told
    at an unknown time (a per-slot key, or a day key from before the column
    existed) and counts as seen whatever `at` is.
    """
    row = conn.execute(
        "SELECT best_time FROM seen_slots WHERE subscription_id=? AND slot_hash=?",
        (sub_id, slot_hash),
    ).fetchone()
    if row is None:
        return False
    best = row[0]
    if at is None or best is None:
        return True
    return at >= best

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
