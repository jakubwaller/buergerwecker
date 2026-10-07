from __future__ import annotations
import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import NamedTuple
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
    """Create or revive the row for a push token, keeping its subscriptions.
    It loses any retirement and any remembered dead answer (the evidence
    clock starts over).

    A registration of a known token only ever yields a pending secret, on a
    verified row and on a never-verified one alike: the existing main secret
    keeps its access (verified or locked), language and subscriptions stay,
    and the new secret has only `GET /device` and the verify routes for 24
    hours. The main secret changes hands only through a posted code: when the
    pending holder posts the code pushed to the token it replaces the old
    secret, which dies at that moment; when the main holder posts it, any
    pending secret is dropped. Whoever holds the new secret must prove
    possession of the phone.

    A repeat registration never refuses: it stamps a new verification request
    and the sender's rules decide whether a push goes out now, within a
    minute by the poller's sweep, or later. Those rules (see
    `token_verify_wait`) are one attempt a minute per token, and a daily
    budget per token for requests that need no credential (registrations,
    MAX_VERIFY_PUSHES_PER_DAY) kept apart from the one the device's own main
    credential spends (MAX_OWNER_VERIFY_PUSHES_PER_DAY), counted in a record
    that outlives the row: knowing a phone's token must not be enough to make
    it buzz on demand, nor to lock the real app out. A code already delivered
    stays valid until a new one replaces it."""
    existing = conn.execute(
        "SELECT id FROM push_devices WHERE platform=? AND token=?",
        (platform, token)).fetchone()
    if existing is None:
        return conn.execute(
            "INSERT INTO push_devices (platform, token, secret_hash, language, "
            "verify_requested_at, verify_kind) "
            "VALUES (?,?,?,?,CURRENT_TIMESTAMP,'open') "
            "RETURNING id", (platform, token, secret_hash, language)).fetchone()[0]
    # The main secret is never touched, verified row or not: it changes hands
    # only through a posted code. A row can be unverified while it holds a real
    # user's subscriptions (a token change pauses them), and rotating its
    # secret to whoever knows the token would hand them DELETE /device and
    # the subscriptions.
    conn.execute(
        "UPDATE push_devices SET pending_secret_hash=?, "
        "pending_since=CURRENT_TIMESTAMP, retired_at=NULL, retire_reason=NULL, "
        "dead_since=NULL WHERE id=?", (secret_hash, existing["id"]))
    request_verification(conn, existing["id"], code="keep", kind="open")
    return existing["id"]


# A verification code is valid this long after it was requested.
VERIFY_WINDOW = "-1 day"
# Verification pushes one token is sent per rolling 24 hours on requests that
# need no credential (a registration, the same token again included, and a
# pending credential's resend), whoever asks.
MAX_VERIFY_PUSHES_PER_DAY = 5
# The same for requests the device's own main credential makes (a token
# change, a resend): a budget of its own, so a stranger who re-registers the
# token cannot spend it and lock the real app out of its next token change.
MAX_OWNER_VERIFY_PUSHES_PER_DAY = 5
# Refused attempts, with evidence that the platform works, after which the
# sweep gives up on a request.
MAX_VERIFY_FAILURES = 3
# Every attempt the relay answered for one token per rolling day, whatever the
# evidence and whoever asked: the hard ceiling on what anyone can make us send
# to one token. Above the two budgets together, so it never binds a token the
# relay delivers to; it binds the refusals that count toward no budget, those
# without evidence that the platform works, which an attacker can produce at
# will by keeping every answer a junk token's first.
MAX_VERIFY_ATTEMPTS_PER_TOKEN_PER_DAY = 20
# Refusals, with evidence or without, after which an unverified device with
# no subscription is retired: it has nothing to lose, the app re-registers on
# its 410, and a junk registration stops costing the sweep anything.
MAX_UNCONFIRMED_REFUSALS = 3


def token_key(platform: str, token: str) -> str:
    """What the verification record keys a token by: it must outlive the
    device row, and it need not be the token itself."""
    return hashlib.sha256(f"{platform}|{token}".encode("utf-8")).hexdigest()


def token_verify_wait(conn: sqlite3.Connection, key: str, kind: str,
                      config: str | None) -> int:
    """Seconds until the token `key` may be sent another verification push on
    a request of `kind` (0 = now), from `verify_attempts`, so the rules hold
    across workers, the poller, and a deleted and re-created row:

    - 60 s after the last attempt the relay answered, of either kind;
    - the kind's daily budget, counting deliveries and refusals with evidence
      that the platform works (`credited`);
    - MAX_VERIFY_ATTEMPTS_PER_TOKEN_PER_DAY, counting every answered attempt,
      except a refusal without evidence made under other platform settings
      than the current `config` (push.config_fingerprint; None counts them
      all). Fixing a misconfiguration changes the settings and so forgives
      what it refused; nothing a client does can. (Forgiving once the
      platform delivered to anyone let any delivery reset the count: the
      attacker's own phone, or other users' digests.)"""
    rows = conn.execute(
        "SELECT kind, credited, config, "
        "CAST(strftime('%s','now') - strftime('%s', at) AS INTEGER) AS age "
        "FROM verify_attempts WHERE token_key=? "
        "AND at > datetime('now','-1 day') ORDER BY at DESC", (key,)).fetchall()
    wait = 0
    if rows:
        wait = max(wait, 60 - rows[0]["age"])
    cap = (MAX_OWNER_VERIFY_PUSHES_PER_DAY if kind == "owner"
           else MAX_VERIFY_PUSHES_PER_DAY)

    def frees(ages: list[int], limit: int) -> int:
        # The window frees when the attempt that made the count reach the
        # limit ages out.
        return max(1, 86400 - ages[limit - 1]) if len(ages) >= limit else 0
    wait = max(wait, frees([r["age"] for r in rows
                            if r["credited"] and r["kind"] == kind], cap))
    counted = [r["age"] for r in rows
               if r["credited"] or config is None or r["config"] == config]
    wait = max(wait, frees(counted, MAX_VERIFY_ATTEMPTS_PER_TOKEN_PER_DAY))
    return max(0, wait)


def device_verify_wait(conn: sqlite3.Connection, device_id: int) -> int:
    """Seconds until the device row may be sent another verification push:
    60 s after its last delivered one, whichever token that went to (the
    delivered `verify|<device_id>|<minute>` idempotency rows). A token change
    right after a push waits out the minute instead of buzzing at once."""
    prefix = f"verify|{int(device_id)}|"
    age = conn.execute(
        "SELECT MIN(CAST(strftime('%s','now') - strftime('%s', sent_at) AS INTEGER)) "
        "FROM sent_idempotency WHERE idem_key >= ? AND idem_key < ? "
        "AND provider != 'pending' AND sent_at > datetime('now','-2 minutes')",
        (prefix, prefix[:-1] + "}")).fetchone()[0]
    return max(0, 60 - age) if age is not None else 0


def verify_push_wait(conn: sqlite3.Connection, device_id: int, *,
                     kind: str | None = None, config: str | None = None) -> int:
    """Seconds until the device may be sent a verification push on a request
    of `kind` (default: the outstanding request's): the device's own minute
    and its current token's budget and ceiling (`token_verify_wait`, under
    the platform settings `config`), whichever is later."""
    row = conn.execute("SELECT platform, token, verify_kind FROM push_devices "
                       "WHERE id=?", (device_id,)).fetchone()
    if row is None:
        return 0
    return max(device_verify_wait(conn, device_id),
               token_verify_wait(conn, token_key(row["platform"], row["token"]),
                                 kind or row["verify_kind"] or "open", config))


def record_verify_attempts(conn: sqlite3.Connection,
                           attempts: list[tuple[str, str, str, bool]]) -> None:
    """One row per (token_key, kind, config, credited) the relay answered:
    credited for a delivery or a refusal the platform's evidence stands
    behind (see push.send_verifications); `config` the platform settings it
    was made under (push.config_fingerprint)."""
    conn.executemany("INSERT INTO verify_attempts (token_key, kind, config, credited) "
                     "VALUES (?,?,?,?)",
                     [(k, kind, p, 1 if c else 0) for k, kind, p, c in attempts])


def devices_awaiting_verification(conn: sqlite3.Connection, *,
                                  device_ids: list[int] | None = None,
                                  platforms: list[str] | None = None,
                                  min_age_seconds: int = 0,
                                  limit: int | None = None
                                  ) -> list[sqlite3.Row]:
    """Devices (never verified, or a verified one with a pending secret to
    prove) whose verification push was requested in the last 24 hours (and
    at least `min_age_seconds` ago), has not been delivered yet, has not been
    refused MAX_VERIFY_FAILURES times and is not waiting out a not-before
    (`id`, `language`, `platform`, `token`, `verify_kind`), on one of
    `platforms` if given. Least recently tried first (a device the sender
    tried and did not deliver to waits a minute), then oldest request, at
    most `limit`: a sweep rotates through a long queue instead of retrying
    its head."""
    sql = ("SELECT id, language, platform, token, verify_kind FROM push_devices "
           "WHERE (verified_at IS NULL OR pending_secret_hash IS NOT NULL) "
           "AND retired_at IS NULL AND verify_sent_at IS NULL "
           "AND verify_requested_at > datetime('now', ?) "
           "AND verify_failures < ? "
           "AND (verify_next_at IS NULL OR verify_next_at <= CURRENT_TIMESTAMP)")
    params: list = [VERIFY_WINDOW, MAX_VERIFY_FAILURES]
    if platforms is not None:
        if not platforms:
            return []
        sql += f" AND platform IN ({','.join('?' * len(platforms))})"
        params += list(platforms)
    if min_age_seconds:
        sql += " AND verify_requested_at <= datetime('now', ?)"
        params.append(f"-{int(min_age_seconds)} seconds")
    if device_ids is not None:
        if not device_ids:
            return []
        sql += f" AND id IN ({','.join('?' * len(device_ids))})"
        params += list(device_ids)
    sql += " ORDER BY verify_next_at, verify_requested_at, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def defer_verification(conn: sqlite3.Connection, device_id: int,
                       seconds: int) -> None:
    """The budget says wait, or the last try did not deliver: the sweep
    leaves the device alone until then and moves on to the rest."""
    conn.execute("UPDATE push_devices SET verify_next_at=datetime('now', ?) "
                 "WHERE id=?", (f"+{int(seconds)} seconds", device_id))


def record_verify_refusals(conn: sqlite3.Connection, device_ids: list[int], *,
                           confirmed: bool) -> None:
    """The relay refused these devices' verification push. Every refusal
    counts toward `verify_tries` and sends the device to the back of the
    sweep's queue, a minute after the first and twice as long after each
    further one, up to an hour; only a `confirmed` one (the platform showed it
    works) counts toward MAX_VERIFY_FAILURES."""
    conn.executemany(
        "UPDATE push_devices SET verify_failures=verify_failures+?, "
        "verify_next_at=datetime('now', '+' || "
        "  min(3600, 60 << min(verify_tries, 6)) || ' seconds'), "
        "verify_tries=verify_tries+1 WHERE id=?",
        [(1 if confirmed else 0, d) for d in device_ids])


def retire_unconfirmed(conn: sqlite3.Connection,
                       refused: list[tuple[int, str]]) -> list[int]:
    """Retire each (device_id, token) whose token has been refused
    MAX_UNCONFIRMED_REFUSALS times since it last delivered or changed
    (`verify_tries`, which re-registering and resending do not reset) and
    that is unverified and holds no subscription: without evidence the
    platform works it is never retired as dead, and a junk registration would
    otherwise be retried for its whole day. Nothing is lost: the app's next
    call answers 410 and it registers afresh, and a revived row that is
    refused again is retired again at once. The ids retired."""
    retired = []
    for device_id, token in refused:
        row = conn.execute(
            "SELECT 1 FROM push_devices d WHERE d.id=? AND d.token=? "
            "AND d.verified_at IS NULL AND d.verify_tries >= ? "
            "AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.device_id=d.id "
            "                AND s.deleted_at IS NULL)",
            (device_id, token, MAX_UNCONFIRMED_REFUSALS)).fetchone()
        if row and retire_device(conn, device_id, "unconfirmed_refusals", token=token):
            retired.append(device_id)
    return retired


def set_verify_code(conn: sqlite3.Connection, device_id: int,
                    code_hash: str) -> bool:
    """Store the hash of a freshly made code, and say whether this sender
    owns it. The write is the claim: it succeeds only while the push is
    undelivered and the stored code is at least 60 seconds old (or absent), so
    of any number of senders (workers, the web request and the poller) exactly
    one per device per minute stores a code and sends it. False means another
    sender holds the current code, or the push was delivered meanwhile: skip
    the device and touch nothing."""
    return conn.execute(
        "UPDATE push_devices SET verify_code_hash=?, "
        "verify_code_at=CURRENT_TIMESTAMP WHERE id=? AND verify_sent_at IS NULL "
        "AND (verify_code_at IS NULL OR verify_code_at <= datetime('now','-60 seconds'))",
        (code_hash, device_id)).rowcount == 1


def mark_verification_sent(conn: sqlite3.Connection,
                           sent: list[tuple[int, str]]) -> None:
    """Stamp the delivery of each (device_id, code_hash), only while the row
    still holds that code: a resend or a token change that replaced it in the
    meantime has a push of its own to deliver, and stamping would stop the
    sweep from ever sending it. A delivery shows the token works: its refusal
    count (`verify_tries`) starts over."""
    conn.executemany(
        "UPDATE push_devices SET verify_sent_at=CURRENT_TIMESTAMP, verify_tries=0 "
        "WHERE id=? AND verify_code_hash=?", sent)


def request_verification(conn: sqlite3.Connection, device_id: int, *,
                         kind: str, code: str = "drop") -> None:
    """Stamp a new verification request: `verify_requested_at` restarts (so
    the poller's sweep grace always covers the web request's own send),
    `verify_sent_at`, the failure count and the not-before clear, whatever
    the sender's rules then decide. `kind` is whose request it is ("owner",
    the device's main credential; "open", anyone else) and picks the budget
    the push is counted against; an owner request still waiting for its push
    stays one, since the push that answers both is the same push. What
    happens to the stored code and its stamp depends on the path:

    - "drop" (a token change): cleared unconditionally; a code made for the
      old token is useless and must stop working.
    - "drop_if_stale" (a resend): cleared only when the code is absent or at
      least 60 seconds old. A younger one is either in flight (the sender that
      stored it will deliver it) or was just delivered, and clearing it would
      let the resend bypass the claim: the in-flight sender would deliver a
      code the database no longer holds.
    - "keep" (a re-registration): kept; the code was delivered to the same
      token and stays valid until a new one replaces it, and the next send
      waits until it is a minute old, as the one-a-minute rule wants."""
    if code == "drop":
        clear = "verify_code_hash=NULL, verify_code_at=NULL "
    elif code == "drop_if_stale":
        stale = "(verify_code_at IS NULL OR verify_code_at <= datetime('now','-60 seconds'))"
        clear = (f"verify_code_hash=CASE WHEN {stale} THEN NULL ELSE verify_code_hash END, "
                 f"verify_code_at=CASE WHEN {stale} THEN NULL ELSE verify_code_at END ")
    else:
        clear = "verify_code_hash=verify_code_hash "
    # Every expression reads the row as it was, so verify_sent_at here is the
    # old value: an owner request is outstanding while its push has not gone
    # out. An open request joining it changes nothing about it, its failure
    # count and not-before included, or re-registering would reset them.
    joins_owner = ("(:kind='open' AND verify_kind='owner' "
                   "AND verify_sent_at IS NULL)")
    # verify_tries is not touched: it counts refusals of the token, not of the
    # request, and only a delivery or a token change starts it over. Reset by
    # every re-registration, it kept a junk row out of retire_unconfirmed.
    conn.execute(
        "UPDATE push_devices SET verify_requested_at=CURRENT_TIMESTAMP, "
        f"verify_kind=CASE WHEN {joins_owner} THEN 'owner' ELSE :kind END, "
        f"verify_failures=CASE WHEN {joins_owner} THEN verify_failures ELSE 0 END, "
        f"verify_next_at=CASE WHEN {joins_owner} THEN verify_next_at ELSE NULL END, "
        f"verify_sent_at=NULL, {clear}WHERE id=:id",
        {"kind": kind, "id": device_id})


def pending_secret_fresh(conn: sqlite3.Connection, device_id: int) -> bool:
    """A pending secret is usable for 24 hours from when it was stored
    (`pending_since`, which a resend or re-registration does not move)."""
    row = conn.execute(
        "SELECT pending_since > datetime('now', ?) AS fresh "
        "FROM push_devices WHERE id=?", (VERIFY_WINDOW, device_id)).fetchone()
    return bool(row and row["fresh"])


def verify_device(conn: sqlite3.Connection, device_id: int, code: str, *,
                  pending_hash: str | None = None) -> bool:
    """True when `code` is the one last pushed to the device and was
    requested within 24 hours; False otherwise. The code is compared by hash,
    in constant time, and cleared once used. A caller holding the pending
    credential passes the hash it authenticated with as `pending_hash`, and
    exactly that secret is promoted (it becomes the secret, the old one dies);
    if a re-registration replaced it in the meantime nothing is promoted and
    the answer is False, so the real phone's code never promotes a stranger's
    secret. A caller holding the main credential (`pending_hash` None)
    verifies a never-verified row, and on any row drops the pending secret,
    its stamp and the code: the phone answered its owner, so whoever
    re-registered the token loses the attempt. That is all the old, already
    verified credential can do with a code; it never promotes a new
    install's secret."""
    import hmac
    from app.api import _hash
    row = conn.execute(
        "SELECT verify_code_hash, pending_secret_hash, "
        "verify_code_at > datetime('now', ?) AS fresh "
        "FROM push_devices WHERE id=?", (VERIFY_WINDOW, device_id)).fetchone()
    if row is None or not row["verify_code_hash"] or not row["fresh"]:
        return False
    if not hmac.compare_digest(row["verify_code_hash"], _hash(code)):
        return False
    if pending_hash is not None:
        cur = conn.execute(
            "UPDATE push_devices SET secret_hash=pending_secret_hash, "
            "pending_secret_hash=NULL, pending_since=NULL, "
            "verified_at=CURRENT_TIMESTAMP, "
            "verify_code_hash=NULL WHERE id=? AND pending_secret_hash=?",
            (device_id, pending_hash))
        if cur.rowcount == 0:
            return False
    else:
        # The owner proved possession: a stranger's pending secret is dropped.
        conn.execute("UPDATE push_devices SET "
                     "verified_at=COALESCE(verified_at, CURRENT_TIMESTAMP), "
                     "verify_code_hash=NULL, pending_secret_hash=NULL, "
                     "pending_since=NULL WHERE id=?", (device_id,))
    return True


class LiveDevice(NamedTuple):
    platform: str
    token: str
    verified: bool
    # Holds a subscription that is not deleted (expired ones included).
    subscribed: bool


def live_devices(conn: sqlite3.Connection,
                 device_ids: list[int]) -> dict[int, LiveDevice]:
    """{device_id: LiveDevice} for the ids that are not retired."""
    if not device_ids:
        return {}
    marks = ",".join("?" * len(device_ids))
    rows = conn.execute(
        f"SELECT d.id, d.platform, d.token, d.verified_at IS NOT NULL AS verified, "
        f"EXISTS (SELECT 1 FROM subscriptions s WHERE s.device_id = d.id "
        f"        AND s.deleted_at IS NULL) AS subscribed "
        f"FROM push_devices d WHERE d.id IN ({marks}) AND d.retired_at IS NULL",
        list(device_ids),
    ).fetchall()
    return {r["id"]: LiveDevice(r["platform"], r["token"], bool(r["verified"]),
                                bool(r["subscribed"])) for r in rows}


def retire_device(conn: sqlite3.Connection, device_id: int, reason: str, *,
                  token: str) -> bool:
    """The relay says `token` is dead (APNs 410, FCM UNREGISTERED). The
    device's subscriptions end with it: nothing could reach them, and a
    reinstall registers afresh. Only while the row still holds that token: a
    dead answer for a token the device has since replaced says nothing about
    the device. True when this call retired it."""
    cur = conn.execute(
        "UPDATE push_devices SET retired_at=CURRENT_TIMESTAMP, retire_reason=? "
        "WHERE id=? AND token=? AND retired_at IS NULL",
        (reason, device_id, token),
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        "UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP "
        "WHERE device_id=? AND deleted_at IS NULL",
        (device_id,),
    )
    return True


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
                  secret_hash: str, token: str | None = None,
                  language: str | None = None) -> str:
    """A rotated push token or a changed language, for the holder of the main
    secret `secret_hash` (the one that authenticated the call). A new token
    un-verifies the device (the secret stays): it has to prove it receives
    pushes on the new token, on the owner's verification budget, and until
    then its subscriptions are paused (`active_subscriptions`). Without it a
    verified device could swap its token for junk, freeing the real one to
    register and verify again, and mint verified devices at will. A code
    already pushed to the old token is invalidated. A language-only change
    leaves verification alone.

    Returns "ok"; "unauthorized" when the row no longer has that secret (a
    pending one was promoted in between); or "token_in_use" when another row
    already holds that token on the same platform: that is a registration
    this install made itself (a token names one install), and the app
    re-registers rather than this call guessing which row to keep."""
    row = conn.execute("SELECT * FROM push_devices WHERE id=? AND secret_hash=?",
                       (device_id, secret_hash)).fetchone()
    if row is None:
        return "unauthorized"
    new_token = token if token is not None else row["token"]
    new_lang = language if language is not None else row["language"]
    if new_token != row["token"]:
        clash = conn.execute(
            "SELECT id FROM push_devices WHERE platform=? AND token=? AND id!=?",
            (row["platform"], new_token, device_id)).fetchone()
        if clash is not None:
            return "token_in_use"
    cur = conn.execute(
        "UPDATE push_devices SET token=?, language=?, "
        "dead_since=CASE WHEN token=? THEN dead_since ELSE NULL END, "
        "last_seen_at=CURRENT_TIMESTAMP WHERE id=? AND secret_hash=?",
        (new_token, new_lang, new_token, device_id, secret_hash),
    )
    if cur.rowcount != 1:
        return "unauthorized"
    if new_token != row["token"]:
        conn.execute(
            "UPDATE push_devices SET verified_at=NULL, pending_secret_hash=NULL, "
            "verify_code_hash=NULL, verify_sent_at=NULL, verify_tries=0 "
            "WHERE id=?", (device_id,))
        request_verification(conn, device_id, kind="owner")
    return "ok"


def delete_device(conn: sqlite3.Connection, device_id: int,
                  secret_hash: str) -> bool:
    """"Delete my data" from the app: the device row and, by cascade, every
    subscription it holds, seen_slots and digest_deliveries included. Only
    for the holder of the main secret `secret_hash`; False when the row no
    longer has it (a pending secret was promoted in between)."""
    return conn.execute("DELETE FROM push_devices WHERE id=? AND secret_hash=?",
                        (device_id, secret_hash)).rowcount == 1


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


# What "live" means for the city-wide counts below. A mail subscription is
# live once confirmed, until deleted or its term ends: what the poller polls.
# An app subscription is live until deleted or its term ends *whether or not
# its device is verified right now*. A paused one (a device that changed its
# token and has not posted the new code yet) does not poll, but it resumes
# the moment the device verifies, without passing any check again; leaving
# it out would let one device pause its subscriptions, make room for
# another's, and verify again. An expired one does not count: it can only
# come back through /renew, which judges it again.
_LIVE_MAIL = ("device_id IS NULL AND confirmed_at IS NOT NULL "
              "AND deleted_at IS NULL AND expires_at > CURRENT_TIMESTAMP")
_LIVE_APP = ("device_id IS NOT NULL "
             "AND deleted_at IS NULL AND expires_at > CURRENT_TIMESTAMP")


def _types_in(rows) -> set[str]:
    out: set[str] = set()
    for r in rows:
        out.update(Filter.from_json(r["filters_json"]).appointment_types)
    return out


def city_services(conn: sqlite3.Connection, city: str, *,
                  exclude_id: int | None = None) -> tuple[set[str], set[str]]:
    """(services a live mail subscription watches, services a live app
    subscription watches) in `city`, leaving out subscription `exclude_id`
    (the one being edited or renewed). The plan cap's inputs, see
    `app.planning.cap_refuses`."""
    skip = -1 if exclude_id is None else exclude_id
    mail = conn.execute(
        f"SELECT filters_json FROM subscriptions WHERE city=? AND id<>? "
        f"AND {_LIVE_MAIL}", (city, skip)).fetchall()
    app = conn.execute(
        f"SELECT filters_json FROM subscriptions WHERE city=? AND id<>? "
        f"AND {_LIVE_APP}", (city, skip)).fetchall()
    return _types_in(mail), _types_in(app)


def own_live_services(conn: sqlite3.Connection, sub_id: int | None) -> set[str]:
    """The services subscription `sub_id` watches while it is live (mail or
    app), else none: what an edit or a renewal of it already has polled, so
    the plan cap does not judge it as new. An expired one has nothing
    polled; renewing it is judged like a sign-up."""
    if sub_id is None:
        return set()
    return _types_in(conn.execute(
        f"SELECT filters_json FROM subscriptions WHERE id=? "
        f"AND (({_LIVE_MAIL}) OR ({_LIVE_APP}))", (sub_id,)).fetchall())


def app_subscriptions_in_city(conn: sqlite3.Connection, city: str, *,
                              exclude_id: int | None = None) -> int:
    """Live app subscriptions in `city`, every device together, leaving out
    `exclude_id`: what MAX_APP_SUBSCRIPTIONS_PER_CITY caps."""
    return conn.execute(
        f"SELECT COUNT(*) FROM subscriptions WHERE city=? AND id<>? "
        f"AND {_LIVE_APP}",
        (city, -1 if exclude_id is None else exclude_id)).fetchone()[0]


def device_footprint_in_city(conn: sqlite3.Connection, device_id: int,
                             city: str, *, exclude_id: int | None = None
                             ) -> tuple[int, set[str]]:
    """(subscriptions, distinct services) one device holds in `city`,
    leaving out `exclude_id`. Counted over the same rows as
    MAX_SUBSCRIPTIONS_PER_DEVICE (not deleted, an expired one included: it
    is renewable)."""
    rows = conn.execute(
        "SELECT filters_json FROM subscriptions WHERE device_id=? AND city=? "
        "AND id<>? AND deleted_at IS NULL",
        (device_id, city, -1 if exclude_id is None else exclude_id)).fetchall()
    return len(rows), _types_in(rows)


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
        push_token=(row["push_token"] if "push_token" in row.keys() else None),
    )

def active_subscriptions(conn: sqlite3.Connection) -> list[Subscription]:
    """What the poll cycle serves and the plan cap counts. A push
    subscription of a device that has not proven its token (never verified,
    or changed its token and not yet verified again) is not running: it
    neither polls nor takes a plan slot. A legitimate token rotation pauses
    its subscriptions for the seconds until the new token verifies. A push
    subscription carries the verified token it is served on (`push_token`)."""
    rows = conn.execute(
        "SELECT s.*, d.token AS push_token FROM subscriptions s "
        "LEFT JOIN push_devices d ON d.id = s.device_id "
        "WHERE s.confirmed_at IS NOT NULL "
        "AND s.deleted_at IS NULL "
        "AND s.expires_at > CURRENT_TIMESTAMP "
        "AND (s.device_id IS NULL OR d.verified_at IS NOT NULL) "
        "ORDER BY s.id"
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
