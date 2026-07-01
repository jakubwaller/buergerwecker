from __future__ import annotations
import os
import time as time_mod
from datetime import datetime, timedelta
from app.config import load_config
from app.db import connect, init_schema
from app.cycle import run_cycle
from app.http_session import CountingSession
from app.housekeeping import run_once as housekeeping_run

def main() -> None:
    cfg = load_config()
    conn = connect(cfg.db_path)
    init_schema(conn)
    http = CountingSession()
    consecutive_failures = 0
    while True:
        # Sleep until next minute boundary
        now = time_mod.time()
        sleep_s = 60 - (now % 60)
        time_mod.sleep(sleep_s)
        cycle_id = datetime.utcnow().strftime("%Y%m%dT%H%M")
        try:
            _maybe_housekeeping(conn)
            run_cycle(conn,
                      max_plans_per_city=cfg.max_plans_per_city,
                      rate_limit_minutes=cfg.rate_limit_minutes,
                      cycle_id=cycle_id,
                      cfg=cfg,
                      http=http)
            # Retry confirmation emails deferred by quota exhaustion. After
            # run_cycle so time-sensitive slot notifications get quota first.
            from app.confirmations import send_pending_confirmations
            send_pending_confirmations(conn, cfg)
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            print(f"cycle {cycle_id} failed (consecutive={consecutive_failures}): {exc}",
                  flush=True)
            if consecutive_failures >= 3:
                _maybe_alert(conn, cfg, str(exc))

def _maybe_alert(conn, cfg, last_error: str) -> None:
    """Send a developer-alert email at most once per 24h."""
    from datetime import datetime, timedelta
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_failure_alert_at'"
    ).fetchone()
    if row:
        try:
            if datetime.utcnow() - datetime.fromisoformat(row["value"]) < timedelta(hours=24):
                return
        except ValueError:
            pass
    try:
        from app.mail import send as mail_send, _idem_key
        body = (f"Poller has been failing repeatedly. Last error:\n\n{last_error}\n\n"
                f"Check logs: docker compose logs poller")
        mail_send(conn, cfg.developer_email,
                  "[termine-notifier] poller failure burst",
                  body,
                  idem_key=_idem_key(0, [], f"alert-{datetime.utcnow().date()}"))
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_failure_alert_at', ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
            "updated_at=CURRENT_TIMESTAMP",
            (datetime.utcnow().isoformat(),),
        )
    except Exception as inner:
        print(f"failed to send alert email: {inner}", flush=True)

def _maybe_housekeeping(conn) -> None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_housekeeping_at'"
    ).fetchone()
    if not row:
        housekeeping_run(conn)
        return
    last = datetime.fromisoformat(row["value"])
    if datetime.utcnow() - last > timedelta(hours=24):
        housekeeping_run(conn)

if __name__ == "__main__":
    main()
