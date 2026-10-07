from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 16

# SQLite's own timestamp shape, the one CURRENT_TIMESTAMP and datetime('now')
# produce. Queries compare stored timestamps against those as plain text, so a
# value written from Python has to match it: isoformat()'s "T" sorts after the
# space, and a subscription due to expire at 05:21 stayed active until the date
# rolled over at midnight UTC.
SQL_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Columns Python used to fill with isoformat(); init_schema rewrites any
# leftover "T" values in place.
_PY_WRITTEN_TIMESTAMPS = (
    ("subscriptions", "expires_at"),
    ("subscriptions", "consent_special_at"),
    ("city_state", "zero_match_since"),
    ("city_state", "last_canary_alert_at"),
    ("city_state", "last_polled_at"),
    ("availability_samples", "sampled_at"),
)


def sql_ts(dt: datetime) -> str:
    """`dt` (naive UTC) in SQLite's timestamp shape — see SQL_TS_FORMAT."""
    return dt.strftime(SQL_TS_FORMAT)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  email             TEXT NOT NULL,
  city              TEXT NOT NULL DEFAULT 'leipzig',
  language          TEXT NOT NULL DEFAULT 'de',
  filters_json      TEXT NOT NULL,
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  confirmed_at      TIMESTAMP,
  last_notified_at  TIMESTAMP,
  expires_at        TIMESTAMP NOT NULL,
  reminder_sent_at  TIMESTAMP,
  heartbeat_30d_at  TIMESTAMP,
  heartbeat_60d_at  TIMESTAMP,
  deleted_at        TIMESTAMP,
  confirmation_sent_at TIMESTAMP,
  last_match_count  INTEGER,
  consecutive_digests INTEGER NOT NULL DEFAULT 0,
  consent_special_at TIMESTAMP,
  -- Set on an app subscription: digests go to this push device instead of
  -- an address, and `email` is '' (never NULL — the NOT NULL predates the
  -- app, and a sentinel keeps every `WHERE email=?` lookup from matching a
  -- push row). Deleting the device takes its subscriptions with it, which is
  -- what "delete my data" from the app means.
  device_id         INTEGER REFERENCES push_devices(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_active_subs
  ON subscriptions(deleted_at, confirmed_at, expires_at, city);

-- One row per app install that registered for push. `token` is the APNs
-- device token or the FCM registration token, opaque to us and to anyone
-- reading a backup: it addresses a device through Apple's or Google's relay
-- and names nobody. `secret_hash` is the SHA-256 of the per-device secret the
-- app authenticates its API calls with; the secret itself is only ever shown
-- once, at registration. A device is retired (not deleted) when the relay
-- reports the token dead (APNs 410, FCM UNREGISTERED) and the platform is
-- known to be working, so the API can tell the app to re-register;
-- housekeeping purges retired rows after 30 days.
CREATE TABLE IF NOT EXISTS push_devices (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  platform      TEXT NOT NULL,
  token         TEXT NOT NULL,
  secret_hash   TEXT NOT NULL,
  language      TEXT NOT NULL DEFAULT 'de',
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  retired_at    TIMESTAMP,
  retire_reason TEXT,
  -- First dead-token answer the relay gave for this token, cleared by any
  -- delivery. A device is retired only once the platform has delivered to
  -- someone after this moment (see push.send_push_batch).
  dead_since    TIMESTAMP,
  -- Verification by push (see app/api.py): a device may not manage
  -- subscriptions until it has posted back the code we pushed to its token.
  -- Only the SHA-256 of the code is stored, written when the push is sent.
  verified_at          TIMESTAMP,
  verify_code_hash     TEXT,
  verify_requested_at  TIMESTAMP,
  verify_sent_at       TIMESTAMP,
  -- Secret of a re-registration of a verified token, usable only until the
  -- new install verifies (see repo.register_device).
  pending_secret_hash  TEXT,
  -- When the pending secret was stored, and when the current code was made:
  -- the 24-hour lives of both run from these, not from verify_requested_at,
  -- which every resend and re-registration re-stamps.
  pending_since        TIMESTAMP,
  verify_code_at       TIMESTAMP,
  -- Whose request the outstanding verification push answers: 'owner' (the
  -- device's own main credential: a token change or a resend) or 'open' (a
  -- registration, which needs no credential). Each has its own daily budget
  -- per token (verify_attempts), so a stranger cannot spend the owner's.
  verify_kind          TEXT,
  -- Attempts the relay refused for the outstanding request; the sweep gives
  -- up on it at repo.MAX_VERIFY_FAILURES. A new request starts over.
  verify_failures      INTEGER NOT NULL DEFAULT 0,
  -- Not before: set after a refused attempt or when the budget says wait,
  -- so the sweep rotates through the queue instead of retrying its head.
  verify_next_at       TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_devices_token
  ON push_devices(platform, token);

-- Verification pushes attempted per push token, delivered or refused (a
-- platform-wide outage is not counted: it says nothing about the token).
-- `token_key` is the SHA-256 of "<platform>|<token>", so the count outlives
-- the device row: deleting a device and registering its token again starts no
-- new budget. `kind` is the request's (see push_devices.verify_kind). Read
-- over a rolling day by repo.token_verify_wait; housekeeping prunes rows older
-- than that.
CREATE TABLE IF NOT EXISTS verify_attempts (
  token_key  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_verify_attempts_key ON verify_attempts(token_key, at);

-- Rate-limit events that must hold across the web workers
-- (ratelimit.db_rate_hit): new devices per client network, slot-overview
-- reads per device. `bucket` names the limit and its subject; a client
-- network appears only as a keyed hash, never as an address. Housekeeping
-- prunes rows older than a day, the longest window.
CREATE TABLE IF NOT EXISTS rate_events (
  bucket  TEXT NOT NULL,
  at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rate_events_bucket ON rate_events(bucket, at);

CREATE TABLE IF NOT EXISTS seen_slots (
  subscription_id INTEGER NOT NULL,
  slot_hash       TEXT NOT NULL,
  sent_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- HH:MM of the earliest slot reported under a key coarser than the slot
  -- (a day key). NULL = the key names the slot exactly, or the row predates
  -- the column: either way "already told, whatever the time".
  best_time       TEXT,
  PRIMARY KEY (subscription_id, slot_hash),
  FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_seen_sent_at ON seen_slots(sent_at);

CREATE TABLE IF NOT EXISTS sent_idempotency (
  idem_key  TEXT PRIMARY KEY,
  provider  TEXT NOT NULL,
  sent_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_sent_idem_at ON sent_idempotency(sent_at);

CREATE TABLE IF NOT EXISTS email_send_counts (
  provider TEXT NOT NULL,
  day      TEXT NOT NULL,
  n        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, day)
);

-- Notifications a cycle could not send because the combined provider quota was
-- spent. Durable and per-UTC-day because the alert mail is rate-limited to once
-- per 24h: a per-cycle count in that mail cannot tell you whether the day lost
-- one digest or four hundred. This is the only record that someone was not told
-- about a slot, so it outlives sent_idempotency's 14-day prune.
CREATE TABLE IF NOT EXISTS email_deferral_counts (
  day TEXT PRIMARY KEY,
  n   INTEGER NOT NULL DEFAULT 0
);

-- One row per cycle that deferred, saying which wall it hit. The counter above
-- cannot tell a deferral against Mailjet's hourly warm-up cap — cleared by the
-- next cycle, nobody notices — from one against the combined daily pool, which
-- holds until the rolling 24h window frees a slot, by which time the
-- appointment is usually gone. `wall` is 'hourly', 'daily' or 'outage' (every
-- provider with room failed at the HTTP level); `frees_at` is when the
-- tightest-bound provider gets one slot back, i.e. the earliest a retry can
-- succeed. Pruned after 90 days; the per-day counter keeps the totals.
CREATE TABLE IF NOT EXISTS email_deferrals (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  n        INTEGER NOT NULL,
  wall     TEXT NOT NULL,
  frees_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_email_deferrals_at ON email_deferrals(at);

-- One row per delivered digest. subscriptions.last_notified_at only keeps the
-- latest, and the per-subscriber daily cap (MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY)
-- needs to count them over a rolling 24h window. Goes with the subscription;
-- housekeeping prunes after 7 days.
CREATE TABLE IF NOT EXISTS digest_deliveries (
  subscription_id INTEGER NOT NULL,
  sent_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_digest_deliveries_sub_at
  ON digest_deliveries(subscription_id, sent_at);

-- Subscribers the cap held back, one row per subscriber per UTC day. A held
-- digest is not queued: its slots stay unseen and go out once the window
-- frees. This is the measurement behind the cap's value, so it deliberately
-- has no FOREIGN KEY — a subscriber who churns the same day still counts.
-- Pruned after 90 days.
CREATE TABLE IF NOT EXISTS digest_cap_holds (
  day             TEXT NOT NULL,
  subscription_id INTEGER NOT NULL,
  PRIMARY KEY (day, subscription_id)
);

-- Per-address delivery failures. A provider that parses our request and still
-- rejects it (HTTP 400/422) is refusing the recipient, not failing itself; once
-- an address collects MAX_SEND_FAILURES_PER_ADDRESS of those we stop attempting
-- it, so one typo'd sign-up isn't retried every cycle forever. Cleared on any
-- successful delivery to that address, and by housekeeping once no subscription
-- carries the address any more — the row is a bare e-mail address, so it may not
-- outlive the subscription that justified storing it.
CREATE TABLE IF NOT EXISTS email_failures (
  email          TEXT PRIMARY KEY,
  failures       INTEGER NOT NULL DEFAULT 0,
  last_failed_at TIMESTAMP
);

-- Addresses the receiving mail systems have told us to stop mailing, learned
-- from provider webhooks rather than from an API rejection. This is the
-- asynchronous half of deliverability: a provider accepts a message with HTTP
-- 200 and only reports minutes later that the mailbox does not exist, or that
-- the recipient pressed "spam". Nothing in the send path can see either, so
-- without this table a dead or hostile address is mailed forever, which is how
-- a sending domain gets blocked.
--
-- `reason IS NULL` means the row is only counting soft bounces and the address
-- is still mailable; a non-NULL reason is a suppression and `mail._dead_addresses`
-- excludes the address before it costs an API call. Kept separate from
-- `email_failures` (synchronous 400/422 rejections) on purpose: a successful
-- send clears that counter, and it fires on API *acceptance*, which is exactly
-- what happens right before an asynchronous bounce arrives. Sharing one counter
-- would reset the evidence every cycle.
--
-- Retention splits by reason (housekeeping._prune_suppressions): bounce rows
-- die with the subscription that justified them, complaint rows run on their
-- own clock (COMPLAINT_RETENTION_DAYS, a year). A bounce claims a mailbox does
-- not exist *today* and goes stale, and a sign-up lifts it; a complaint is a
-- person saying we are spam, which needs a human and outlives the subscription.
CREATE TABLE IF NOT EXISTS email_suppressions (
  email         TEXT PRIMARY KEY,
  reason        TEXT,
  provider      TEXT,
  detail        TEXT,
  soft_bounces  INTEGER NOT NULL DEFAULT 0,
  suppressed_at TIMESTAMP,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_suppressed
  ON email_suppressions(reason) WHERE reason IS NOT NULL;

-- The last successful poll of each watched service, for the app's live
-- overview and widget (app/snapshots.py): the soonest slots as compact
-- JSON, the count before the cap, and when. One row per (city, service),
-- the union of the plans that cover it, replaced every cycle all of them
-- succeed; a failed poll leaves the previous row. Keyed by service rather
-- than by plan because a plan's key names its office set and changes with
-- every edit. Pruned after a day, so a service nobody watches any more does
-- not outlive the inventory it described. Read by
-- GET /api/v1/cities/<slug>/slots, never by the notification path.
CREATE TABLE IF NOT EXISTS slot_snapshots (
  city         TEXT NOT NULL,
  service_uuid TEXT NOT NULL,
  slots_json   TEXT NOT NULL,
  n_total      INTEGER NOT NULL,
  polled_at    TIMESTAMP NOT NULL,
  PRIMARY KEY (city, service_uuid)
);

CREATE TABLE IF NOT EXISTS meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS city_state (
  city                  TEXT PRIMARY KEY,
  zero_match_since      TIMESTAMP,
  last_canary_alert_at  TIMESTAMP,
  requests_today        INTEGER NOT NULL DEFAULT 0,
  last_polled_at        TIMESTAMP,
  polls_today           INTEGER NOT NULL DEFAULT 0,
  polls_total           INTEGER NOT NULL DEFAULT 0,
  requests_total        INTEGER NOT NULL DEFAULT 0,
  counts_date           TEXT
);

CREATE TABLE IF NOT EXISTS slots_cache (
  slot_token   TEXT PRIMARY KEY,
  city         TEXT NOT NULL,
  upstream_url TEXT NOT NULL,
  cached_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_slots_cache_at ON slots_cache(cached_at);

-- Periodic sample of free-slot counts per tenant/appointment type/office.
-- Written by the polling cycle (throttled, see app.analytics), pruned by
-- housekeeping. Purely observational: nothing in the notification path reads it.
CREATE TABLE IF NOT EXISTS availability_samples (
  sampled_at    TIMESTAMP NOT NULL,
  city          TEXT NOT NULL,
  service_uuid  TEXT NOT NULL,
  location_uuid TEXT NOT NULL,
  n_slots       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_avail_city_at
  ON availability_samples(city, sampled_at);
"""

def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # `isolation_level=None` = autocommit mode. Without this, Python's sqlite3
    # module opens implicit BEGINs before DML statements and never closes
    # them — which then collides with the explicit BEGIN issued by the
    # `transaction()` context manager.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 5s for a competing writer instead of raising "database is
    # locked" immediately. The web workers and the poller share this file, so
    # concurrent writes (a sign-up landing mid-poll-cycle) are expected — WAL
    # serialises them, and this lets a blocked write queue rather than error.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic BEGIN…COMMIT (or ROLLBACK on exception).

    Requires the connection to be in autocommit mode (`isolation_level=None`),
    which `connect()` above sets. Outside this context manager, every
    statement is its own transaction.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

def _add_missing_columns(conn: sqlite3.Connection, table: str,
                         columns: dict[str, str]) -> None:
    """Idempotently add columns that an existing table may predate.

    `CREATE TABLE IF NOT EXISTS` never alters an already-present table, so
    schema additions to a live DB need explicit `ALTER TABLE ADD COLUMN`. The
    duplicate-column `try/except` makes this safe even if two processes (poller
    and web) run init_schema concurrently.
    """
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass  # added concurrently by another process

def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    # Upgrade pre-existing city_state rows that predate the poll/request counters.
    _add_missing_columns(conn, "city_state", {
        "polls_today":    "INTEGER NOT NULL DEFAULT 0",
        "polls_total":    "INTEGER NOT NULL DEFAULT 0",
        "requests_total": "INTEGER NOT NULL DEFAULT 0",
        "counts_date":    "TEXT",
    })
    # confirmation_sent_at: when a pending sign-up's confirmation email was
    # successfully sent, so the retry pass can re-send quota-deferred ones.
    # last_match_count: slots matched at the last delivered digest, read by the
    # adaptive rate limit. NULL on existing rows means "not measured yet",
    # which the ladder treats as the base interval — so a migrated DB keeps
    # today's cadence until each subscriber's first digest re-measures it.
    # consent_special_at: when the subscriber gave the separate, explicit
    # Art. 9(2)(a) consent for a special-category service. NULL means they
    # never did — which is also the only legal state for a subscription to a
    # sensitive service, so the column doubles as the Art. 7(1) record of
    # consent and as the marker for the shorter retention.
    _add_missing_columns(conn, "subscriptions", {
        "confirmation_sent_at": "TIMESTAMP",
        "last_match_count": "INTEGER",
        "consecutive_digests": "INTEGER NOT NULL DEFAULT 0",
        "consent_special_at": "TIMESTAMP",
        # device_id: the app's push device, NULL on every mail subscription.
        # ADD COLUMN may carry a REFERENCES clause as long as the default is
        # NULL, which it is.
        "device_id": "INTEGER REFERENCES push_devices(id) ON DELETE CASCADE",
    })
    # Schema 16: a device's subscriptions are looked up by device_id on every
    # API call, and housekeeping's device prune runs one NOT EXISTS per device
    # row; without an index each was a scan of the whole table. Here, not in
    # SCHEMA_SQL, because an older table gets device_id only just above.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subs_device "
                 "ON subscriptions(device_id, deleted_at)")
    # Schema 15: device verification. A device row that predates the columns
    # reads as unverified and is asked to verify on its next call. Schema 16
    # adds the request's kind, its failure count and the sweep's not-before.
    _add_missing_columns(conn, "push_devices", {
        "verified_at": "TIMESTAMP",
        "verify_code_hash": "TEXT",
        "verify_requested_at": "TIMESTAMP",
        "verify_sent_at": "TIMESTAMP",
        "pending_secret_hash": "TEXT",
        "pending_since": "TIMESTAMP",
        "verify_code_at": "TIMESTAMP",
        "verify_kind": "TEXT",
        "verify_failures": "INTEGER NOT NULL DEFAULT 0",
        "verify_next_at": "TIMESTAMP",
    })
    # best_time: the earliest time told under a day key. Existing day-key rows
    # get NULL, which has_seen_slot reads as "told at an unknown time" and
    # keeps suppressing the whole day exactly as before the column existed —
    # the migration can't recover a time the old key never stored, and those
    # rows are pruned within 7 days anyway.
    _add_missing_columns(conn, "seen_slots", {"best_time": "TEXT"})
    # Durable per-day send counters power the admin page's provider-quota view.
    # sent_idempotency only lives 14 days (housekeeping prune), so month-to-date
    # can't be derived from it — seed the counters once from whatever history is
    # still there. INSERT OR IGNORE keeps the concurrent poller+web init race
    # harmless (first writer wins, second is a no-op).
    empty = conn.execute(
        "SELECT NOT EXISTS (SELECT 1 FROM email_send_counts)"
    ).fetchone()[0]
    if empty:
        conn.execute(
            "INSERT OR IGNORE INTO email_send_counts (provider, day, n) "
            "SELECT provider, date(sent_at), COUNT(*) FROM sent_idempotency "
            "WHERE provider != 'pending' GROUP BY provider, date(sent_at)"
        )
    # The per-subscriber cap counts digest_deliveries over the last 24h. A
    # migrated DB has none, which would lift the cap for everyone on the first
    # day — seed it once from seen_slots, where every delivered digest wrote
    # its slots under one timestamp, so distinct minutes ≈ digests. The web
    # workers and the poller all run this on the same `up -d`; BEGIN IMMEDIATE
    # takes the write lock before the emptiness check, so the second process
    # waits and then sees the seed instead of adding its own.
    conn.execute("BEGIN IMMEDIATE")
    try:
        empty = conn.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM digest_deliveries)"
        ).fetchone()[0]
        if empty:
            conn.execute(
                "INSERT INTO digest_deliveries (subscription_id, sent_at) "
                "SELECT subscription_id, MIN(sent_at) FROM seen_slots "
                "WHERE sent_at > datetime('now','-1 day') "
                "GROUP BY subscription_id, strftime('%Y-%m-%d %H:%M', sent_at)"
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    # Leftover isoformat() timestamps, rewritten in SQLite's shape (see
    # SQL_TS_FORMAT). Only the poller calls init_schema, so this runs once per
    # poller start and the WHERE makes every later start a cheap no-op. Web and
    # poller are replaced together on `up -d`: a sign-up the outgoing web
    # container takes after this pass can still land in the old form, and the
    # next poller start converts it. A value datetime() cannot parse is left
    # exactly as it was.
    for table, column in _PY_WRITTEN_TIMESTAMPS:
        conn.execute(
            f"UPDATE {table} SET {column}=datetime({column}) "
            f"WHERE {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*' "
            f"AND datetime({column}) IS NOT NULL"
        )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP",
        (str(SCHEMA_VERSION),),
    )
