from __future__ import annotations
import sqlite3
from datetime import datetime


def _humanize_age(iso: str | None, now: datetime) -> str:
    """Return a ' (3h ago)' suffix for an ISO timestamp; '' if missing/unparsable.

    Naive timestamps are treated as UTC, mirroring the dashboard's JS.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.rstrip("Z"))
    except (TypeError, ValueError):
        return ""
    sec = max(0, int((now - dt).total_seconds()))
    if sec < 60:
        rel = "just now"
    elif sec < 3600:
        rel = f"{sec // 60}m ago"
    elif sec < 86400:
        rel = f"{sec // 3600}h ago"
    else:
        rel = f"{sec // 86400}d ago"
    return f" ({rel})"


def _ts(iso: str | None, now: datetime, *, missing: str) -> str:
    """Absolute UTC timestamp + relative hint, e.g. '2026-06-09 14:32Z (3h ago)'.

    Email is static, so (unlike the live dashboard) we show the exact UTC time
    and append the relative age as a glance hint. Missing -> `missing`.
    """
    if not iso:
        return missing
    try:
        abs_ = datetime.fromisoformat(iso.rstrip("Z")).strftime("%Y-%m-%d %H:%M") + "Z"
    except (TypeError, ValueError):
        abs_ = iso
    return f"{abs_}{_humanize_age(iso, now)}"


def render_summary_text(s: dict, *, now: datetime) -> str:
    """Plain-text ops summary mirroring the /admin dashboard sections.

    Pure: takes a `stats()` dict and the current time (injected for testable
    relative-age rendering) and returns the email body.
    """
    out = ["OVERVIEW",
           f"  Active subscriptions   {s['active_subscriptions']}",
           f"  Pending confirmation   {s['pending_confirmation']}",
           f"  Signups                24h {s['signups_last_24h']} · 7d {s['signups_last_7d']}",
           f"  Digests sent (7d)      {s['digests_sent_last_7d']}",
           f"  Emails sent (total)    {s['emails_sent_total']}",
           f"  Slots cached           {s['slots_cached']}",
           "",
           "CITIES"]
    # City-key union, same sources as the dashboard template.
    city_keys = (list(s.get("upstream_by_city", {}))
                 + list(s.get("active_subscriptions_by_city", {}))
                 + list(s.get("last_polled_at_by_city", {})))
    cities = sorted(dict.fromkeys(city_keys))
    if not cities:
        out.append("  No polling activity yet.")
    else:
        for city in cities:
            up = s.get("upstream_by_city", {}).get(city, {})
            zms = s.get("parser_zero_match_since_by_city", {}).get(city)
            lp = s.get("last_polled_at_by_city", {}).get(city)
            status = f"NO MATCHES since {zms}" if zms else "matching slots"
            subs = s.get("active_subscriptions_by_city", {}).get(city, 0)
            plans = s.get("current_plan_count_by_city", {}).get(city, 0)
            out.append(f"  {city} — {status}")
            out.append(f"    Last polled   {_ts(lp, now, missing='never')}")
            out.append(f"    Active subs   {subs} · Plans {plans}")
            out.append(f"    Polls         {up.get('polls_today', 0)} today "
                       f"· {up.get('polls_total', 0)} total")
            out.append(f"    Requests      {up.get('requests_today', 0)} today "
                       f"· {up.get('requests_total', 0)} total")
    out += ["",
            "SYSTEM",
            f"  Last housekeeping  {_ts(s.get('last_housekeeping_at'), now, missing='never')}",
            f"  Last backup        {_ts(s.get('last_backup_at'), now, missing='never')}",
            f"  Failure alert      {_ts(s.get('last_failure_alert_at'), now, missing='none')}"]
    return "\n".join(out)


def stats(conn: sqlite3.Connection) -> dict:
    def scalar(q, *args):
        row = conn.execute(q, args).fetchone()
        return row[0] if row else 0

    def meta_val(key):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # Per-city active subscriptions
    by_city_subs: dict[str, int] = {}
    by_city_plans: dict[str, int] = {}
    rows = conn.execute(
        "SELECT city, COUNT(*) AS n FROM subscriptions "
        "WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
        "AND expires_at > CURRENT_TIMESTAMP "
        "GROUP BY city"
    ).fetchall()
    for r in rows:
        by_city_subs[r["city"]] = r["n"]
    # Per-city distinct plans
    try:
        from app.repo import active_subscriptions
        from app.planning import build_plans
        import os
        max_cap = int(os.environ.get("MAX_PLANS_PER_CITY", "10"))
        subs = active_subscriptions(conn)
        plans = build_plans([(s.city, s.sub_filter) for s in subs],
                            max_plans_per_city=max_cap)
        for p in plans:
            by_city_plans[p.city] = by_city_plans.get(p.city, 0) + 1
    except Exception:
        pass
    # Per-city canary marker
    canary_rows = conn.execute(
        "SELECT city, zero_match_since FROM city_state "
        "WHERE zero_match_since IS NOT NULL"
    ).fetchall()
    canary = {r["city"]: r["zero_match_since"] for r in canary_rows}
    # Upstream poll/request counters + last-polled, per city. Defensive: a DB
    # that hasn't been migrated to the counter columns yet reports zeros.
    today = datetime.utcnow().date().isoformat()
    upstream_by_city: dict[str, dict] = {}
    last_polled_at_by_city: dict[str, str] = {}
    try:
        for r in conn.execute(
            "SELECT city, polls_today, polls_total, requests_today, "
            "requests_total, counts_date, last_polled_at FROM city_state"
        ).fetchall():
            fresh = r["counts_date"] == today
            upstream_by_city[r["city"]] = {
                "polls_today": r["polls_today"] if fresh else 0,
                "polls_total": r["polls_total"],
                "requests_today": r["requests_today"] if fresh else 0,
                "requests_total": r["requests_total"],
            }
            if r["last_polled_at"]:
                last_polled_at_by_city[r["city"]] = r["last_polled_at"]
    except sqlite3.OperationalError:
        pass  # pre-migration DB; counters not available yet
    return {
        "active_subscriptions":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                   "AND confirmed_at IS NOT NULL AND expires_at > CURRENT_TIMESTAMP"),
        "active_subscriptions_by_city": by_city_subs,
        "current_plan_count_by_city": by_city_plans,
        "parser_zero_match_since_by_city": canary,
        "pending_confirmation":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE confirmed_at IS NULL "
                   "AND deleted_at IS NULL"),
        "signups_last_24h":
            scalar("SELECT COUNT(*) FROM subscriptions "
                   "WHERE created_at > datetime('now','-1 day')"),
        "signups_last_7d":
            scalar("SELECT COUNT(*) FROM subscriptions "
                   "WHERE created_at > datetime('now','-7 days')"),
        "digests_sent_last_7d":
            scalar("SELECT COUNT(*) FROM sent_idempotency "
                   "WHERE sent_at > datetime('now','-7 days') "
                   "AND provider != 'pending'"),
        "upstream_by_city": upstream_by_city,
        "last_polled_at_by_city": last_polled_at_by_city,
        "slots_cached": scalar("SELECT COUNT(*) FROM slots_cache"),
        "emails_sent_total":
            scalar("SELECT COUNT(*) FROM sent_idempotency WHERE provider != 'pending'"),
        "last_failure_alert_at": meta_val("last_failure_alert_at"),
        "last_housekeeping_at": meta_val("last_housekeeping_at"),
        "last_backup_at":       meta_val("last_backup_at"),
    }
