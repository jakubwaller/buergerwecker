from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

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
  confirmation_sent_at TIMESTAMP
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
    # Tracks when a pending sign-up's confirmation email was successfully sent,
    # so the retry pass can re-send confirmations that were quota-deferred.
    _add_missing_columns(conn, "subscriptions", {
        "confirmation_sent_at": "TIMESTAMP",
    })
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP",
        (str(SCHEMA_VERSION),),
    )
