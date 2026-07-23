from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta

from app.analytics import availability_daily, availability_summary, usage_daily

# Thresholds for summary_anomalies(). Kept as module constants so the tests can
# pin exact boundaries and prod can be retuned in one place.
QUOTA_WARN_PCT = 80        # a provider's usage crossing this % of a cap warns
                           # here, ahead of the hard block in maybe_quota_alert.
SIGNUP_SPIKE_MIN = 10      # ignore "spikes" below this absolute 24h count
SIGNUP_SPIKE_FACTOR = 3    # 24h signups >= factor x daily baseline == a spike
SIGNUP_DROP_BASELINE = 3   # only flag a zero-signup day if the baseline is this
                           # busy (>= ~21/wk) — a quiet tenant hitting 0 is normal
STALE_POLL_HOURS = 3       # a city with subs unpolled this long has stalled
RECENT_ALERT_HOURS = 24    # reflect dedicated alerts fired within this window
BACKUP_STALE_HOURS = 48    # mirrors housekeeping._check_backup_health


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


def _parse_ts(iso: str | None) -> datetime | None:
    """ISO string (naive UTC, optional trailing Z) -> datetime, or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.rstrip("Z"))
    except (TypeError, ValueError):
        return None


def summary_anomalies(s: dict, *, now: datetime) -> list[str]:
    """Short, human-readable lines for anything worth a look — empty when all is
    healthy. Pure: reads a stats() dict + injected `now`.

    Hard failures (parser canary, stale backup, catalog drift, quota block,
    poller errors) already send their own targeted mail. The first three checks
    here surface *softer* signals those don't; the last two simply reflect a
    recent hard alert so this one mail is a complete picture, not a thing to
    cross-check against the others.
    """
    out: list[str] = []

    # 1. A provider's send volume is climbing toward a configured cap — warns
    #    ahead of the hard quota block in mail.maybe_quota_alert.
    for prov, u in sorted((s.get("email_usage") or {}).items()):
        for period, cap_key in (("today", "day_quota"), ("month", "month_quota")):
            cap = u.get(cap_key)
            used = u.get(period) or 0
            if cap and used >= cap * QUOTA_WARN_PCT / 100:
                out.append(f"{prov} {period} quota at {round(used * 100 / cap)}% "
                           f"({used}/{cap})")

    # 2. Signup volume deviates sharply from the trailing 7-day baseline — a
    #    press/Reddit surge, or an inflow that suddenly dried up.
    d24 = s.get("signups_last_24h") or 0
    baseline = (s.get("signups_last_7d") or 0) / 7
    if d24 >= SIGNUP_SPIKE_MIN and d24 >= baseline * SIGNUP_SPIKE_FACTOR:
        out.append(f"signup spike: {d24} in 24h vs ~{baseline:.0f}/day baseline")
    elif baseline >= SIGNUP_DROP_BASELINE and d24 == 0:
        out.append(f"no signups in 24h (baseline ~{baseline:.0f}/day)")

    # 3. A city with active subscribers has stopped polling — a silent stall the
    #    zero-match canary can't catch (it keys off matches, not poll liveness).
    #    Zero-matches itself is deliberately NOT flagged: for a scarce tenant
    #    like Leipzig that's a normal state, and a broken parser is the canary's job.
    subs_by_city = s.get("active_subscriptions_by_city") or {}
    polled = s.get("last_polled_at_by_city") or {}
    labels = s.get("city_labels") or {}
    for city, n in sorted(subs_by_city.items()):
        if n <= 0:
            continue
        label = labels.get(city, city)
        last = _parse_ts(polled.get(city))
        if last is None:
            out.append(f"{label}: {n} active subs but no poll recorded")
        elif now - last > timedelta(hours=STALE_POLL_HOURS):
            hrs = int((now - last).total_seconds() // 3600)
            out.append(f"{label}: not polled for {hrs}h ({n} active subs)")

    # 4. Reflect a dedicated alert that fired recently, for one consolidated view.
    fa = _parse_ts(s.get("last_failure_alert_at"))
    if fa is not None and now - fa <= timedelta(hours=RECENT_ALERT_HOURS):
        out.append("a failure alert fired in the last "
                   f"{RECENT_ALERT_HOURS}h "
                   f"({_ts(s.get('last_failure_alert_at'), now, missing='')})")
    bk = _parse_ts(s.get("last_backup_at"))
    if bk is None or now - bk > timedelta(hours=BACKUP_STALE_HOURS):
        out.append(f"backup is stale (>{BACKUP_STALE_HOURS}h) or missing")

    return out


def render_summary_email(s: dict, *, now: datetime, anomalies: list[str],
                         base_url: str = "") -> str:
    """Compact, phone-readable ops mail. Leads with the anomalies (or a weekly
    all-clear line), then a small at-a-glance snapshot, then a dashboard link.
    The full per-city / availability / usage breakdown lives on /admin — this
    mail is a glance, not the report it used to be.
    """
    lines: list[str] = []
    if anomalies:
        n = len(anomalies)
        lines.append(f"{n} thing{'s' if n != 1 else ''} need"
                     f"{'' if n != 1 else 's'} a look:")
        lines += [f"  • {a}" for a in anomalies]
    else:
        lines.append("Weekly all-clear — nothing unusual. Everything healthy.")
    lines.append("")

    by_city = s.get("active_subscriptions_by_city") or {}
    labels = s.get("city_labels") or {}
    city_str = " · ".join(f"{labels.get(c, c)} {n}"
                          for c, n in sorted(by_city.items())) or "none"
    prov = s.get("emails_by_provider_7d") or {}
    prov_str = " · ".join(f"{k} {prov[k]}" for k in sorted(prov)) or "none"
    lines += ["SNAPSHOT",
              f"  Active subs   {s.get('active_subscriptions', 0)}  ({city_str})",
              f"  Signups       24h {s.get('signups_last_24h', 0)}"
              f" · 7d {s.get('signups_last_7d', 0)}",
              f"  Notified      24h {s.get('notifications_24h', 0)}"
              f" · 7d {s.get('notifications_7d', 0)}",
              f"  Delivery 7d   {prov_str}"]
    # Quota line only when a daily cap is configured — otherwise it's just noise.
    quota_bits = [f"{p} {u.get('today', 0)}/{u['day_quota']}"
                  for p, u in sorted((s.get("email_usage") or {}).items())
                  if u.get("day_quota")]
    if quota_bits:
        lines.append(f"  Quota today   {' · '.join(quota_bits)}")

    admin = f"{base_url.rstrip('/')}/admin" if base_url else "/admin"
    lines += ["", f"Full dashboard → {admin}"]
    return "\n".join(lines)


def _email_usage(conn: sqlite3.Connection, cfg) -> dict:
    """Month-to-date + today send counts per provider, with configured caps.

    Reads the durable email_send_counts table (survives the 14-day
    sent_idempotency prune), so the admin page answers "how far into the
    free-tier quota are we?" without logging into the provider dashboards.
    Days/months are UTC — an approximation of each provider's own reset cycle.
    """
    caps = {
        "mailjet": {"month_quota": getattr(cfg, "mailjet_monthly_quota", None),
                    "day_quota":   getattr(cfg, "mailjet_daily_quota", None)},
        "resend":  {"month_quota": getattr(cfg, "resend_monthly_quota", None),
                    "day_quota":   getattr(cfg, "resend_daily_quota", None)},
    }
    usage = {p: {"month": 0, "today": 0, **caps[p]} for p in caps}
    try:
        rows = conn.execute(
            "SELECT provider, "
            "  SUM(n) AS month, "
            "  SUM(CASE WHEN day = date('now') THEN n ELSE 0 END) AS today "
            "FROM email_send_counts "
            "WHERE day >= date('now', 'start of month') "
            "GROUP BY provider"
        ).fetchall()
    except sqlite3.OperationalError:
        return usage  # pre-migration DB; counters not available yet
    for r in rows:
        u = usage.setdefault(r["provider"],
                             {"month_quota": None, "day_quota": None})
        u["month"] = r["month"]
        u["today"] = r["today"]
    return usage


def stats(conn: sqlite3.Connection, cfg=None) -> dict:
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
        "email_usage": _email_usage(conn, cfg),
        "last_failure_alert_at": meta_val("last_failure_alert_at"),
        "last_housekeeping_at": meta_val("last_housekeeping_at"),
        "last_backup_at":       meta_val("last_backup_at"),
        "availability": _availability(conn, city_labels),
        "availability_daily": availability_daily(conn),
        "usage_daily": usage_daily(conn),
    }


def _availability(conn: sqlite3.Connection, city_labels: dict) -> list[dict]:
    """Availability summary rows with catalog labels resolved for display.

    Unknown uuids (a service the city has since retired) keep their uuid — the
    history is still worth seeing, and dropping rows would silently understate
    past scarcity.
    """
    from app.catalog import load_catalog
    rows = availability_summary(conn)
    cats: dict[str, object] = {}
    for r in rows:
        city = r["city"]
        if city not in cats:
            try:
                cats[city] = load_catalog(city)
            except Exception:
                cats[city] = None
        cat = cats[city]
        r["city_label"] = city_labels.get(city, city)
        r["service"] = (cat.appointment_type_label(r["service_uuid"], "en")
                        if cat else r["service_uuid"])
        r["location"] = (cat.location_label(r["location_uuid"], "en")
                         if cat else r["location_uuid"])
    return rows
