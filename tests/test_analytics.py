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
    # ...but still counts as a sample, so the daily series shows 0, not a hole.
    assert availability_daily(conn) == [{"city": "leipzig",
                                         "day": datetime.utcnow().date().isoformat(),
                                         "avg_per_service": 0.0}]


def test_polled_service_without_slots_records_explicit_zero(tmp_path):
    conn = _conn(tmp_path)
    record_availability(conn, {"leipzig": []}, {"leipzig": {"S1"}})
    row = conn.execute("SELECT service_uuid, location_uuid, n_slots "
                       "FROM availability_samples").fetchone()
    assert (row["service_uuid"], row["location_uuid"], row["n_slots"]) == ("S1", "", 0)
    (r,) = availability_summary(conn)
    assert r["location_uuid"] == ""
    assert r["avg_slots"] == 0.0 and r["coverage"] == 100 and r["zero_rate"] == 100


def test_summary_separates_scarcity_from_coverage(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.utcnow()
    # Polled 3 of 4 samples; slots in 1 of the 3 polled ones.
    record_availability(conn, {"leipzig": [_slot(), _slot(t="09:30")]},
                        {"leipzig": {"S1"}}, now=now - timedelta(minutes=60))
    record_availability(conn, {"leipzig": []}, {"leipzig": {"S1"}},
                        now=now - timedelta(minutes=40))
    record_availability(conn, {"leipzig": []}, {"leipzig": {"S1"}},
                        now=now - timedelta(minutes=20))
    record_availability(conn, {"leipzig": []}, now=now)  # poll failed: not polled
    (r,) = availability_summary(conn)
    assert r["samples"] == 1
    assert r["avg_slots"] == 0.7           # 2 slots over 3 polled samples
    assert r["coverage"] == 75             # polled in 3 of 4 city samples
    assert r["zero_rate"] == 67            # empty in 2 of 3 polled samples


def test_daily_mean_is_per_polled_service(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.utcnow()
    # One sample: S1 floods 3 slots, S2 polled but empty → (3+0)/2 services.
    record_availability(conn, {"leipzig": [
        _slot(), _slot(t="09:30"), _slot(t="10:00"),
    ]}, {"leipzig": {"S1", "S2"}}, now=now)
    (d,) = availability_daily(conn)
    assert d["avg_per_service"] == 1.5


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
