from datetime import datetime, timedelta, time
from app.db import connect, init_schema
from app.models import Filter
from app.repo import insert_pending, confirm, soft_delete, active_subscriptions, \
    set_last_notified, record_seen_slot, has_seen_slot
import pytest

@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn

def _f():
    return Filter(
        appointment_types=["svc-A"], locations="all",
        weekdays=[1,2,3,4,5,6,7],
        time_window_start=time(0,0), time_window_end=time(23,59),
    )

def test_insert_pending_then_confirm(db):
    sub_id = insert_pending(db, email="a@x.com", city="leipzig",
                            language="de", filter_=_f(),
                            ttl_days=90)
    assert sub_id > 0
    assert active_subscriptions(db) == []  # not confirmed yet
    confirm(db, sub_id)
    active = active_subscriptions(db)
    assert len(active) == 1
    assert active[0].email == "a@x.com"
    assert active[0].confirmed_at is not None

def test_soft_delete_removes_from_active(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                         language="de", filter_=_f(), ttl_days=90)
    confirm(db, sid)
    soft_delete(db, sid)
    assert active_subscriptions(db) == []

def test_seen_slot_dedup(db):
    sid = insert_pending(db, email="a@x.com", city="leipzig",
                         language="de", filter_=_f(), ttl_days=90)
    confirm(db, sid)
    assert has_seen_slot(db, sid, "hash1") is False
    record_seen_slot(db, sid, "hash1")
    assert has_seen_slot(db, sid, "hash1") is True
