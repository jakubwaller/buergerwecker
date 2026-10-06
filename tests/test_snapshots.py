"""The last poll's slots, kept for the app's overview (app/snapshots.py), and
how the cycle and housekeeping feed and trim it. No network."""
import json
from datetime import datetime, time
from unittest.mock import MagicMock, patch

import pytest

from app.db import connect, init_schema
from app.models import Filter, PollPlan, Slot
from app.repo import confirm, insert_pending
from app.snapshots import (MAX_SNAPSHOT_SLOTS, city_slots, prune_snapshots,
                           record_snapshots)


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def _slot(d="2026-06-10", t="10:30", loc="loc-1", svc="svc-A", res=""):
    return Slot(d, t, loc, svc, "tok", res)


def _plan(svc="svc-A", locs="all", city="leipzig"):
    return PollPlan(city=city, appointment_type=svc, locations=locs)


def _record(db, plans, slots_by_plan, *, ok=None, cities=None, now=None):
    ok = plans if ok is None else ok
    cities = cities if cities is not None else {p.city for p in plans}
    record_snapshots(db, plans, cities, ok, slots_by_plan, now=now)


def test_record_keeps_the_soonest_slots_deduped_and_counts_the_rest(db):
    plan = _plan()
    slots = [_slot(t="11:00"), _slot(t="10:30"), _slot(t="10:30", res="r2"),
             _slot(d="2026-06-09", loc="loc-2")]
    _record(db, [plan], {plan.key(): slots}, now=datetime(2026, 6, 8, 12, 0))
    row = db.execute("SELECT * FROM slot_snapshots").fetchone()
    assert row["city"] == "leipzig" and row["service_uuid"] == "svc-A"
    assert row["polled_at"] == "2026-06-08 12:00:00"
    assert json.loads(row["slots_json"]) == [["2026-06-09", "10:30", "loc-2"],
                                            ["2026-06-10", "10:30", "loc-1"],
                                            ["2026-06-10", "11:00", "loc-1"]]
    assert row["n_total"] == 3        # the second counter at 10:30 is the same slot


def test_record_caps_the_list_but_not_the_count(db):
    plan = _plan()
    slots = [_slot(t=f"{8 + i // 60:02d}:{i % 60:02d}") for i in range(MAX_SNAPSHOT_SLOTS + 40)]
    _record(db, [plan], {plan.key(): slots})
    row = db.execute("SELECT slots_json, n_total FROM slot_snapshots").fetchone()
    assert len(json.loads(row["slots_json"])) == MAX_SNAPSHOT_SLOTS
    assert row["n_total"] == MAX_SNAPSHOT_SLOTS + 40


def test_the_plans_of_one_service_are_one_row_with_an_exact_count(db):
    """Keyed by service, not by plan: a plan's key names its office set and
    changes with every edit, and two plans covering the same office must
    not count its slots twice."""
    a, b = _plan(locs="all"), _plan(locs=["loc-1"])
    _record(db, [a, b], {a.key(): [_slot(), _slot(loc="loc-2", t="09:00")],
                         b.key(): [_slot()]}, now=datetime(2026, 6, 8, 12, 0))
    other = _plan(svc="svc-B")
    _record(db, [other], {other.key(): [_slot(svc="svc-B", d="2026-07-01")]},
            now=datetime(2026, 6, 8, 12, 1))
    assert db.execute("SELECT COUNT(*) FROM slot_snapshots").fetchone()[0] == 2
    out = {e["service_uuid"]: e for e in city_slots(db, "leipzig")}
    assert out["svc-A"]["slots"] == [("2026-06-10", "09:00", "loc-2"),
                                     ("2026-06-10", "10:30", "loc-1")]
    assert out["svc-A"]["n_total"] == 2 and out["svc-A"]["polled_at"] == "2026-06-08 12:00:00"
    assert out["svc-B"]["slots"] == [("2026-07-01", "10:30", "loc-1")]
    assert city_slots(db, "bonn") == []


def test_a_later_poll_replaces_the_row_and_an_empty_one_empties_it(db):
    plan = _plan()
    _record(db, [plan], {plan.key(): [_slot()]}, now=datetime(2026, 6, 8, 12, 0))
    _record(db, [plan], {plan.key(): []}, now=datetime(2026, 6, 8, 12, 1))
    row = db.execute("SELECT slots_json, n_total, polled_at FROM slot_snapshots").fetchone()
    assert json.loads(row["slots_json"]) == [] and row["n_total"] == 0
    assert row["polled_at"] == "2026-06-08 12:01:00"
    assert db.execute("SELECT COUNT(*) FROM slot_snapshots").fetchone()[0] == 1


def test_a_changed_office_set_replaces_the_row_rather_than_adding_one(db):
    """The subscriber narrowed to one office: the service's row is rewritten
    from the new plan, and no stale row from the old office set survives to
    be merged under a fresh timestamp."""
    wide = _plan(locs="all")
    _record(db, [wide], {wide.key(): [_slot(), _slot(loc="loc-2", t="08:00")]},
            now=datetime(2026, 6, 8, 12, 0))
    narrow = _plan(locs=["loc-1"])
    _record(db, [narrow], {narrow.key(): [_slot()]}, now=datetime(2026, 6, 8, 12, 1))
    [entry] = city_slots(db, "leipzig")
    assert entry["slots"] == [("2026-06-10", "10:30", "loc-1")]
    assert entry["n_total"] == 1 and entry["polled_at"] == "2026-06-08 12:01:00"


def test_a_failed_plan_keeps_the_services_old_row(db):
    """One of the service's plans raised: the row is not rewritten from a
    partial result, and its older polled_at says so."""
    a, b = _plan(locs="all"), _plan(locs=["loc-1"])
    _record(db, [a, b], {a.key(): [_slot()], b.key(): [_slot()]},
            now=datetime(2026, 6, 8, 12, 0))
    _record(db, [a, b], {a.key(): [_slot(t="08:00")], b.key(): []}, ok=[a],
            now=datetime(2026, 6, 8, 12, 1))
    row = db.execute("SELECT n_total, polled_at, slots_json FROM slot_snapshots").fetchone()
    assert row["polled_at"] == "2026-06-08 12:00:00"
    assert json.loads(row["slots_json"]) == [["2026-06-10", "10:30", "loc-1"]]
    # A city that was not due is left alone entirely.
    _record(db, [a, b], {a.key(): [], b.key(): []}, cities=set(),
            now=datetime(2026, 6, 8, 12, 2))
    assert db.execute("SELECT polled_at FROM slot_snapshots").fetchone()[0] == "2026-06-08 12:00:00"


@pytest.mark.parametrize("raw, expect", [
    ("nope", []),
    ("null", []),
    ("7", []),
    ('[["2026-06-10","10:30"]]', []),
    ('[["2026-06-10","10:30",["loc"]]]', []),
    ('["abc"]', []),
    ('[7, ["2026-06-10","10:30","loc-1"]]', [("2026-06-10", "10:30", "loc-1")]),
])
def test_city_slots_survives_a_malformed_row(db, raw, expect):
    db.execute("INSERT INTO slot_snapshots VALUES ('leipzig', 'svc-A', ?, 1, "
               "CURRENT_TIMESTAMP)", (raw,))
    [entry] = city_slots(db, "leipzig")
    assert entry["slots"] == expect


def test_prune_drops_rows_older_than_a_day(db):
    db.execute("INSERT INTO slot_snapshots VALUES ('leipzig', 'old', '[]', 0, "
               "datetime('now','-25 hours'))")
    db.execute("INSERT INTO slot_snapshots VALUES ('leipzig', 'new', '[]', 0, "
               "datetime('now','-23 hours'))")
    prune_snapshots(db)
    assert [r[0] for r in db.execute("SELECT service_uuid FROM slot_snapshots")] == ["new"]
    from app.housekeeping import _prune_slot_snapshots
    db.execute("UPDATE slot_snapshots SET polled_at=datetime('now','-2 days')")
    _prune_slot_snapshots(db)
    assert db.execute("SELECT COUNT(*) FROM slot_snapshots").fetchone()[0] == 0


def test_a_database_error_does_not_break_the_cycle(db):
    plan = _plan()
    db.execute("DROP TABLE slot_snapshots")
    _record(db, [plan], {plan.key(): [_slot()]})   # logs, does not raise


# ---------------------------------------------------------------------------
# The cycle feeds it

_ENV = {
    "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m", "MAILJET_FROM_EMAIL": "x@x",
    "MAILJET_FROM_NAME": "x", "MAILJET_DAILY_QUOTA": "6000",
    "TOKEN_SECRET_PRIMARY": "x" * 32, "TOKEN_SECRET_PREVIOUS": "",
    "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
    "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
    "SUBSCRIPTION_TTL_DAYS": "90", "RENEWAL_REMINDER_DAYS_BEFORE": "10",
    "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
    "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
    "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
    "DEVELOPER_EMAIL": "dev@example.com", "KOFI_URL": "https://k",
}


def test_cycle_snapshots_every_polled_service_and_keeps_a_failed_one(db, monkeypatch):
    from app.cycle import run_cycle
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    f = Filter(appointment_types=["svc-A"], locations="all", weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    sid = insert_pending(db, email="a@example.com", city="leipzig", language="de",
                         filter_=f, ttl_days=90)
    confirm(db, sid)
    scraper = MagicMock()
    scraper.poll.return_value = [_slot(), _slot(t="09:00")]
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.cycle.send_digest"), patch("app.cycle.flush_digests"):
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    [entry] = city_slots(db, "leipzig")
    assert entry["service_uuid"] == "svc-A" and entry["n_total"] == 2
    assert entry["slots"][0] == ("2026-06-10", "09:00", "loc-1")
    first = entry["polled_at"]
    scraper.poll.side_effect = RuntimeError("upstream down")
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.cycle.send_digest"), patch("app.cycle.flush_digests"):
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c2")
    [entry] = city_slots(db, "leipzig")
    assert entry["n_total"] == 2 and entry["polled_at"] == first
