#!/usr/bin/env python3
"""Local load test for termine-notifier. Providers are mocked — no network,
no real emails — so this is safe to run anywhere and must NOT be pointed at
production. It measures the two things a traffic spike actually stresses:

  A. Concurrent sign-up writes  -> SQLite write contention / lock errors.
  B. run_cycle() at N subscribers -> matching-loop + delivery time per cycle.

Usage:
    python scripts/loadtest.py                # default sizes
    python scripts/loadtest.py --subs 50000   # cycle test at 50k subscribers
    python scripts/loadtest.py --threads 32 --per-thread 200
"""
from __future__ import annotations
import argparse
import os
import tempfile
import threading
import time as _time
from datetime import time as _t
from unittest.mock import patch, MagicMock


def _setenv(db_path: str) -> None:
    os.environ.update({
        "DB_PATH": db_path,
        "TOKEN_SECRET_PRIMARY": "x" * 32, "TOKEN_SECRET_PREVIOUS": "",
        "ADMIN_TOKEN": "a" * 32, "PUBLIC_BASE_URL": "https://x",
        "MAILJET_API_KEY": "m", "MAILJET_API_SECRET": "m",
        "MAILJET_FROM_EMAIL": "x@x", "MAILJET_FROM_NAME": "x",
        "MAILJET_DAILY_QUOTA": "200", "RESEND_API_KEY": "re",
        "DEDUP_WINDOW_HOURS": "24", "RATE_LIMIT_MINUTES": "15",
        "SUBSCRIPTION_TTL_DAYS": "90", "RENEWAL_REMINDER_DAYS_BEFORE": "10",
        "MAX_PLANS_PER_CITY": "15", "PARSER_CANARY_THRESHOLD_HOURS": "2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR": "99999",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY": "99999",
        "DEVELOPER_EMAIL": "dev@x", "KOFI_URL": "https://k",
        # Free-tier quotas so the cycle test reflects production: delivery is
        # quota-bounded and the rest is deferred, exactly as it would be live.
        "RESEND_DAILY_QUOTA": "100", "MAILJET_HOURLY_QUOTA": "10",
    })


def test_concurrent_signups(db_path, threads, per_thread):
    from app.db import connect
    from app.models import Filter
    from app.repo import insert_pending
    f = Filter(["svc-A"], "all", [1, 2, 3, 4, 5], _t(0, 0), _t(23, 59))
    errors, done = [], []

    def worker(wid):
        conn = connect(db_path)
        for i in range(per_thread):
            try:
                insert_pending(conn, email=f"u{wid}_{i}@x.com", city="leipzig",
                               language="de", filter_=f, ttl_days=90)
                done.append(1)
            except Exception as exc:               # e.g. "database is locked"
                errors.append(str(exc))

    ts = [threading.Thread(target=worker, args=(w,)) for w in range(threads)]
    t0 = _time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    dt = _time.perf_counter() - t0
    total = threads * per_thread
    print(f"\n[A] Concurrent sign-ups: {threads} threads x {per_thread} = {total}")
    print(f"    time       : {dt:.2f}s  ({total/dt:,.0f} inserts/sec)")
    print(f"    lock errors: {len(errors)}"
          + (f"  e.g. {errors[0]}" if errors else "  (none)"))


def test_cycle_scaling(db_path, n_subs):
    from app.db import connect, transaction
    from app.config import load_config
    from app.models import Filter, Slot
    from app.repo import insert_pending, confirm
    from app.cycle import run_cycle
    conn = connect(db_path)
    f = Filter(["svc-A"], "all", [1, 2, 3, 4, 5, 6, 7], _t(0, 0), _t(23, 59))
    # Seed inside one transaction: autocommit per-row would fsync N times and
    # dominate the run. This is harness setup, not the measured path.
    with transaction(conn):
        for i in range(n_subs):
            sid = insert_pending(conn, email=f"c{i}@x.com", city="leipzig",
                                 language="de", filter_=f, ttl_days=90)
            confirm(conn, sid)
    cfg = load_config()
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "tok")]
    scraper = MagicMock(); scraper.poll.return_value = slots
    with patch("app.cycle.get_scraper", return_value=scraper), \
         patch("app.mail._call_mailjet_batch", return_value=True), \
         patch("app.mail._call_resend_batch", return_value=True):
        t0 = _time.perf_counter()
        run_cycle(conn, max_plans_per_city=15, rate_limit_minutes=15,
                  cycle_id="load", cfg=cfg)
        dt = _time.perf_counter() - t0
    sent = conn.execute(
        "SELECT COUNT(*) AS n FROM sent_idempotency WHERE provider!='pending'"
    ).fetchone()["n"]
    print(f"\n[B] run_cycle at {n_subs:,} confirmed subscribers (free-tier quotas)")
    print(f"    cycle time  : {dt:.2f}s  ({'OK, under 60s' if dt < 60 else 'OVER 60s!'})")
    print(f"    digests sent: {sent:,} (quota-capped); deferred: {n_subs - sent:,}")
    print(f"    matching cost: {dt/max(n_subs,1)*1000:.3f} ms/sub")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--per-thread", type=int, default=100)
    ap.add_argument("--subs", type=int, nargs="+", default=[1000, 10000, 50000])
    args = ap.parse_args()

    for n in args.subs:
        with tempfile.TemporaryDirectory() as d:
            db_path = os.path.join(d, "load.db")
            _setenv(db_path)
            from app.db import connect, init_schema
            init_schema(connect(db_path))
            if n == args.subs[0]:
                test_concurrent_signups(db_path, args.threads, args.per_thread)
            test_cycle_scaling(db_path, n)


if __name__ == "__main__":
    main()
