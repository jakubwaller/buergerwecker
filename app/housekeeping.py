from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from app.config import load_config
from app import mail as _mail
from app.mail import _idem_key
from app.tokens import sign
from app.config import ttl_days_for

def mail_send(*args, **kwargs):
    # Indirect through the module so tests can patch `app.mail.send`.
    return _mail.send(*args, **kwargs)

def run_once(conn: sqlite3.Connection) -> None:
    cfg = load_config()
    _purge_hard(conn)
    _clamp_expiry(conn, cfg)
    _soft_delete_expired(conn, cfg)
    _send_renewal_reminders(conn, cfg)
    _send_heartbeats(conn, cfg, milestone_days=30, milestone_col="heartbeat_30d_at")
    _send_heartbeats(conn, cfg, milestone_days=60, milestone_col="heartbeat_60d_at")
    _prune_seen_slots(conn)
    _prune_idempotency(conn)
    _prune_deferrals(conn)
    _prune_digest_deliveries(conn)
    _prune_cap_holds(conn)
    _prune_email_failures(conn)
    _prune_suppressions(conn, cfg)
    _prune_slots_cache(conn)
    _prune_availability(conn)
    _check_parser_canary(conn, cfg)
    _check_backup_health(conn, cfg)
    _start_catalog_sync(cfg)
    _send_summary_email(conn, cfg)
    conn.execute(
        "INSERT INTO meta (key,value) VALUES ('last_housekeeping_at', ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (datetime.utcnow().isoformat(),),
    )

def _purge_hard(conn):
    conn.execute("DELETE FROM subscriptions "
                 "WHERE deleted_at IS NOT NULL "
                 "AND deleted_at < datetime('now','-30 days')")

def _clamp_expiry(conn, cfg):
    """The configured term is a ceiling on everyone's remaining time, not just
    a default for new sign-ups. Lowering SUBSCRIPTION_TTL_DAYS in .env would
    otherwise leave every existing subscription on its old, longer term — the
    dead weight the shorter term exists to shed — until it ran out on its
    own. Only ever pulls an expiry in; a raised term does not extend anyone
    without them clicking through the check-in."""
    for sensitive, cond in ((False, "consent_special_at IS NULL"),
                            (True, "consent_special_at IS NOT NULL")):
        days = f"+{ttl_days_for(cfg, sensitive)} days"
        conn.execute(
            f"UPDATE subscriptions SET expires_at=datetime('now', ?) "
            f"WHERE deleted_at IS NULL AND {cond} "
            f"AND expires_at > datetime('now', ?)",
            (days, days),
        )

def _soft_delete_expired(conn, cfg):
    """Expiry pauses; deletion comes EXPIRED_GRACE_DAYS later. An expired
    subscription gets no digests (repo.active_subscriptions filters on
    expires_at) but its `/renew` link still works, so the check-in's "weiter"
    link is not dead the morning after the deadline in the mail."""
    conn.execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP "
                 "WHERE deleted_at IS NULL AND expires_at < datetime('now', ?)",
                 (f"-{cfg.expired_grace_days} days",))

def _send_renewal_reminders(conn, cfg):
    """The still-looking check-in. A subscription's term is short on purpose:
    most people never click "Abmelden" after they have booked, and a booked
    person drawing two digests a day is both wasted quota and the shape of
    mail that draws spam complaints. So shortly before the term ends we ask —
    one mail, two one-click answers — and doing nothing means "I'm done":
    digests stop at expiry. The "weiter" link keeps working for
    EXPIRED_GRACE_DAYS after that (see _soft_delete_expired).

    reminder_sent_at is the once-per-term latch; /renew clears it, so every
    term asks exactly once. The idempotency key carries the expiry date for
    the same reason — the bare id would collide with the previous term's key
    while it is still in sent_idempotency."""
    from app.catalog import city_display_name
    from app.db import transaction
    rows = conn.execute(
        "SELECT id, email, language, city, expires_at FROM subscriptions "
        "WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
        "AND reminder_sent_at IS NULL "
        "AND expires_at BETWEEN CURRENT_TIMESTAMP AND datetime('now', ?)",
        (f"+{cfg.renewal_reminder_days_before} days",),
    ).fetchall()
    for row in rows:
        lang = "en" if row["language"] == "en" else "de"
        links = {}
        for purpose in ("renew", "unsubscribe", "manage"):
            tok = sign(row["id"], purpose,
                       primary=cfg.token_secret_primary,
                       previous=cfg.token_secret_previous)
            links[purpose] = f"{cfg.public_base_url}/{purpose}/{tok}"
        subj, body = _checkin_mail(
            lang, city=city_display_name(row["city"], lang),
            expires_at=row["expires_at"], grace_days=cfg.expired_grace_days,
            renew_url=links["renew"], unsub_url=links["unsubscribe"],
            manage_url=links["manage"])
        try:
            with transaction(conn):
                # Mark sent BEFORE the API call: the idempotency table in
                # mail.py prevents a second provider call, and this transaction
                # ensures the reminder_sent_at flag is visible even if the
                # process is killed right after the API call returns.
                conn.execute("UPDATE subscriptions SET reminder_sent_at=CURRENT_TIMESTAMP "
                             "WHERE id=?", (row["id"],))
                mail_send(conn, row["email"], subj, body,
                          idem_key=_idem_key(row["id"], [],
                                             f"renewal-{row['id']}-{row['expires_at'][:10]}"),
                          unsub_url=links["unsubscribe"])
        except Exception:
            # transaction rolled back; the row is eligible for retry next pass.
            pass

def _checkin_mail(lang: str, *, city: str | None, expires_at: str,
                  grace_days: int, renew_url: str, unsub_url: str,
                  manage_url: str) -> tuple[str, str]:
    """Subject and plain-text body of the still-looking check-in."""
    stop = datetime.fromisoformat(expires_at[:19]).date()
    resume_until = stop + timedelta(days=grace_days)
    if lang == "en":
        where = f" in {city}" if city else ""
        fmt = lambda d: f"{d.day} {d.strftime('%B %Y')}"  # noqa: E731
        subj = f"Still looking for an appointment{where}?"
        body = (
            f"Still looking for an appointment{where}?\n"
            f"\n"
            f"Yes, keep looking: {renew_url}\n"
            f"No, I've got one: {unsub_url}\n"
            f"\n"
            f"If you do nothing, the notifications stop on {fmt(stop)}. "
            f"Until {fmt(resume_until)} the first link switches them back on.\n"
            f"\n"
            f"Only certain days or times? Adjust your filter: {manage_url}\n"
        )
    else:
        where = f" in {city}" if city else ""
        fmt = lambda d: d.strftime("%d.%m.%Y")  # noqa: E731
        subj = f"Suchst du noch einen Termin{where}?"
        body = (
            f"Suchst du noch einen Termin{where}?\n"
            f"\n"
            f"Ja, weiter suchen: {renew_url}\n"
            f"Nein, ich habe einen: {unsub_url}\n"
            f"\n"
            f"Ohne Antwort stoppen die Benachrichtigungen am {fmt(stop)}. "
            f"Bis {fmt(resume_until)} kannst du sie mit dem ersten Link wieder "
            f"einschalten.\n"
            f"\n"
            f"Nur bestimmte Tage oder Zeiten? Filter anpassen: {manage_url}\n"
        )
    return subj, body

def _send_heartbeats(conn, cfg, *, milestone_days: int, milestone_col: str):
    # Send to subscribers who are past the milestone age AND haven't been
    # notified recently. "Recently" = within the milestone window, so a
    # subscriber notified once 5 days after signup still gets a heartbeat
    # at day 30 if no further notifications happened in between.
    rows = conn.execute(
        f"SELECT id, email, language FROM subscriptions "
        f"WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
        f"AND expires_at > CURRENT_TIMESTAMP "
        f"AND {milestone_col} IS NULL "
        f"AND (last_notified_at IS NULL "
        f"     OR last_notified_at < datetime('now','-{milestone_days} days')) "
        f"AND confirmed_at < datetime('now','-{milestone_days} days')"
    ).fetchall()
    for row in rows:
        manage_tok = sign(row["id"], "manage",
                          primary=cfg.token_secret_primary,
                          previous=cfg.token_secret_previous)
        manage_url = f"{cfg.public_base_url}/manage/{manage_tok}"
        unsub_tok = sign(row["id"], "unsubscribe",
                         primary=cfg.token_secret_primary,
                         previous=cfg.token_secret_previous)
        unsub_url = f"{cfg.public_base_url}/unsubscribe/{unsub_tok}"
        body = (f"Du bist weiterhin abonniert — dein Filter passt einfach noch nicht. "
                f"Hier verwalten: {manage_url}"
                if row["language"] == "de" else
                f"You're still subscribed — your filter just hasn't matched yet. "
                f"Manage here: {manage_url}")
        subj = ("Abo-Update" if row["language"] == "de"
                else "Subscription check-in")
        from app.db import transaction
        try:
            with transaction(conn):
                conn.execute(
                    f"UPDATE subscriptions SET {milestone_col}=CURRENT_TIMESTAMP "
                    f"WHERE id=?",
                    (row["id"],),
                )
                mail_send(conn, row["email"], subj, body,
                          idem_key=_idem_key(row["id"], [],
                                             f"heartbeat-{milestone_days}d-{row['id']}"),
                          unsub_url=unsub_url)
        except Exception:
            pass

def _prune_seen_slots(conn):
    conn.execute("DELETE FROM seen_slots WHERE sent_at < datetime('now','-7 days')")

def _prune_idempotency(conn):
    conn.execute("DELETE FROM sent_idempotency WHERE sent_at < datetime('now','-14 days')")

def _prune_deferrals(conn):
    # The event log is one row per deferring cycle — bounded by the poll
    # cadence, but unbounded in time. The per-day counter keeps the totals.
    conn.execute("DELETE FROM email_deferrals WHERE at < datetime('now','-90 days')")

def _prune_digest_deliveries(conn):
    # The cap reads a rolling 24h; a week keeps /admin's per-subscriber
    # volume readable without holding a per-digest row forever.
    conn.execute("DELETE FROM digest_deliveries "
                 "WHERE sent_at < datetime('now','-7 days')")

def _prune_cap_holds(conn):
    conn.execute("DELETE FROM digest_cap_holds WHERE day < date('now','-90 days')")

def _prune_email_failures(conn):
    """The bounce counter is keyed by the address itself, so it has to age out
    on the same clock the address does. `_purge_hard` above deletes a
    subscription 30 days after it was deleted; without this, its row here
    outlived it forever — a bare e-mail address with nothing left to be
    attached to, still dumped into every nightly backup, against a privacy
    policy that promises final removal 30 days after deletion.

    Depends on `_purge_hard` having run first in `run_once`. NOT EXISTS rather
    than NOT IN: the latter deletes nothing at all if any subscription e-mail
    is ever NULL."""
    conn.execute("DELETE FROM email_failures WHERE NOT EXISTS ("
                 "SELECT 1 FROM subscriptions s WHERE s.email = email_failures.email)")

def _prune_suppressions(conn, cfg=None):
    """Bounce suppressions age out on the same clock as `email_failures`; spam
    complaints never do.

    A bounce is a claim with a shelf life — it says the mailbox does not exist
    *today*. Domains get fixed and typos get corrected, so a stale bounce
    suppression silently blocks someone who is reachable again. Letting it die
    with the subscription costs at most one bounced message if that person ever
    signs up again (their own action, gated by a double opt-in), and keeps the
    privacy promise simple: the row is a bare e-mail address and must not
    outlive the subscription that justified storing it.

    A complaint is not a claim about a mailbox, it is a person telling their
    provider we are spam. Re-mailing them is the single most damaging thing
    this service can do to its sending domain, so a complaint runs on its own
    clock (COMPLAINT_RETENTION_DAYS, a year by default) rather than on the
    subscription's — otherwise the suppression that matters most would be the
    one most easily lost, since feedback loops report late and a complaint can
    arrive for an address whose subscription was already purged.

    Not indefinite, though: Art. 5(1)(e) wants a stated period, and this
    service is double opt-in only, so a lapsed entry can at worst cost one
    confirmation mail to somebody who went back to the site and asked for it.
    Coming back before the year is up goes through a human — the sign-up form
    says so rather than silently swallowing the confirmation (see
    web.subscribe and docs/DEPLOY.md).

    Depends on `_purge_hard` having run first in `run_once`. NOT EXISTS rather
    than NOT IN, per `_prune_email_failures`."""
    days = getattr(cfg, "complaint_retention_days", 365) if cfg else 365
    conn.execute(
        "DELETE FROM email_suppressions WHERE "
        "  (reason = 'complaint' AND suppressed_at < datetime('now', ?)) "
        "  OR ((reason IS NULL OR reason != 'complaint') AND NOT EXISTS ("
        "        SELECT 1 FROM subscriptions s "
        "        WHERE s.email = email_suppressions.email))",
        (f"-{int(days)} days",),
    )

def _prune_slots_cache(conn):
    # Slots are short-lived in the upstream system; 14 days is generous.
    conn.execute("DELETE FROM slots_cache WHERE cached_at < datetime('now','-14 days')")

def _prune_availability(conn):
    # Analytics history; the admin page only ever looks back a few weeks.
    from app.analytics import prune_availability
    prune_availability(conn)

def _check_parser_canary(conn, cfg):
    """Email developer if any city has been all-zero for > threshold during business hours."""
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    # Skip outside typical-load hours (08:00–20:00 Europe/Berlin ≈ 06:00–18:00 UTC)
    if not (6 <= now.hour <= 18):
        return
    threshold = timedelta(hours=cfg.parser_canary_threshold_hours)
    rows = conn.execute(
        "SELECT city, zero_match_since, last_canary_alert_at "
        "FROM city_state WHERE zero_match_since IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            since = datetime.fromisoformat(row["zero_match_since"])
        except (TypeError, ValueError):
            continue
        if now - since < threshold:
            continue
        city = row["city"]
        if row["last_canary_alert_at"]:
            try:
                last = datetime.fromisoformat(row["last_canary_alert_at"])
                if now - last < timedelta(hours=24):
                    continue
            except ValueError:
                pass
        body = (f"Parser canary: city '{city}' has produced zero matches "
                f"since {row['zero_match_since']} "
                f"(> {cfg.parser_canary_threshold_hours}h).")
        try:
            mail_send(conn, cfg.developer_email,
                      f"[buergerwecker] parser canary: {city}",
                      body,
                      idem_key=_idem_key(0, [], f"canary-{city}-{now.date()}"))
            conn.execute(
                "UPDATE city_state SET last_canary_alert_at=? WHERE city=?",
                (now.isoformat(), city),
            )
        except Exception:
            pass

def _check_backup_health(conn, cfg):
    """Alert if backup hasn't written meta.last_backup_at in > 48h, OR if
    the backup container left a BACKUP-FAIL / BACKUP-METAFAIL sentinel."""
    from datetime import datetime, timedelta
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_backup_at'"
    ).fetchone()
    stale = True
    if row:
        try:
            last = datetime.fromisoformat(row["value"].rstrip("Z"))
            stale = (datetime.utcnow() - last) > timedelta(hours=48)
        except ValueError:
            pass
    if not stale:
        return
    try:
        mail_send(conn, cfg.developer_email,
                  "[buergerwecker] backup is stale",
                  "meta.last_backup_at is missing or older than 48h. "
                  "Check the backup container logs and /mnt/backup for "
                  "BACKUP-FAIL / BACKUP-METAFAIL sentinel files.",
                  idem_key=_idem_key(0, [], f"backup-stale-{datetime.utcnow().date()}"))
    except Exception:
        pass

_sync_thread: threading.Thread | None = None

def _start_catalog_sync(cfg) -> None:
    """Run _sync_catalogs on its own thread, with its own connection.

    Housekeeping runs inline in the poller's single loop, and the sync walks
    every tenant against its live portal — TEVIS sleeps half a second per
    service, Smart-CJM drives a three-step wizard per service — so done inline
    it stopped polling for minutes once a day, and nothing noticed: no cycle
    failed, the canary saw nothing, a slot that came and went in that window
    was simply never reported. The thread is a daemon (the sync writes
    atomically, so a stop mid-run leaves no half file) and a run still going
    when the next day's housekeeping fires is left alone rather than doubled.
    """
    global _sync_thread
    if not cfg.catalog_sync_enabled:
        return
    if _sync_thread is not None and _sync_thread.is_alive():
        print("catalog sync: previous run still in progress, skipping", flush=True)
        return

    def _run():
        from app.db import connect
        conn = connect(cfg.db_path)
        try:
            _sync_catalogs(conn, cfg)
        except Exception as exc:
            print(f"catalog sync failed: {exc!r}", flush=True)
        finally:
            conn.close()

    _sync_thread = threading.Thread(target=_run, name="catalog-sync", daemon=True)
    _sync_thread.start()

def wait_for_catalog_sync(timeout: float | None = None) -> None:
    """Block until the background sync (if any) is done. For tests."""
    if _sync_thread is not None:
        _sync_thread.join(timeout)

def _sync_catalogs(conn, cfg):
    """Refresh per-city catalog files from live APIs. Alerts developer on drift.

    Gated by CATALOG_SYNC_ENABLED so test environments don't make network calls.
    Failures here must never crash the daily run.
    """
    if not cfg.catalog_sync_enabled:
        return
    import requests
    from app import catalog_sync
    root = Path(__file__).resolve().parent.parent / "catalog"
    if not root.is_dir():
        return

    def _alert(*, city, service_drift, location_drift):
        lines = [f"Catalog drift detected for {city}.", ""]
        if service_drift:
            lines.append("Services:")
            lines.append(json.dumps(service_drift, ensure_ascii=False, indent=2))
        if location_drift:
            lines.append("Locations:")
            lines.append(json.dumps(location_drift, ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("Catalog files have been overwritten on disk with live values.")
        body = "\n".join(lines)
        try:
            mail_send(conn, cfg.developer_email,
                      f"[buergerwecker] catalog drift: {city}",
                      body,
                      idem_key=_idem_key(0, [],
                                         f"catalog-drift-{city}-{datetime.utcnow().date()}"))
        except Exception:
            pass

    http = requests.Session()
    for city_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            catalog_sync.sync_city(city_dir.name, http, alert_fn=_alert)
        except Exception:
            pass


def _send_summary_email(conn, cfg):
    """Exception-based ops mail: send only when something is unusual, plus a
    weekly all-clear so silence stays trustworthy. On a quiet, non-heartbeat
    day this sends nothing. Full detail always lives on /admin."""
    from app.admin import stats, summary_anomalies, render_summary_email
    now = datetime.utcnow()
    s = stats(conn, cfg)
    anomalies = summary_anomalies(s, now=now)

    # Weekly heartbeat gate. Any summary mail (anomaly OR heartbeat) resets the
    # clock, so a busy week never also gets a redundant all-clear.
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_summary_email_at'"
    ).fetchone()
    last_sent = None
    if row:
        try:
            last_sent = datetime.fromisoformat(row["value"])
        except ValueError:
            pass
    heartbeat_due = last_sent is None or (now - last_sent) >= timedelta(days=7)

    if not anomalies and not heartbeat_due:
        return  # quiet day, nothing to say

    body = render_summary_email(s, now=now, anomalies=anomalies,
                                base_url=cfg.public_base_url)
    if anomalies:
        n = len(anomalies)
        subject = f"[buergerwecker] ops: {n} thing{'s' if n != 1 else ''} need a look"
        tag = "anomaly"
    else:
        subject = "[buergerwecker] weekly all-clear"
        tag = "heartbeat"
    try:
        mail_send(conn, cfg.developer_email, subject, body,
                  idem_key=_idem_key(0, [], f"summary-{tag}-{now.date()}"))
        conn.execute(
            "INSERT INTO meta (key,value) VALUES ('last_summary_email_at', ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
            "updated_at=CURRENT_TIMESTAMP",
            (now.isoformat(),),
        )
    except Exception:
        pass
