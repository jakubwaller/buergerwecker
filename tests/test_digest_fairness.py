from datetime import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.db import connect, init_schema
from app.digest import QueuedDigest, flush_digests
from app.mail import BatchResult, Outgoing
from app.models import Filter, Slot
from app.push import OutgoingPush, PushResult
from app.repo import (confirm, insert_pending, insert_push_subscription,
                      register_device)


@pytest.fixture
def db(tmp_path):
    conn = connect(str(tmp_path / "d.db"))
    init_schema(conn)
    return conn


def _q(sub_id, last_notified_at):
    return QueuedDigest(
        item=Outgoing(to=f"u{sub_id}@example.com", subject="s", body="b",
                      idem_key=f"k{sub_id}"),
        subscription=SimpleNamespace(id=sub_id, last_notified_at=last_notified_at),
        slots=[],
        match_count=1,
    )


def _flush_order(db, sink):
    with patch("app.digest.send_batch") as sb, \
         patch("app.digest.maybe_quota_alert"):
        sb.return_value = BatchResult()
        flush_digests(db, sink, SimpleNamespace())
    return [i.idem_key for i in sb.call_args.args[1]]


def test_flush_sends_longest_waiting_first(db):
    """send_batch fills provider batches in list order and defers the tail, so
    list order decides who loses a digest when quota runs out. Longest wait
    leads; never-notified subscribers lead outright."""
    order = _flush_order(db, [_q(1, "2026-08-19T10:00:00"),
                              _q(2, None),
                              _q(3, "2026-08-19T08:00:00")])
    assert order == ["k2", "k3", "k1"]


def test_flush_order_does_not_depend_on_staging_order(db):
    """The cycle stages in a stable order (city, then subscription id). Without
    the sort that put the same subscribers at the back of every saturated cycle
    — the point of the fix is that staging order stops mattering."""
    stamps = {1: "2026-08-19T10:00:00", 2: None, 3: "2026-08-19T08:00:00"}
    forward = _flush_order(db, [_q(i, stamps[i]) for i in (1, 2, 3)])
    backward = _flush_order(db, [_q(i, stamps[i]) for i in (3, 2, 1)])
    assert forward == backward == ["k2", "k3", "k1"]


def test_flush_tolerates_all_subscribers_never_notified(db):
    """All-NULL timestamps must not blow up the sort (a naive tuple key
    comparing None to None raises TypeError)."""
    assert _flush_order(db, [_q(1, None), _q(2, None)]) == ["k1", "k2"]


# ---------------------------------------------------------------------------
# One queue, one wall: when mail is out of quota, push does not get ahead
# (digest._behind_the_mail_wall). Real subscriptions, the real send_batch
# against a mocked provider, the push batch mocked.

def _filter():
    return Filter(appointment_types=["svc-A"], locations="all",
                  weekdays=[1, 2, 3, 4, 5, 6, 7],
                  time_window_start=time(0, 0), time_window_end=time(23, 59))


_SLOT = Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")


def _mail(db, name, waited_since):
    email = f"{name}@example.com"
    sid = insert_pending(db, email=email, city="leipzig", language="de",
                         filter_=_filter(), ttl_days=30)
    confirm(db, sid)
    return QueuedDigest(item=Outgoing(to=email, subject="s", body="b", idem_key=name),
                        subscription=SimpleNamespace(id=sid, last_notified_at=waited_since),
                        slots=[_SLOT], match_count=1)


def _push(db, name, waited_since):
    dev = register_device(db, platform="apns", token=f"tok-{name}",
                          secret_hash="h" * 64, language="de")
    sid = insert_push_subscription(db, device_id=dev, city="leipzig", language="de",
                                   filter_=_filter(), ttl_days=30)
    return QueuedDigest(item=OutgoingPush(device_id=dev, title="t", body="b",
                                          idem_key=name, token=f"tok-{name}"),
                        subscription=SimpleNamespace(id=sid, last_notified_at=waited_since),
                        slots=[_SLOT], match_count=1)


def _wall_cfg(room, **over):
    """A mail pool of `room` (Mailjet alone, its hourly window the tighter)."""
    base = dict(mailjet_hourly_quota=room, mailjet_daily_quota=200,
                email_provider_order=("mailjet",), max_send_failures_per_address=3,
                push_budget_per_cycle=0, push_budget_seconds=0)
    base.update(over)
    return SimpleNamespace(**base)


def _flush(db, sink, cfg):
    """Flush; returns (push items handed to the push batch, subscription ids
    recorded as told)."""
    handed = []

    def push_batch(conn, items, cfg, **budget):
        handed.extend(i.idem_key for i in items[:budget.get("max_items") or None])
        return PushResult(delivered=set(handed))

    with patch("app.mail._call_mailjet_batch", return_value=200), \
         patch("app.push.send_push_batch", push_batch), \
         patch("app.digest.maybe_quota_alert"):
        flush_digests(db, sink, cfg)
    told = {r[0] for r in db.execute("SELECT DISTINCT subscription_id FROM seen_slots")}
    return handed, told


@pytest.fixture
def one_provider(monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("SWEEGO_API_KEY", raising=False)


def test_push_waits_behind_the_mail_the_quota_turns_away(db, one_provider):
    m1 = _mail(db, "m1", "2026-06-01 08:00:00")
    p1 = _push(db, "p1", "2026-06-01 09:00:00")
    m2 = _mail(db, "m2", "2026-06-01 10:00:00")     # the wall: no room left for it
    p2 = _push(db, "p2", "2026-06-01 11:00:00")
    handed, told = _flush(db, [p2, m2, p1, m1], _wall_cfg(room=1))
    assert handed == ["p1"]
    assert told == {m1.subscription.id, p1.subscription.id}
    # m2 and p2 wait together, unrecorded, and lead the next cycle.
    assert db.execute("SELECT COUNT(*) FROM email_deferral_counts").fetchone()[0] == 1


def test_push_that_waited_longer_than_the_wall_still_goes_out(db, one_provider):
    """With the pool spent the wall is the first mail digest in the queue;
    a push digest ahead of it waited longer than anyone the quota turns away."""
    db.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('x', 'mailjet')")
    p0 = _push(db, "p0", None)                      # never notified: first in line
    m1 = _mail(db, "m1", "2026-06-01 08:00:00")
    p1 = _push(db, "p1", "2026-06-01 09:00:00")
    handed, told = _flush(db, [m1, p1, p0], _wall_cfg(room=1))
    assert handed == ["p0"]
    assert told == {p0.subscription.id}


def test_no_wall_once_mail_is_out_for_the_day(db, one_provider):
    """The daily window binds: the deferred mail waits for the rolling 24h
    window, so holding push would only silence the app with it."""
    m1 = _mail(db, "m1", "2026-06-01 08:00:00")
    p1 = _push(db, "p1", "2026-06-01 09:00:00")
    m2 = _mail(db, "m2", "2026-06-01 10:00:00")     # deferred until tomorrow
    p2 = _push(db, "p2", "2026-06-01 11:00:00")
    handed, told = _flush(db, [p2, m2, p1, m1],
                          _wall_cfg(room=100, mailjet_daily_quota=1))
    assert handed == ["p1", "p2"]
    assert told == {m1.subscription.id, p1.subscription.id, p2.subscription.id}


def test_no_wall_while_mail_has_room(db, one_provider):
    sink = [_mail(db, "m1", "2026-06-01 08:00:00"), _push(db, "p1", "2026-06-01 09:00:00"),
            _mail(db, "m2", "2026-06-01 10:00:00"), _push(db, "p2", "2026-06-01 11:00:00")]
    handed, told = _flush(db, sink, _wall_cfg(room=10))
    assert handed == ["p1", "p2"]
    assert told == {q.subscription.id for q in sink}


def test_a_dead_address_takes_no_room(db, one_provider):
    """send_batch never sends to a suppressed address, so it does not stand
    in the queue for the pool's room."""
    dead = _mail(db, "dead", "2026-06-01 07:00:00")
    db.execute("INSERT INTO email_failures (email, failures) VALUES ('dead@example.com', 3)")
    m1 = _mail(db, "m1", "2026-06-01 08:00:00")
    p1 = _push(db, "p1", "2026-06-01 09:00:00")
    handed, told = _flush(db, [dead, m1, p1], _wall_cfg(room=1))
    assert handed == ["p1"]
    assert told == {m1.subscription.id, p1.subscription.id}


def test_the_push_budget_holds_no_mail_back(db, one_provider):
    """One-sided on purpose: devices are free to mint, and a push queue that
    held mail back would hand them a lever over the website's subscribers."""
    p1 = _push(db, "p1", "2026-06-01 07:00:00")
    p2 = _push(db, "p2", "2026-06-01 08:00:00")
    m1 = _mail(db, "m1", "2026-06-01 09:00:00")
    handed, told = _flush(db, [m1, p2, p1], _wall_cfg(room=10, push_budget_per_cycle=1))
    assert handed == ["p1"]
    assert told == {p1.subscription.id, m1.subscription.id}
