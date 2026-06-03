import sqlite3
from pathlib import Path
from app.db import connect, init_schema, SCHEMA_VERSION

def test_init_schema_creates_all_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(str(db_path))
    init_schema(conn)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur}
    for expected in ("subscriptions", "seen_slots", "sent_idempotency",
                     "meta", "city_state", "slots_cache"):
        assert expected in tables, f"missing table: {expected}"

def test_transaction_commits_on_success(tmp_path):
    from app.db import transaction
    db_path = tmp_path / "t.db"
    conn = connect(str(db_path))
    init_schema(conn)
    with transaction(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    row = conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()
    assert row[0] == "v"

def test_transaction_rolls_back_on_exception(tmp_path):
    from app.db import transaction
    db_path = tmp_path / "t.db"
    conn = connect(str(db_path))
    init_schema(conn)
    try:
        with transaction(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    row = conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()
    assert row is None

def test_standalone_dml_visible_to_second_connection(tmp_path):
    """Autocommit means a standalone INSERT is visible across connections
    without an explicit commit. Regression guard for the isolation_level
    footgun."""
    db_path = str(tmp_path / "t.db")
    c1 = connect(db_path)
    init_schema(c1)
    c1.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    c2 = connect(db_path)
    row = c2.execute("SELECT value FROM meta WHERE key='k'").fetchone()
    assert row is not None and row[0] == "v"

def test_transaction_does_not_collide_with_dml(tmp_path):
    """The transaction() context manager must work right after standalone
    DML on the same connection."""
    from app.db import transaction
    db_path = str(tmp_path / "t.db")
    conn = connect(db_path)
    init_schema(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")
    with transaction(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('b', '2')")
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] >= 2

def test_init_schema_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(str(db_path))
    init_schema(conn)
    init_schema(conn)
    cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
    assert cur.fetchone()[0] == str(SCHEMA_VERSION)

def test_city_state_has_counter_columns(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(city_state)")}
    for c in ("polls_today", "polls_total", "requests_today",
              "requests_total", "counts_date"):
        assert c in cols, f"missing city_state column: {c}"

def test_migration_adds_counters_to_preexisting_city_state(tmp_path):
    """An older DB whose city_state predates the counter columns must be
    upgraded in place without losing rows."""
    db = str(tmp_path / "old.db")
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE city_state ("
        "  city TEXT PRIMARY KEY, zero_match_since TIMESTAMP,"
        "  last_canary_alert_at TIMESTAMP,"
        "  requests_today INTEGER NOT NULL DEFAULT 0,"
        "  last_polled_at TIMESTAMP);"
    )
    raw.execute("INSERT INTO city_state (city) VALUES ('leipzig')")
    raw.commit(); raw.close()
    conn = connect(db)
    init_schema(conn)  # must ALTER in the missing columns
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(city_state)")}
    for c in ("polls_today", "polls_total", "requests_total", "counts_date"):
        assert c in cols, f"migration missed column: {c}"
    assert conn.execute("SELECT city FROM city_state").fetchone()["city"] == "leipzig"

def test_wal_mode_enabled(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(str(db_path))
    init_schema(conn)
    cur = conn.execute("PRAGMA journal_mode")
    assert cur.fetchone()[0].lower() == "wal"
