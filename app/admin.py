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
    prov = s.get("emails_by_provider_7d") or {}
    prov_str = " · ".join(f"{k} {prov[k]}" for k in sorted(prov)) or "none"
    ln = s.get("last_notification")
    recent = (f"{_ts(ln.get('at'), now, missing='none')} — sub #{ln.get('sub_id')}"
              if ln else "none yet")
    out = ["OVERVIEW",
           f"  Active subscriptions   {s['active_subscriptions']}",
           f"  Pending confirmation   {s['pending_confirmation']}",
           f"  Signups                24h {s['signups_last_24h']} · 7d {s['signups_last_7d']}",
           f"  Digests sent (7d)      {s['digests_sent_last_7d']}",
           f"  Emails sent (total)    {s['emails_sent_total']}",
           f"  Delivery (7d)          {prov_str}",
           f"  Slots cached           {s['slots_cached']}",
           "",
           "NOTIFICATIONS (appointment slots delivered to subscribers)",
           f"  Subscribers notified   24h {s.get('notifications_24h', 0)}"
           f" · 7d {s.get('notifications_7d', 0)}"
           f" · ever {s.get('subscribers_ever_notified', 0)}",
           f"  Awaiting first match   {s.get('active_awaiting_first_match', 0)}"
           f" of {s['active_subscriptions']} active",
           f"  Most recent            {recent}",
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
            label = s.get("city_labels", {}).get(city, city)
            out.append(f"  {label} — {status}")
            out.append(f"    Last polled   {_ts(lp, now, missing='never')}")
            out.append(f"    Active subs   {subs} · Plans {plans}")
            out.append(f"    Polls         {up.get('polls_today', 0)} today "
                       f"· {up.get('polls_total', 0)} total")
            out.append(f"    Requests      {up.get('requests_today', 0)} today "
                       f"· {up.get('requests_total', 0)} total")
    # Combined load per physical upstream host — several tenants can share one
    # server, and the rate-limit/ban-risk number is the host total.
    hosts = s.get("upstream_by_host") or {}
    if hosts:
        out += ["", "UPSTREAM HOSTS (combined load across tenants)"]
        for host in sorted(hosts):
            agg = hosts[host]
            out.append(f"  {host}")
            out.append(f"    Requests      {agg.get('requests_today', 0)} today "
                       f"· {agg.get('requests_total', 0)} total")
            out.append(f"    Polls         {agg.get('polls_today', 0)} today "
                       f"· {agg.get('polls_total', 0)} total")
            out.append(f"    Tenants       {', '.join(agg.get('tenants', []))}")
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
    # Human labels + upstream host per tenant, from the catalog. The "city"
    # key is a tenant (leipzig, leipzig-abh), not a geography; the label comes
    # from display.json. A key whose catalog dir no longer exists renders as
    # the raw key and is left out of host aggregation.
    from urllib.parse import urlsplit
    from app.catalog import load_catalog
    city_labels: dict[str, str] = {}
    city_hosts: dict[str, str] = {}
    for c in set(list(by_city_subs) + list(upstream_by_city)
                 + list(last_polled_at_by_city)):
        try:
            cat = load_catalog(c)
        except Exception:
            continue
        label = cat.display_text("label", "en")  # admin is English-only
        if label:
            city_labels[c] = label
        host = urlsplit(cat.scraper_config.get("base_url", "")).netloc
        if host:
            city_hosts[c] = host
    # Aggregate upstream counters per physical host: several tenants can share
    # one upstream (leipzig + leipzig-abh both poll
    # terminvereinbarung.leipzig.de), and the number that matters for
    # rate-limit/ban risk is the HOST total, not the per-tenant split. The
    # *_today values are already normalized to 0 for stale counts_date above,
    # so summing is safe.
    upstream_by_host: dict[str, dict] = {}
    for c, up in upstream_by_city.items():
        host = city_hosts.get(c)
        if not host:
            continue
        agg = upstream_by_host.setdefault(host, {
            "polls_today": 0, "polls_total": 0,
            "requests_today": 0, "requests_total": 0, "tenants": [],
        })
        for k in ("polls_today", "polls_total", "requests_today", "requests_total"):
            agg[k] += up[k]
        agg["tenants"].append(c)
    for agg in upstream_by_host.values():
        agg["tenants"].sort()
    # Slot-match notifications actually delivered to subscribers. `last_notified_at`
    # is set only when a real appointment slot matched and a digest went out, so it
    # is the truest "a subscriber was served" signal — distinct from emails_sent_total,
    # which also counts confirmations, heartbeats and these summary emails.
    notif = conn.execute(
        "SELECT id, last_notified_at FROM subscriptions "
        "WHERE last_notified_at IS NOT NULL ORDER BY last_notified_at DESC LIMIT 1"
    ).fetchone()
    last_notification = ({"sub_id": notif["id"], "at": notif["last_notified_at"]}
                         if notif else None)
    # Delivery provider mix (7d). A rising `resend` share means the Mailjet primary
    # is rejecting sends and the failover is carrying the mail — an early warning.
    provider_7d: dict[str, int] = {}
    for r in conn.execute(
        "SELECT provider, COUNT(*) AS n FROM sent_idempotency "
        "WHERE sent_at > datetime('now','-7 days') AND provider != 'pending' "
        "GROUP BY provider"
    ).fetchall():
        provider_7d[r["provider"]] = r["n"]
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
        "upstream_by_host": upstream_by_host,
        "city_labels": city_labels,
        "last_polled_at_by_city": last_polled_at_by_city,
        "slots_cached": scalar("SELECT COUNT(*) FROM slots_cache"),
        "emails_sent_total":
            scalar("SELECT COUNT(*) FROM sent_idempotency WHERE provider != 'pending'"),
        "notifications_24h":
            scalar("SELECT COUNT(*) FROM subscriptions "
                   "WHERE last_notified_at > datetime('now','-1 day')"),
        "notifications_7d":
            scalar("SELECT COUNT(*) FROM subscriptions "
                   "WHERE last_notified_at > datetime('now','-7 days')"),
        "subscribers_ever_notified":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE last_notified_at IS NOT NULL"),
        "active_awaiting_first_match":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                   "AND confirmed_at IS NOT NULL AND expires_at > CURRENT_TIMESTAMP "
                   "AND last_notified_at IS NULL"),
        "last_notification": last_notification,
        "emails_by_provider_7d": provider_7d,
        "last_failure_alert_at": meta_val("last_failure_alert_at"),
        "last_housekeeping_at": meta_val("last_housekeeping_at"),
        "last_backup_at":       meta_val("last_backup_at"),
    }
