from datetime import datetime, timedelta

from app.analytics import (availability_daily, availability_summary,
                           prune_availability, record_availability, usage_daily)
from app.db import connect, init_schema
from app.models import Slot


def _conn(tmp_path):
    c = connect(str(tmp_path / "t.db"))
    init_schema(c)
    return c


def _slot(loc="L1", svc="S1", d="2026-08-01", t="09:00"):
    return Slot(date=d, time_str=t, location_uuid=loc, service_uuid=svc,
                booking_token="tok")


def test_record_groups_by_service_and_location(tmp_path):
    conn = _conn(tmp_path)
    record_availability(conn, {"leipzig": [
        _slot(t="09:00"), _slot(t="09:30"), _slot(loc="L2"),
    ]})
    rows = conn.execute(
        "SELECT location_uuid, n_slots FROM availability_samples ORDER BY location_uuid"
    ).fetchall()
    assert [(r["location_uuid"], r["n_slots"]) for r in rows] == [("L1", 2), ("L2", 1)]


def test_sampling_is_throttled_per_city(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.utcnow()
    record_availability(conn, {"leipzig": [_slot()]}, now=now)
    record_availability(conn, {"leipzig": [_slot()]}, now=now + timedelta(minutes=1))
    assert conn.execute("SELECT COUNT(DISTINCT sampled_at) FROM availability_samples"
                        ).fetchone()[0] == 1
    record_availability(conn, {"leipzig": [_slot()]}, now=now + timedelta(minutes=20))
    assert conn.execute("SELECT COUNT(DISTINCT sampled_at) FROM availability_samples"
                        ).fetchone()[0] == 2


def test_empty_poll_records_a_zero_marker(tmp_path):
    conn = _conn(tmp_path)
    record_availability(conn, {"leipzig": []})
    row = conn.execute("SELECT service_uuid, n_slots FROM availability_samples").fetchone()
    assert (row["service_uuid"], row["n_slots"]) == ("", 0)
    # The marker is excluded from the per-key summary...
    assert availability_summary(conn) == []
    # ...but still counts as a sample, so a later key reads as 100% empty then.
    assert availability_daily(conn) == [{"city": "leipzig",
                                         "day": datetime.utcnow().date().isoformat(),
                                         "avg_total": 0.0}]


def test_summary_counts_absent_keys_as_zero(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.utcnow()
    record_availability(conn, {"leipzig": [_slot()]}, now=now - timedelta(minutes=40))
    record_availability(conn, {"leipzig": []}, now=now - timedelta(minutes=20))
    record_availability(conn, {"leipzig": []}, now=now)
    (r,) = availability_summary(conn)
    assert r["avg_slots"] == 1.0 and r["samples"] == 1
    assert r["zero_rate"] == 67  # present in 1 of 3 samples


def test_prune_drops_old_samples(tmp_path):
    conn = _conn(tmp_path)
    conn.execute("INSERT INTO availability_samples VALUES "
                 "(datetime('now','-200 days'),'leipzig','S1','L1',3)")
    prune_availability(conn)
    assert conn.execute("SELECT COUNT(*) FROM availability_samples").fetchone()[0] == 0


def test_usage_daily_buckets_signups(tmp_path):
    conn = _conn(tmp_path)
    for city, conf in [("leipzig", 1), ("leipzig", 1), ("dresden", 0)]:
        conn.execute(
            "INSERT INTO subscriptions (email, city, filters_json, expires_at, "
            " confirmed_at) VALUES ('a@b.c', ?, '{}', datetime('now','+30 days'), ?)",
            (city, "2026-07-20" if conf else None))
    (d,) = usage_daily(conn)
    assert d["signups"] == 3 and d["confirmed"] == 2 and d["deleted"] == 0
    assert d["by_city"] == {"leipzig": 2, "dresden": 1}
