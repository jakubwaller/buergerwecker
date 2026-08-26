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
# Deliverability. 0.30% is the spam-complaint rate Gmail and Yahoo publish as
# the line for a bulk sender; above it they throttle or junk the domain, and no
# amount of provider quota buys a way out. The bounce figure is the industry
# rule of thumb at which receivers start doing the same.
COMPLAINT_RATE_WARN_PCT = 0.3
BOUNCE_RATE_WARN_PCT = 2.0
# Below this many sends a rate is arithmetic noise: one complaint out of 50
# mails is 2%, and reporting that as a reputation emergency trains the reader
# to ignore the line.
DELIVERABILITY_MIN_SENDS = 500
WEBHOOK_SILENT_HOURS = 48  # a feedback loop this quiet is presumed broken


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


def _walls_detail(walls: dict | None) -> str:
    """" (hourly 2, daily 1 — ...)" for a day's deferrals, or "" when the
    split is unknown. The daily wall is the one that costs appointments, so it
    gets the sentence; an hourly deferral is cleared by the next cycle and an
    outage by the next retry."""
    if not walls:
        return ""
    bits = [f"{w} {walls[w]}" for w in ("outage", "hourly", "daily") if walls.get(w)]
    tail = ""
    if walls.get("daily"):
        tail = (" — the daily ones wait for the rolling 24h window, and the "
                "slot may be gone by then")
    return f" ({', '.join(bits)}{tail})"

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

    # 1. Send volume is climbing toward a configured cap — warns ahead of the
    #    hard quota block in mail.maybe_quota_alert.
    #    Daily is graded on the COMBINED pool, for the same reason the alert is:
    #    Mailjet-first routing only spills down the chain once Mailjet is
    #    exhausted, so "mailjet today 98%" fires on any busy day while the pool
    #    still has a third of its capacity free. Monthly stays per-provider —
    #    those caps are hard, per-account walls that no failover can borrow
    #    against.
    #    Graded on the ROLLING 24h window, not the UTC-day counters, because
    #    that is what mail._send_batch actually gates on. The two diverge, and
    #    the divergence is worst exactly when it matters: just after UTC
    #    midnight `today` snaps to 0 while `rolling` still carries last
    #    evening's traffic, so grading on `today` reports all-clear at the
    #    moment the real gate is closest to deferring. It also read low the rest
    #    of the day — on 2026-08-25 the alert said 89% (532/600) while the gate
    #    stood at 92% (555/600), i.e. the warning fired late and disagreed with
    #    the Email quota section of the same page.
    usage = sorted((s.get("email_usage") or {}).items())
    capped = [(p, u) for p, u in usage if u.get("day_quota")]
    #    `rolling` is missing only on a pre-migration DB, where the counters are
    #    all zero anyway. Fall back to `today` rather than let a missing key
    #    read as "no sends" and silence the warning.
    roll_used = sum(u["rolling"] if u.get("rolling") is not None
                    else (u.get("today") or 0)
                    for _, u in capped)
    roll_cap = sum(u["day_quota"] for _, u in capped)
    if roll_cap and roll_used >= roll_cap * QUOTA_WARN_PCT / 100:
        out.append(f"combined rolling 24h quota at "
                   f"{round(roll_used * 100 / roll_cap)}% "
                   f"({roll_used}/{roll_cap})")
    for prov, u in usage:
        cap, used = u.get("month_quota"), u.get("month") or 0
        if cap and used >= cap * QUOTA_WARN_PCT / 100:
            out.append(f"{prov} month quota at {round(used * 100 / cap)}% "
                       f"({used}/{cap})")

    #    A deferral is not a "nearing the cap" warning — it is the cap already
    #    having cost someone a notification, so it is reported whatever the
    #    percentages say.
    deferred = s.get("deferrals_today") or 0
    if deferred:
        out.append(f"{deferred} notification(s) deferred today for lack of quota"
                   f"{_walls_detail(s.get('deferral_walls_today'))}")

    # 2. Deliverability. This is the one failure in this list that cannot be
    #    fixed after the fact: once large receivers throttle the sending domain
    #    over a complaint or bounce rate, extra provider quota buys nothing and
    #    recovery takes weeks. A silent webhook is reported for the same reason
    #    — with the feedback loop off, both rates read 0.00% forever, which is
    #    indistinguishable from healthy.
    d = s.get("deliverability") or {}
    if d and not d.get("configured"):
        out.append("delivery-feedback webhooks are not configured — bounces "
                   "and spam complaints are never learned")
    elif (d.get("sent_30d") or 0) >= DELIVERABILITY_MIN_SENDS:
        cr = d.get("complaint_rate")
        if cr is not None and cr >= COMPLAINT_RATE_WARN_PCT:
            out.append(f"spam-complaint rate {cr:.2f}% over 30d "
                       f"({d.get('complaint_30d')}/{d.get('sent_30d')}) — "
                       f"Gmail and Yahoo throttle above "
                       f"{COMPLAINT_RATE_WARN_PCT}%")
        br = d.get("bounce_rate")
        if br is not None and br >= BOUNCE_RATE_WARN_PCT:
            out.append(f"hard-bounce rate {br:.2f}% over 30d "
                       f"({d.get('hard_bounce_30d')}/{d.get('sent_30d')})")
    for name in d.get("providers_silent") or []:
        out.append(f"no delivery feedback from {name} in "
                   f"{WEBHOOK_SILENT_HOURS}h — check its webhook")

    # 3. Signup volume deviates sharply from the trailing 7-day baseline — a
    #    press/Reddit surge, or an inflow that suddenly dried up.
    d24 = s.get("signups_last_24h") or 0
    baseline = (s.get("signups_last_7d") or 0) / 7
    if d24 >= SIGNUP_SPIKE_MIN and d24 >= baseline * SIGNUP_SPIKE_FACTOR:
        out.append(f"signup spike: {d24} in 24h vs ~{baseline:.0f}/day baseline")
    elif baseline >= SIGNUP_DROP_BASELINE and d24 == 0:
        out.append(f"no signups in 24h (baseline ~{baseline:.0f}/day)")

    # 4. A city with active subscribers has stopped polling — a silent stall the
    #    zero-match canary can't catch (it keys off matches, not poll liveness).
    #    Zero-matches itself is deliberately NOT flagged: for a scarce tenant
    #    like Leipzig that's a normal state, and a broken parser is the canary's job.
    subs_by_city = s.get("active_subscriptions_by_city") or {}
    polled = s.get("last_polled_at_by_city") or {}
    # Prefer the short geographic name ("Kiel") over the full product label
    # ("Kiel: citizens' office appointments") — these lines already say what's
    # wrong, the label's service description is just noise here.
    labels = {**(s.get("city_labels") or {}), **(s.get("city_names") or {})}
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

    # 5. Reflect a dedicated alert that fired recently, for one consolidated view.
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
    labels = {**(s.get("city_labels") or {}), **(s.get("city_names") or {})}
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
    # Quota line only when a daily cap is configured — otherwise it's just
    # noise. email_usage arrives in EMAIL_PROVIDER_ORDER; keep that order.
    capped = [(p, u) for p, u in (s.get("email_usage") or {}).items()
              if u.get("day_quota")]
    quota_bits = [f"{p} {u.get('today', 0)}/{u['day_quota']}" for p, u in capped]
    if quota_bits:
        lines.append(f"  Quota today   {' · '.join(quota_bits)}")
        roll_used = sum((u.get("rolling") or 0) for _, u in capped)
        roll_cap = sum(u["day_quota"] for _, u in capped)
        lines.append(f"  Gating 24h    {roll_used}/{roll_cap} combined rolling")
    if s.get("deferrals_today") or s.get("deferrals_7d"):
        lines.append(f"  Deferred      today {s.get('deferrals_today', 0)}"
                     f" · 7d {s.get('deferrals_7d', 0)}"
                     f"{_walls_detail(s.get('deferral_walls_today'))}")
    if s.get("subscriber_cap"):
        d = s.get("digests_per_sub_24h") or {}
        lines.append(f"  Sub cap {s['subscriber_cap']}/24h  held today "
                     f"{s.get('cap_holds_today', 0)} · capped now "
                     f"{s.get('capped_now', 0)} · {d.get('digests', 0)} digests "
                     f"to {d.get('subs', 0)} subscribers, {d.get('mean', 0)}/sub")

    admin = f"{base_url.rstrip('/')}/admin" if base_url else "/admin"
    lines += ["", f"Full dashboard → {admin}"]
    return "\n".join(lines)


def _email_usage(conn: sqlite3.Connection, cfg) -> dict:
    """Month-to-date + today send counts per provider, with configured caps.

    Reads the durable email_send_counts table (survives the 14-day
    sent_idempotency prune), so the admin page answers "how far into the
    free-tier quota are we?" without logging into the provider dashboards.
    Days/months are UTC — an approximation of each provider's own reset cycle.

    Each provider also carries `rolling`, the trailing-24h count from
    mail._window_used. That is the number that actually gates a send, and it is
    NOT the UTC-day figure next to it: just after UTC midnight `today` snaps to
    0 while `rolling` still carries last evening's traffic. Showing only one of
    them is what made the admin page and the low-quota mail look like they were
    contradicting each other.
    """
    from app.mail import _window_used
    caps = {
        "mailjet": {"month_quota": getattr(cfg, "mailjet_monthly_quota", None),
                    "day_quota":   getattr(cfg, "mailjet_daily_quota", None)},
    }
    # Brevo/Sweego join the table only once they can actually send: API key
    # configured AND named in EMAIL_PROVIDER_ORDER — the same gate
    # mail._daily_usage applies via _providers. A provider that cannot send
    # (say, a key configured ahead of a smoke test while the order still
    # excludes it) must not add its cap to the combined-pool grading in
    # summary_anomalies. Only Mailjet gets an unconditional row: it is required
    # config. Retired providers (e.g. Resend, phased out 2026-08) keep their
    # historical counters and trail the table via chain_order, capless.
    order = getattr(cfg, "email_provider_order", ("mailjet", "brevo", "sweego"))
    if getattr(cfg, "brevo_api_key", "") and "brevo" in order:
        caps["brevo"] = {"month_quota": getattr(cfg, "brevo_monthly_quota", None),
                         "day_quota":   getattr(cfg, "brevo_daily_quota", None)}
    if getattr(cfg, "sweego_api_key", "") and "sweego" in order:
        caps["sweego"] = {"month_quota": getattr(cfg, "sweego_monthly_quota", None),
                          "day_quota":   getattr(cfg, "sweego_daily_quota", None)}
    usage = {p: {"month": 0, "today": 0, **caps[p]} for p in caps}

    def chain_order(u: dict) -> dict:
        # The admin page and ops mail render this dict in iteration order;
        # make that EMAIL_PROVIDER_ORDER (the actual fallback chain), with
        # providers that only exist in the counters — retired ones — after.
        out = {p: u[p] for p in order if p in u}
        out.update(sorted((p, v) for p, v in u.items() if p not in out))
        return out

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
        return chain_order(usage)  # pre-migration DB; counters not available yet
    for r in rows:
        u = usage.setdefault(r["provider"],
                             {"month_quota": None, "day_quota": None})
        u["month"] = r["month"]
        u["today"] = r["today"]
    for name, u in usage.items():
        u["rolling"] = _window_used(conn, name, 86400)
    return chain_order(usage)


def _deliverability(conn: sqlite3.Connection, cfg=None) -> dict:
    """Bounce and complaint rates, and whether the feedback loop is alive.

    The complaint rate is the number that decides whether large receivers keep
    accepting our mail at all: Gmail and Yahoo publish 0.3% as the line for a
    bulk sender, and a hard-bounce rate above a few percent gets a sending
    domain throttled the same way. Both are measured over 30 days against what
    was actually sent in that window.

    Rates are only as honest as the webhooks feeding them, so
    `providers_silent` reports every configured provider that has not reported
    an event in WEBHOOK_SILENT_HOURS. A rate of 0.00% and a dead webhook look identical in the
    numbers, and the second one is the dangerous state.
    """
    def scalar(q, *args):
        row = conn.execute(q, args).fetchone()
        return row[0] if row else 0

    # Every key the template and `summary_anomalies` read is seeded here, not
    # only on the way out: the `except` below returns this dict as-is on a DB
    # that predates the table, and a missing key is not a blank cell — Jinja
    # hands `'%.2f'|format(...)` an Undefined and the whole admin page 500s.
    # The one instrument for a stalled migration must not be the page that
    # cannot render during one.
    out: dict = {"configured": bool(getattr(cfg, "webhook_secret", "")),
                 "by_reason": {}, "providers": [], "providers_silent": [],
                 "parse_errors": {}, "suppressed": 0, "watchlist": 0,
                 "sent_30d": 0, "complaint_rate": None, "bounce_rate": None,
                 "complaint_30d": 0, "hard_bounce_30d": 0}
    try:
        for r in conn.execute(
            "SELECT reason, COUNT(*) AS n FROM email_suppressions "
            "WHERE reason IS NOT NULL GROUP BY reason"
        ).fetchall():
            out["by_reason"][r["reason"]] = r["n"]
        out["suppressed"] = sum(out["by_reason"].values())
        out["watchlist"] = scalar(
            "SELECT COUNT(*) FROM email_suppressions "
            "WHERE reason IS NULL AND soft_bounces > 0")
        sent_30d = scalar("SELECT COALESCE(SUM(n), 0) FROM email_send_counts "
                          "WHERE day > date('now','-30 days')")
        out["sent_30d"] = sent_30d
        for reason, key in (("complaint", "complaint_rate"),
                            ("hard_bounce", "bounce_rate")):
            n = scalar("SELECT COUNT(*) FROM email_suppressions "
                       "WHERE reason=? AND suppressed_at > datetime('now','-30 days')",
                       reason)
            out[key] = (100.0 * n / sent_30d) if sent_30d else None
            out[reason + "_30d"] = n
    except sqlite3.OperationalError:
        return out  # pre-migration DB

    # Per-provider silence only means something once the loop is switched on;
    # with WEBHOOK_SECRET unset every provider is trivially silent, and
    # reporting that per provider buries the one finding that matters.
    order = (getattr(cfg, "email_provider_order", ()) if cfg else ()) \
        if out["configured"] else ()
    for name in order:
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"last_webhook_at_{name}",)).fetchone()
        last = row["value"] if row else None
        # Silence is only evidence of a broken webhook if mail actually went
        # out through this provider and nothing came back. A leg sitting idle
        # at the end of the fallback chain reports nothing because it sent
        # nothing, and flagging that every day teaches the reader to skip the
        # whole section.
        sent = conn.execute(
            "SELECT COUNT(*) AS n FROM sent_idempotency WHERE provider=? "
            "AND sent_at > datetime('now', ?)",
            (name, f"-{WEBHOOK_SILENT_HOURS} hours"),
        ).fetchone()["n"]
        silent = bool(sent)
        if silent and last:
            try:
                silent = (datetime.utcnow() - datetime.fromisoformat(last)
                          ) > timedelta(hours=WEBHOOK_SILENT_HOURS)
            except ValueError:
                silent = True
        out["providers"].append({"name": name, "last_event_at": last,
                                 "sent_recently": sent, "silent": silent})
        if silent:
            out["providers_silent"].append(name)
        err = conn.execute("SELECT value FROM meta WHERE key=?",
                           (f"webhook_errors_{name}",)).fetchone()
        if err:
            out["parse_errors"][name] = err["value"]
    return out

def stats(conn: sqlite3.Connection, cfg=None) -> dict:
    from app.mail import deferral_walls_today, last_deferral

    def scalar(q, *args):
        row = conn.execute(q, args).fetchone()
        return row[0] if row else 0

    # The per-subscriber daily cap and what it is doing: who is capped right
    # now, who it held today, and the number it exists to move — digests per
    # notified subscriber over the last 24h (was 4.6 with 57 of 103 at 5+
    # when it was introduced).
    sub_cap = getattr(cfg, "max_digests_per_subscriber_per_day", 0) or 0
    capped_now = scalar(
        "SELECT COUNT(*) FROM (SELECT subscription_id FROM digest_deliveries "
        "WHERE sent_at > datetime('now','-24 hours') "
        "GROUP BY subscription_id HAVING COUNT(*) >= ?)", sub_cap) if sub_cap else 0
    row = conn.execute(
        "SELECT COUNT(DISTINCT subscription_id) AS subs, COUNT(*) AS digests "
        "FROM digest_deliveries WHERE sent_at > datetime('now','-24 hours')"
    ).fetchone()
    digests_per_sub = {
        "subs": row["subs"], "digests": row["digests"],
        "mean": round(row["digests"] / row["subs"], 1) if row["subs"] else 0,
    }

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
    city_names: dict[str, str] = {}
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
        name = cat.display_text("city_name", "en")
        if name:
            city_names[c] = name
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
    # is the truest "a subscriber was served" signal — distinct from the emails-sent
    # counters, which also count confirmations, heartbeats and these summary emails.
    notif = conn.execute(
        "SELECT id, last_notified_at FROM subscriptions "
        "WHERE last_notified_at IS NOT NULL ORDER BY last_notified_at DESC LIMIT 1"
    ).fetchone()
    last_notification = ({"sub_id": notif["id"], "at": notif["last_notified_at"]}
                         if notif else None)
    # One row per tenant for the dashboard's city table, sorted by active subs
    # so the tenants that matter are on top — with 30 tenants the page can no
    # longer afford a card per city, and a template assembling this from five
    # separate dicts was the messier place to do it.
    cities = []
    for c in set(list(by_city_subs) + list(upstream_by_city)
                 + list(last_polled_at_by_city) + list(by_city_plans)):
        up = upstream_by_city.get(c, {})
        cities.append({
            "key": c,
            "name": city_names.get(c) or city_labels.get(c, c.capitalize()),
            "sub": None,
            "label": city_labels.get(c, c.capitalize()),
            "subs": by_city_subs.get(c, 0),
            "plans": by_city_plans.get(c, 0),
            "polls_today": up.get("polls_today", 0),
            "polls_total": up.get("polls_total", 0),
            "requests_today": up.get("requests_today", 0),
            "requests_total": up.get("requests_total", 0),
            "last_polled_at": last_polled_at_by_city.get(c),
            "zero_match_since": canary.get(c),
        })
    # Two tenants can share one geographic name (leipzig / leipzig-abh are both
    # "Leipzig") — give colliding rows the label's descriptor as a sub-line so
    # the table stays scannable without repeating the full label everywhere.
    name_counts: dict[str, int] = {}
    for r in cities:
        name_counts[r["name"]] = name_counts.get(r["name"], 0) + 1
    for r in cities:
        if name_counts[r["name"]] > 1:
            r["sub"] = (r["label"].split(":", 1)[1].strip()
                        if ":" in r["label"] else r["key"])
    cities.sort(key=lambda r: (-r["subs"], r["name"].lower(), r["key"]))
    # Delivery provider mix (7d). A rising fallback share means the Mailjet
    # primary is rejecting sends and the failover is carrying the mail — an
    # early warning.
    provider_7d: dict[str, int] = {}
    for r in conn.execute(
        "SELECT provider, COUNT(*) AS n FROM sent_idempotency "
        "WHERE sent_at > datetime('now','-7 days') AND provider != 'pending' "
        "GROUP BY provider"
    ).fetchall():
        provider_7d[r["provider"]] = r["n"]
    # All-time sends, from the durable counters rather than sent_idempotency —
    # housekeeping prunes that table at 14 days, so counting its rows produced a
    # "total" that silently meant "the last fortnight". email_send_counts only
    # goes back to its 2026-07-01 backfill, so the figure is reported with the
    # month it starts from instead of being passed off as all of history.
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(n), 0) AS n, MIN(day) AS since "
            "FROM email_send_counts"
        ).fetchone()
        emails_recorded = row["n"]
        emails_since = (
            datetime.strptime(row["since"], "%Y-%m-%d").strftime("%b %Y")
            if row["since"] else None
        )
    except (sqlite3.OperationalError, ValueError):
        emails_recorded, emails_since = 0, None  # pre-migration DB
    return {
        "active_subscriptions":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                   "AND confirmed_at IS NOT NULL AND expires_at > CURRENT_TIMESTAMP"),
        # People, not rows: one address may hold several subscriptions. lower()
        # folds rows that predate the subscribe form's lowercasing.
        "active_subscribers":
            scalar("SELECT COUNT(DISTINCT lower(email)) FROM subscriptions "
                   "WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
                   "AND expires_at > CURRENT_TIMESTAMP"),
        "active_subscriptions_by_city": by_city_subs,
        "cities": cities,
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
        "emails_sent_last_7d":
            scalar("SELECT COUNT(*) FROM sent_idempotency "
                   "WHERE sent_at > datetime('now','-7 days') "
                   "AND provider != 'pending'"),
        "upstream_by_city": upstream_by_city,
        "upstream_by_host": upstream_by_host,
        "city_labels": city_labels,
        "city_names": city_names,
        "last_polled_at_by_city": last_polled_at_by_city,
        "slots_cached": scalar("SELECT COUNT(*) FROM slots_cache"),
        "deliverability": _deliverability(conn, cfg),
        "emails_sent_recorded": emails_recorded,
        "emails_sent_since": emails_since,
        "notifications_24h":
            scalar("SELECT COUNT(*) FROM subscriptions "
                   "WHERE last_notified_at > datetime('now','-1 day')"),
        "notifications_7d":
            scalar("SELECT COUNT(*) FROM subscriptions "
                   "WHERE last_notified_at > datetime('now','-7 days')"),
        "subscribers_ever_notified":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE last_notified_at IS NOT NULL"),
        # Expired without answering the check-in: no digests, /renew still
        # works until housekeeping deletes them after EXPIRED_GRACE_DAYS.
        "paused_in_grace":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                   "AND confirmed_at IS NOT NULL AND expires_at <= CURRENT_TIMESTAMP"),
        "active_awaiting_first_match":
            scalar("SELECT COUNT(*) FROM subscriptions WHERE deleted_at IS NULL "
                   "AND confirmed_at IS NOT NULL AND expires_at > CURRENT_TIMESTAMP "
                   "AND last_notified_at IS NULL"),
        "last_notification": last_notification,
        "emails_by_provider_7d": provider_7d,
        "email_usage": _email_usage(conn, cfg),
        "deferrals_today":
            scalar("SELECT n FROM email_deferral_counts WHERE day = date('now')"),
        "deferrals_7d":
            scalar("SELECT COALESCE(SUM(n), 0) FROM email_deferral_counts "
                   "WHERE day >= date('now','-7 days')"),
        "last_deferral": last_deferral(conn),
        "deferral_walls_today": deferral_walls_today(conn),
        "subscriber_cap": sub_cap,
        "cap_holds_today":
            scalar("SELECT COUNT(*) FROM digest_cap_holds WHERE day = date('now')"),
        "cap_holds_7d":
            scalar("SELECT COUNT(DISTINCT subscription_id) FROM digest_cap_holds "
                   "WHERE day >= date('now','-7 days')"),
        "capped_now": capped_now,
        "digests_per_sub_24h": digests_per_sub,
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
        if not r["location_uuid"]:
            # Polled, but no office ever produced a slot in the window.
            r["location"] = "all offices"
        else:
            r["location"] = (cat.location_label(r["location_uuid"], "en")
                             if cat else r["location_uuid"])
    return rows
