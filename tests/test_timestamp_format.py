"""Stored timestamps share SQLite's own shape.

Queries compare them with CURRENT_TIMESTAMP and datetime('now') as plain text.
An isoformat() value sorts after a same-day space-separated one ("T" > " "),
so a subscription that expired at 05:21 still counted as active until the date
rolled over at midnight UTC — 11 digests to 7 people after their term had
ended, measured on 2026-09-15.
"""
from datetime import datetime, time, timedelta

import pytest

from app.analytics import record_availability
from app.db import connect, init_schema, sql_ts
from app.models import Filter
from app.repo import active_subscriptions, confirm, insert_pending, set_special_consent

SQLITE_SHAPE = ("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
                "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]")


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _f():
    return Filter(
        appointment_types=["svc-A"], locations="all",
        weekdays=[1, 2, 3, 4, 5, 6, 7],
        time_window_start=time(0, 0), time_window_end=time(23, 59),
    )


def _value(db, sql, *args):
    return db.execute(sql, args).fetchone()[0]


def _sign_up(db, **kw):
    return insert_pending(db, email="a@example.com", city="leipzig",
                          language="de", filter_=_f(), **kw)


def test_sql_ts_matches_current_timestamp(db):
    now = _value(db, "SELECT CURRENT_TIMESTAMP")
    assert sql_ts(datetime.fromisoformat(now)) == now


def test_a_term_that_has_run_out_is_not_active(db):
    # ttl_days=0 puts expires_at at "now". Written with isoformat() it read as
    # later than every CURRENT_TIMESTAMP of the same day.
    sid = _sign_up(db, ttl_days=0)
    confirm(db, sid)
    assert active_subscriptions(db) == []


def test_sign_up_and_consent_writes_are_sqlite_shaped(db):
    sid = _sign_up(db, ttl_days=14, consent_special=True)
    set_special_consent(db, sid, True)
    row = db.execute("SELECT expires_at, consent_special_at FROM subscriptions "
                     "WHERE id=?", (sid,)).fetchone()
    for value in row:
        assert _value(db, "SELECT ? GLOB ?", value, SQLITE_SHAPE), value


def test_availability_sample_is_sqlite_shaped(db):
    record_availability(db, {"leipzig": []},
                        now=datetime(2026, 9, 14, 5, 21, 53, 621099))
    assert _value(db, "SELECT sampled_at FROM availability_samples") \
        == "2026-09-14 05:21:53"


def test_init_schema_rewrites_isoformat_leftovers(db):
    sid = _sign_up(db, ttl_days=14)
    confirm(db, sid)
    expired = datetime.utcnow() - timedelta(minutes=5)
    # The shape rows written before the fix still carry.
    db.execute("UPDATE subscriptions SET expires_at=? WHERE id=?",
               (expired.isoformat(), sid))
    db.execute("INSERT INTO city_state (city, zero_match_since, "
               "last_canary_alert_at, last_polled_at) VALUES ('leipzig', ?, ?, ?)",
               ("2026-09-14T05:21:53.621099",) * 3)
    db.execute("INSERT INTO availability_samples (sampled_at, city, service_uuid, "
               "location_uuid, n_slots) VALUES ('2026-09-14T05:21:53.621099', "
               "'leipzig', '', '', 0)")

    init_schema(db)

    assert active_subscriptions(db) == []
    assert _value(db, "SELECT expires_at FROM subscriptions WHERE id=?", sid) \
        == sql_ts(expired)
    assert tuple(db.execute("SELECT zero_match_since, last_canary_alert_at, "
                            "last_polled_at FROM city_state").fetchone()) \
        == ("2026-09-14 05:21:53",) * 3
    assert _value(db, "SELECT sampled_at FROM availability_samples") \
        == "2026-09-14 05:21:53"


def test_init_schema_leaves_unparseable_values_alone(db):
    db.execute("INSERT INTO city_state (city, last_polled_at) VALUES ('leipzig', ?)",
               ("2026-13-45Tnonsense",))
    init_schema(db)
    assert _value(db, "SELECT last_polled_at FROM city_state") == "2026-13-45Tnonsense"
