"""Per-tenant notification granularity (`notify_granularity`).

A tenant whose vendor only ever exposes the *earliest* slot per office keys
seen_slots on the day, not on the slot: the "new" slot that appears the moment
somebody books is the same inventory a minute later, and re-notifying about it
tells the subscriber nothing they don't already know. Every other tenant keeps
per-slot identity, where a different time genuinely is a different opportunity.
"""
import json
from datetime import time
from unittest.mock import patch

import pytest

from app import catalog as catalog_mod
from app.catalog import Catalog, load_catalog
from app.db import connect, init_schema
from app.mail import BatchResult
from app.models import Filter, Slot
from app.repo import confirm, has_seen_slot, insert_pending
from app.cycle import run_cycle


@pytest.fixture
def db(tmp_path, monkeypatch):
    for k, v in {
        "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m",
        "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x",
        "MAILJET_DAILY_QUOTA": "6000",
        "TOKEN_SECRET_PRIMARY": "x" * 32, "TOKEN_SECRET_PREVIOUS": "",
        "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
        "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
        "SUBSCRIPTION_TTL_DAYS": "90", "RENEWAL_REMINDER_DAYS_BEFORE": "10",
        "MAX_PLANS_PER_CITY": "10", "PARSER_CANARY_THRESHOLD_HOURS": "2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99",
        "DEVELOPER_EMAIL": "dev@x", "KOFI_URL": "https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    return conn


def _f(types, locs="all"):
    return Filter(appointment_types=list(types),
                  locations="all" if locs == "all" else list(locs),
                  weekdays=[1, 2, 3, 4, 5, 6, 7],
                  time_window_start=time(0, 0), time_window_end=time(23, 59))


def _deliver_everything(sent):
    """Stand-in for send_batch that reports every staged item delivered."""
    def fake(conn, items, cfg):
        sent.extend(items)
        return BatchResult(delivered={it.idem_key for it in items})
    return fake


def _run(db, slots, *, cycle_id, sent):
    """One full cycle through the real digest/flush path."""
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.digest.send_batch", side_effect=_deliver_everything(sent)):
        gs.return_value.poll.return_value = slots
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id=cycle_id)


def _make_due_again(db):
    """Clear the two clocks that would suppress a second cycle: the
    subscriber's rate limit and the tenant's poll interval. Both are wall-clock
    in production; the test drives the state directly instead of freezing time,
    because last_notified_at is written by SQLite's CURRENT_TIMESTAMP and would
    not follow a frozen Python clock."""
    db.execute("UPDATE subscriptions SET last_notified_at=NULL")
    db.execute("UPDATE city_state SET last_polled_at=NULL")
    db.commit()


# --- the key itself -------------------------------------------------------

def test_day_hash_drops_the_time_but_keeps_day_office_and_service():
    base = Slot("2026-06-10", "09:00", "loc-1", "svc-A", "tok", "r1")
    later = Slot("2026-06-10", "09:20", "loc-1", "svc-A", "tok2", "r2")
    assert base.day_hash() == later.day_hash()
    assert base.hash() != later.hash()
    # …and nothing coarser than that: a different day, office or service is a
    # different piece of news.
    assert base.day_hash() != Slot("2026-06-11", "09:00", "loc-1", "svc-A", "t").day_hash()
    assert base.day_hash() != Slot("2026-06-10", "09:00", "loc-2", "svc-A", "t").day_hash()
    assert base.day_hash() != Slot("2026-06-10", "09:00", "loc-1", "svc-B", "t").day_hash()


def test_the_two_key_spaces_cannot_alias():
    """A coarse key must never collide with a fine one — seen_slots holds both
    for a subscriber whose tenant switched granularity."""
    slot = Slot("2026-06-10", "09:00", "loc-1", "svc-A", "tok")
    assert slot.day_hash() != slot.hash()
    # The payloads differ in shape, not just content: 3 fields vs 4.
    weird = Slot("2026-06-10", "loc-1", "svc-A", "", "tok")
    assert weird.hash() != slot.day_hash()


# --- reading the tenant declaration ---------------------------------------

def test_shipped_tenants_declare_the_granularity_they_need():
    assert load_catalog("muenster-kfz").notify_granularity == "day"
    # Everyone else keeps per-slot identity by omitting the key entirely.
    assert load_catalog("leipzig").notify_granularity == "slot"


def test_seen_key_follows_the_declaration():
    slot = Slot("2026-06-10", "09:00", "243", "2408", "tok")
    assert load_catalog("muenster-kfz").seen_key(slot) == slot.day_hash()
    assert load_catalog("leipzig").seen_key(slot) == slot.hash()


def test_unknown_granularity_falls_back_to_per_slot(tmp_path, monkeypatch):
    """The fail-safe direction is the one that can only ever send *more* mail:
    a typo must not silently start suppressing notifications."""
    city = tmp_path / "testcity"
    city.mkdir()
    (city / "scraper_config.json").write_text(json.dumps(
        {"vendor": "tevis", "base_url": "https://x", "md": 1, "mdt": 2,
         "notify_granularity": "weekly"}), encoding="utf-8")
    (city / "appointment_type.json").write_text(json.dumps({"A": "svc-A"}),
                                                encoding="utf-8")
    (city / "locations.json").write_text(json.dumps({"Amt": "loc-1"}),
                                         encoding="utf-8")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        cat = catalog_mod.load_catalog("testcity")
        assert cat.notify_granularity == "slot"
        slot = Slot("2026-06-10", "09:00", "loc-1", "svc-A", "tok")
        assert cat.seen_key(slot) == slot.hash()
    finally:
        catalog_mod.load_catalog.cache_clear()


def test_a_catalog_object_with_a_bogus_value_still_keys_per_slot():
    """load_catalog normalizes, but Catalog is constructed directly elsewhere
    (tests, catalog_sync); the method itself must not fail open into 'day'."""
    slot = Slot("2026-06-10", "09:00", "loc-1", "svc-A", "tok")
    cat = Catalog(city="x", appointment_types={}, locations={},
                  scraper_config={}, notify_granularity="hourly")
    assert cat.seen_key(slot) == slot.hash()


# --- behaviour through a real cycle ---------------------------------------

def test_day_tenant_is_not_renotified_when_the_earliest_slot_moves(db):
    """Münster-KFZ exposes only the earliest slot for its one office. When
    somebody books it, the next one surfaces — same day, same office, later
    time. That is not new availability and must not produce a second mail."""
    sid = insert_pending(db, email="a@example.com", city="muenster-kfz",
                         language="de", filter_=_f(["2408"]), ttl_days=90)
    confirm(db, sid)
    sent = []
    first = Slot("2026-06-10", "09:00", "243", "2408", "tok")
    _run(db, [first], cycle_id="c1", sent=sent)
    assert len(sent) == 1
    assert has_seen_slot(db, sid, first.day_hash()) is True

    _make_due_again(db)
    moved = Slot("2026-06-10", "09:20", "243", "2408", "tok2")
    _run(db, [moved], cycle_id="c2", sent=sent)
    assert len(sent) == 1, "the replacement earliest slot re-notified"


def test_day_tenant_notifies_again_once_a_new_day_opens(db):
    """The suppression is per day, not a mute button: tomorrow's release is
    news."""
    sid = insert_pending(db, email="a@example.com", city="muenster-kfz",
                         language="de", filter_=_f(["2408"]), ttl_days=90)
    confirm(db, sid)
    sent = []
    _run(db, [Slot("2026-06-10", "09:00", "243", "2408", "tok")],
         cycle_id="c1", sent=sent)
    assert len(sent) == 1

    _make_due_again(db)
    _run(db, [Slot("2026-06-11", "08:00", "243", "2408", "tok2")],
         cycle_id="c2", sent=sent)
    assert len(sent) == 2


def test_day_tenant_notifies_again_for_another_service_the_same_day(db):
    """Zulassung and Führerschein are separate news even at the same office on
    the same day."""
    sid = insert_pending(db, email="a@example.com", city="muenster-kfz",
                         language="de", filter_=_f(["2407", "2408"]),
                         ttl_days=90)
    confirm(db, sid)
    sent = []
    _run(db, [Slot("2026-06-10", "09:00", "243", "2408", "tok")],
         cycle_id="c1", sent=sent)
    assert len(sent) == 1

    _make_due_again(db)
    _run(db, [Slot("2026-06-10", "09:00", "243", "2408", "tok"),
              Slot("2026-06-10", "10:00", "243", "2407", "tok2")],
         cycle_id="c2", sent=sent)
    assert len(sent) == 2


def test_slot_tenant_still_notifies_about_a_different_time_the_same_day(db):
    """The regression guard for everyone else: in a tenant that lists real
    inventory, the 09:00 a subscriber was told about may already be gone, so
    the 14:00 is a genuine second opportunity."""
    sid = insert_pending(db, email="a@example.com", city="leipzig",
                         language="de", filter_=_f(["svc-A"]), ttl_days=90)
    confirm(db, sid)
    sent = []
    _run(db, [Slot("2026-06-10", "09:00", "loc-1", "svc-A", "tok")],
         cycle_id="c1", sent=sent)
    assert len(sent) == 1

    _make_due_again(db)
    _run(db, [Slot("2026-06-10", "14:00", "loc-1", "svc-A", "tok2")],
         cycle_id="c2", sent=sent)
    assert len(sent) == 2


def test_day_tenant_collapses_a_whole_day_into_one_seen_row(db):
    """Several times on one day arrive in a single digest and are recorded
    once; none of them can trigger a further mail."""
    sid = insert_pending(db, email="a@example.com", city="muenster-kfz",
                         language="de", filter_=_f(["2408"]), ttl_days=90)
    confirm(db, sid)
    sent = []
    slots = [Slot("2026-06-10", "09:00", "243", "2408", "t1"),
             Slot("2026-06-10", "11:00", "243", "2408", "t2"),
             Slot("2026-06-10", "15:00", "243", "2408", "t3")]
    _run(db, slots, cycle_id="c1", sent=sent)
    assert len(sent) == 1
    rows = db.execute("SELECT COUNT(*) c FROM seen_slots WHERE subscription_id=?",
                      (sid,)).fetchone()["c"]
    assert rows == 1

    _make_due_again(db)
    _run(db, slots, cycle_id="c2", sent=sent)
    assert len(sent) == 1


def test_a_deferred_digest_records_nothing(db):
    """A digest dropped for quota must leave no seen key behind — at day
    granularity that would silently cost the subscriber the whole day's news,
    not one slot's."""
    sid = insert_pending(db, email="a@example.com", city="muenster-kfz",
                         language="de", filter_=_f(["2408"]), ttl_days=90)
    confirm(db, sid)
    slot = Slot("2026-06-10", "09:00", "243", "2408", "tok")

    def defer_everything(conn, items, cfg):
        return BatchResult(delivered=set(), deferred=len(items))

    with patch("app.cycle.get_scraper") as gs, \
         patch("app.digest.send_batch", side_effect=defer_everything):
        gs.return_value.poll.return_value = [slot]
        run_cycle(db, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id="c1")
    assert has_seen_slot(db, sid, slot.day_hash()) is False

    # …and the next cycle really does re-send it.
    _make_due_again(db)
    sent = []
    _run(db, [slot], cycle_id="c2", sent=sent)
    assert len(sent) == 1
