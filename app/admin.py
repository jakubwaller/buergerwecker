from __future__ import annotations
import sqlite3


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
        "last_housekeeping_at": meta_val("last_housekeeping_at"),
        "last_backup_at":       meta_val("last_backup_at"),
    }
