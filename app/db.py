from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 9

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
  consent_special_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_active_subs
  ON subscriptions(deleted_at, confirmed_at, expires_at, city);

CREATE TABLE IF NOT EXISTS seen_slots (
  subscription_id INTEGER NOT NULL,
  slot_hash       TEXT NOT NULL,
  sent_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
    })
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
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP",
        (str(SCHEMA_VERSION),),
    )
