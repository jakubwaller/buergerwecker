"""The one-off backfill that makes a granularity flip silent.

Flipping a tenant to `day` without it mails every subscriber once about a day
they were usually just told about — the exact noise the flip removes. The script
recovers the dates behind the stored hashes by enumerating the tenant's slot
space, so the new keys are already in place when the new code starts asking for
them.
"""
import pathlib
import sqlite3
import sys
from datetime import date, time, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts.backfill_day_keys import backfill  # noqa: E402

from app.db import connect, init_schema  # noqa: E402
from app.models import Filter, Slot  # noqa: E402
from app.repo import confirm, insert_pending, record_seen_slot  # noqa: E402


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _sub(db, services=("2408",)):
    f = Filter(appointment_types=list(services), locations="all",
               weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    sid = insert_pending(db, email="a@example.com", city="muenster-kfz",
                         language="de", filter_=f, ttl_days=90)
    confirm(db, sid)
    return sid


def _soon(days=3):
    """A date inside the script's enumeration window, computed rather than
    hardcoded — a fixed date would silently fall out of range as time passes and
    turn this test into a no-op."""
    return (date.today() + timedelta(days=days)).isoformat()


def test_backfill_writes_the_day_key_behind_a_stored_slot_hash(db):
    sid = _sub(db)
    slot = Slot(_soon(), "09:00", "243", "2408", "tok")
    record_seen_slot(db, sid, slot.hash())

    stats = backfill(db, "muenster-kfz", apply=True, days_ahead=30)
    assert stats["unrecognized"] == 0
    assert stats["written"] == 1
    row = db.execute("SELECT 1 FROM seen_slots WHERE subscription_id=? AND "
                     "slot_hash=?", (sid, slot.day_hash())).fetchone()
    assert row is not None


def test_dry_run_writes_nothing(db):
    sid = _sub(db)
    slot = Slot(_soon(), "09:00", "243", "2408", "tok")
    record_seen_slot(db, sid, slot.hash())

    stats = backfill(db, "muenster-kfz", apply=False, days_ahead=30)
    assert stats["keys"] == 1
    assert stats["written"] == 0
    assert db.execute("SELECT COUNT(*) c FROM seen_slots").fetchone()["c"] == 1


def test_backfill_is_idempotent(db):
    sid = _sub(db)
    record_seen_slot(db, sid, Slot(_soon(), "09:00", "243", "2408", "t").hash())

    assert backfill(db, "muenster-kfz", apply=True, days_ahead=30)["written"] == 1
    assert backfill(db, "muenster-kfz", apply=True, days_ahead=30)["written"] == 0


def test_many_times_on_one_day_collapse_to_a_single_key(db):
    sid = _sub(db)
    day = _soon()
    for hhmm in ("08:00", "09:20", "14:45"):
        record_seen_slot(db, sid, Slot(day, hhmm, "243", "2408", "t").hash())

    stats = backfill(db, "muenster-kfz", apply=True, days_ahead=30)
    assert stats["recognized"] == 3
    assert stats["written"] == 1


def test_the_backfilled_key_inherits_the_earliest_sighting(db):
    """It must age out of the 7-day prune on the original schedule — a key that
    outlived the rows it stands for would keep suppressing after those rows are
    gone."""
    sid = _sub(db)
    day = _soon()
    db.execute("INSERT INTO seen_slots (subscription_id, slot_hash, sent_at) "
               "VALUES (?,?,?)",
               (sid, Slot(day, "08:00", "243", "2408", "t").hash(),
                "2026-01-01 07:00:00"))
    db.execute("INSERT INTO seen_slots (subscription_id, slot_hash, sent_at) "
               "VALUES (?,?,?)",
               (sid, Slot(day, "09:00", "243", "2408", "t").hash(),
                "2026-01-02 07:00:00"))
    db.commit()

    backfill(db, "muenster-kfz", apply=True, days_ahead=30)
    sent_at = db.execute(
        "SELECT sent_at FROM seen_slots WHERE subscription_id=? AND slot_hash=?",
        (sid, Slot(day, "00:00", "243", "2408", "t").day_hash())).fetchone()["sent_at"]
    assert sent_at == "2026-01-01 07:00:00"


def test_an_unexplainable_row_is_reported_not_swallowed(db):
    """A hash outside the enumerated space costs one redundant mail, not
    correctness — but it must show up in the report, because a large count means
    the enumeration is missing part of the tenant's slot space."""
    sid = _sub(db)
    record_seen_slot(db, sid, "f" * 64)

    stats = backfill(db, "muenster-kfz", apply=True, days_ahead=30)
    assert stats["unrecognized"] == 1
    assert stats["written"] == 0


def test_a_service_no_longer_in_the_catalog_is_still_recovered(db):
    """Subscriptions outlive catalog entries (a withdrawn or excluded service),
    and those subscribers are exactly the ones a partial backfill would leave
    with the redundant mail."""
    sid = _sub(db, services=("9999",))
    slot = Slot(_soon(), "09:00", "243", "9999", "t")
    record_seen_slot(db, sid, slot.hash())

    stats = backfill(db, "muenster-kfz", apply=True, days_ahead=30)
    assert stats["unrecognized"] == 0
    assert db.execute("SELECT 1 FROM seen_slots WHERE subscription_id=? AND "
                      "slot_hash=?", (sid, slot.day_hash())).fetchone() is not None


def test_a_tenant_with_no_subscribers_is_a_no_op(db):
    stats = backfill(db, "muenster-kfz", apply=True, days_ahead=30)
    assert stats == {"subs": 0, "rows": 0, "recognized": 0, "unrecognized": 0,
                     "keys": 0, "written": 0}
