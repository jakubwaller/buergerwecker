from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.db import connect, init_schema
from app.digest import QueuedDigest, flush_digests
from app.mail import BatchResult, Outgoing


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
