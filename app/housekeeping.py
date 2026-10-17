from __future__ import annotations
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from app.config import load_config
from app import mail as _mail
from app.mail import Outgoing, _idem_key
from app.tokens import sign
from app.config import ttl_days_for

def mail_send(*args, **kwargs):
    # Indirect through the module so tests can patch `app.mail.send`.
    return _mail.send(*args, **kwargs)

def run_once(conn: sqlite3.Connection) -> None:
    cfg = load_config()
    failed: list[str] = []

    def step(fn, *args, **kwargs):
        # One broken step must not starve the rest, nor stop the stamp below.
        try:
            fn(*args, **kwargs)
            return True
        except Exception as exc:
            msg = f"housekeeping step {fn.__name__} failed: {exc!r}"
            print(msg, flush=True)
            failed.append(msg)
            return False

    step(_purge_hard, conn)
    step(_clamp_expiry, conn, cfg)
    step(_soft_delete_expired, conn, cfg)
    step(_send_renewal_reminders, conn, cfg)
    step(_send_push_checkins, conn, cfg)
    step(_send_heartbeats, conn, cfg, milestone_days=30, milestone_col="heartbeat_30d_at")
    step(_send_heartbeats, conn, cfg, milestone_days=60, milestone_col="heartbeat_60d_at",
         not_right_after="heartbeat_30d_at")
    step(_prune_seen_slots, conn)
    step(_prune_idempotency, conn)
    step(_prune_deferrals, conn)
    step(_prune_digest_deliveries, conn)
    step(_prune_cap_holds, conn)
    step(_prune_email_failures, conn)
    step(_prune_suppressions, conn, cfg)
    step(_prune_push_devices, conn)
    step(_prune_rate_records, conn)
    step(_prune_availability, conn)
    step(_prune_slot_snapshots, conn)
    # The parser canary is not here: the poller runs it every cycle.
    step(_check_backup_health, conn, cfg)
    step(_start_catalog_sync, cfg)
    if not step(_send_summary_email, conn, cfg, failed=tuple(failed)):
        # The summary is the only report of the failures above, so send them bare.
        step(mail_send, conn, cfg.developer_email, "[buergerwecker] housekeeping failed",
             "\n".join(failed),
             idem_key=_idem_key(0, [], f"housekeeping-failed-{datetime.utcnow().date()}"))
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
        # App subscriptions get the same question as a push, see
        # _send_push_checkins.
        "SELECT id, email, language, city, expires_at FROM subscriptions "
        "WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
        "AND device_id IS NULL "
        "AND reminder_sent_at IS NULL "
        "AND expires_at BETWEEN CURRENT_TIMESTAMP AND datetime('now', ?)",
        (f"+{cfg.renewal_reminder_days_before} days",),
    ).fetchall()
    items, by_key = [], {}
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
        item = Outgoing(
            to=row["email"], subject=subj, body=body,
            idem_key=_idem_key(row["id"], [],
                               f"renewal-{row['id']}-{row['expires_at'][:10]}"),
            unsub_url=links["unsubscribe"])
        items.append(item)
        by_key[item.idem_key] = row["id"]
    # Sent outside any transaction: a provider call can take 30 s, and the
    # write lock must not be held for it. Only delivered mails stamp the latch;
    # what the quota defers is asked again tomorrow, inside the same window.
    result = _mail.send_batch(conn, items, cfg)
    delivered = [by_key[k] for k in _went_out(conn, result, by_key)]
    if delivered:
        with transaction(conn):
            conn.executemany(
                "UPDATE subscriptions SET reminder_sent_at=CURRENT_TIMESTAMP "
                "WHERE id=?", [(sid,) for sid in delivered])

def _went_out(conn, result, by_key) -> set[str]:
    """The keys delivered now, plus those an earlier run sent but never
    latched (a restart between the send and the UPDATE): send_batch skips
    those, and without the latch they would go out again once
    _prune_idempotency forgets the key."""
    if not by_key:
        return set()
    marks = ",".join("?" * len(by_key))
    return set(result.delivered) | {r[0] for r in conn.execute(
        f"SELECT idem_key FROM sent_idempotency WHERE provider != 'pending' "
        f"AND idem_key IN ({marks})", list(by_key))}

def _send_push_checkins(conn, cfg):
    """The still-looking check-in for app subscriptions: the same question
    as the mail, as a push the app answers (keep looking renews over the
    API, no deletes). Same window, same once-per-term latch, same
    idempotency key shape. Only delivered pushes stamp reminder_sent_at: a
    relay outage or a per-token throttle releases the item and the next
    housekeeping run (a day later, with RENEWAL_REMINDER_DAYS_BEFORE days
    of slack) asks again. A retired device gets nothing, and its
    subscriptions are already ended."""
    from app.catalog import city_display_name
    from app.db import transaction
    from app.push import render_checkin, send_push_batch
    from dataclasses import replace
    rows = conn.execute(
        "SELECT s.id, s.device_id, d.token, s.language, s.city, s.expires_at "
        "FROM subscriptions s JOIN push_devices d ON d.id = s.device_id "
        "WHERE s.deleted_at IS NULL AND s.confirmed_at IS NOT NULL "
        "AND s.reminder_sent_at IS NULL AND d.retired_at IS NULL "
        "AND d.verified_at IS NOT NULL "
        "AND s.expires_at BETWEEN CURRENT_TIMESTAMP AND datetime('now', ?)",
        (f"+{cfg.renewal_reminder_days_before} days",),
    ).fetchall()
    if not rows:
        return
    items, by_key = [], {}
    for row in rows:
        lang = "en" if row["language"] == "en" else "de"
        item = render_checkin(lang, sub_id=row["id"],
                              city_name=city_display_name(row["city"], lang),
                              expires_at=row["expires_at"])
        if item is None:
            continue
        item = replace(item, device_id=row["device_id"], token=row["token"])
        items.append(item)
        by_key[item.idem_key] = row["id"]
    try:
        result = send_push_batch(conn, items, cfg)
    except Exception as exc:
        print(f"push: check-in batch failed: {exc!r}", flush=True)
        return
    delivered = [by_key[k] for k in result.delivered if k in by_key]
    if delivered:
        with transaction(conn):
            conn.executemany(
                "UPDATE subscriptions SET reminder_sent_at=CURRENT_TIMESTAMP "
                "WHERE id=?", [(sid,) for sid in delivered])


def _checkin_mail(lang: str, *, city: str | None, expires_at: str,
                  grace_days: int, renew_url: str, unsub_url: str,
                  manage_url: str) -> tuple[str, str]:
    """Subject and plain-text body of the still-looking check-in."""
    from app.i18n import format_date
    stop = datetime.fromisoformat(expires_at[:19]).date()
    resume_until = stop + timedelta(days=grace_days)
    fmt = lambda d: format_date(d, lang)  # noqa: E731
    if lang == "en":
        where = f" in {city}" if city else ""
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

def _heartbeat_mail(lang: str, *, city: str | None, days: int,
                    manage_url: str, unsub_url: str) -> tuple[str, str]:
    """"Still subscribed, nothing matched" — says what the silence means and
    that a narrower filter is the usual reason, with each link labelled."""
    if lang == "en":
        where = f" in {city}" if city else ""
        for_city = f" for {city}" if city else ""
        subj = f"No matching appointments yet{where}"
        body = (
            f"Hello,\n\n"
            f"you are still signed up with Bürgerwecker{for_city}. In the last "
            f"{days} days no appointment matched your filter, so we had "
            f"nothing to send. As soon as one opens up, you will get an "
            f"email.\n\n"
            f"Filter too narrow? Change days, times or locations here:\n\n"
            f"{manage_url}\n\n"
            f"Unsubscribe: {unsub_url}\n\n"
            f"Bürgerwecker\n")
    else:
        where = f" in {city}" if city else ""
        for_city = f" für {city}" if city else ""
        subj = f"Noch keine passenden Termine{where}"
        body = (
            f"Hallo,\n\n"
            f"du bist bei Bürgerwecker weiterhin{for_city} angemeldet. In den "
            f"letzten {days} Tagen war kein Termin frei, der zu deinem Filter "
            f"passt, deshalb kam keine Mail. Sobald einer auftaucht, bekommst "
            f"du eine.\n\n"
            f"Zu eng gefiltert? Tage, Zeiten oder Standorte hier ändern:\n\n"
            f"{manage_url}\n\n"
            f"Abmelden: {unsub_url}\n\n"
            f"Bürgerwecker\n")
    return subj, body

def _send_heartbeats(conn, cfg, *, milestone_days: int, milestone_col: str,
                     not_right_after: str | None = None):
    from app.catalog import city_display_name
    # A row eligible for the 60-day mail is eligible for the 30-day one too,
    # so when both are overdue (housekeeping skipped the day-30 window) the
    # pass before this one has just stamped `not_right_after`. Two heartbeats
    # a second apart, one saying "30 Tage", one "60 Tage", read as a bug;
    # wait a day instead — the 60-day mail then follows on its own.
    gap = (f"AND {not_right_after} < datetime('now','-1 day') "
           if not_right_after else "")
    # Send to subscribers who are past the milestone age AND haven't been
    # notified recently. "Recently" = within the milestone window, so a
    # subscriber notified once 5 days after signup still gets a heartbeat
    # at day 30 if no further notifications happened in between.
    rows = conn.execute(
        f"SELECT id, email, language, city FROM subscriptions "
        f"WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
        f"AND device_id IS NULL "
        f"AND expires_at > CURRENT_TIMESTAMP "
        f"AND {milestone_col} IS NULL {gap}"
        f"AND (last_notified_at IS NULL "
        f"     OR last_notified_at < datetime('now','-{milestone_days} days')) "
        f"AND confirmed_at < datetime('now','-{milestone_days} days')"
    ).fetchall()
    items, by_key = [], {}
    for row in rows:
        manage_tok = sign(row["id"], "manage",
                          primary=cfg.token_secret_primary,
                          previous=cfg.token_secret_previous)
        manage_url = f"{cfg.public_base_url}/manage/{manage_tok}"
        unsub_tok = sign(row["id"], "unsubscribe",
                         primary=cfg.token_secret_primary,
                         previous=cfg.token_secret_previous)
        unsub_url = f"{cfg.public_base_url}/unsubscribe/{unsub_tok}"
        subj, body = _heartbeat_mail(
            "en" if row["language"] == "en" else "de",
            city=city_display_name(row["city"], row["language"]),
            days=milestone_days, manage_url=manage_url, unsub_url=unsub_url)
        item = Outgoing(
            to=row["email"], subject=subj, body=body,
            idem_key=_idem_key(row["id"], [],
                               f"heartbeat-{milestone_days}d-{row['id']}"),
            unsub_url=unsub_url)
        items.append(item)
        by_key[item.idem_key] = row["id"]
    # Outside any transaction, like the renewal reminders; only delivered
    # mails are marked, the rest is picked up by the next daily run.
    result = _mail.send_batch(conn, items, cfg)
    delivered = [by_key[k] for k in _went_out(conn, result, by_key)]
    if delivered:
        from app.db import transaction
        with transaction(conn):
            conn.executemany(
                f"UPDATE subscriptions SET {milestone_col}=CURRENT_TIMESTAMP "
                f"WHERE id=?", [(sid,) for sid in delivered])

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

def _prune_push_devices(conn):
    """A push token is as much a person's data as an address is: it goes on
    the same 30-day clock. A retired device (dead token) is purged 30 days
    after retirement. A live one goes once it has carried no live
    subscription for 30 days and has not registered in 30 days either: the
    app was used once and never again, or every subscription was deleted and
    the device was not heard from since. The app re-registers on its next
    launch, so nothing is lost. ON DELETE CASCADE takes any soft-deleted
    subscriptions along. Depends on `_purge_hard` having run first in
    `run_once`.

    A device that is unverified, was created over a day ago and has had no
    verification request for a day (a verification in progress, e.g. after a
    token change, keeps its row) goes if it has no subscription row at all (deleted or not: a device that ever held one
    keeps the rules above). Its verification code expired with that day, and
    the app registers afresh on its next launch."""
    conn.execute(
        "DELETE FROM push_devices WHERE "
        "  (verified_at IS NULL AND created_at < datetime('now','-1 day') "
        "   AND (verify_requested_at IS NULL "
        "        OR verify_requested_at < datetime('now','-1 day')) "
        "   AND NOT EXISTS (SELECT 1 FROM subscriptions s "
        "                   WHERE s.device_id = push_devices.id)) "
        "  OR (retired_at IS NOT NULL AND retired_at < datetime('now','-30 days')) "
        "  OR (last_seen_at < datetime('now','-30 days') AND NOT EXISTS ("
        "        SELECT 1 FROM subscriptions s "
        "        WHERE s.device_id = push_devices.id "
        "        AND (s.deleted_at IS NULL "
        "             OR s.deleted_at > datetime('now','-30 days'))))")

def _prune_rate_records(conn):
    """The per-token verification record and the cross-worker rate events
    are read over a day at most; older rows can never change a verdict. The
    rate events hold a keyed hash of a client network, so they go as soon as
    they are useless."""
    conn.execute("DELETE FROM verify_attempts WHERE at < datetime('now','-1 day')")
    conn.execute("DELETE FROM rate_events WHERE at < datetime('now','-1 day')")

def _prune_slot_snapshots(conn):
    # A plan nobody subscribes to any more is no longer polled; its snapshot
    # must not keep describing inventory from a day ago.
    from app.snapshots import prune_snapshots
    prune_snapshots(conn)

def _prune_availability(conn):
    # Analytics history; the admin page only ever looks back a few weeks.
    from app.analytics import prune_availability
    prune_availability(conn)

def _check_parser_canary(conn, cfg):
    """Email developer when a scraper looks broken, during business hours.
    Called by the poller every cycle (poller._check_canary). Two alerts, each
    at most once a day: a city with a poll raising in every cycle for > threshold
    (a city-specific break), and a vendor whose polled, non-failing cities
    have all been at zero for > threshold (a break in the shared markup).
    One city at zero alone is just a booked-out service and stays quiet."""
    from app.catalog import CatalogError, load_catalog
    from app.db import sql_ts
    now = datetime.utcnow()
    # Only a poll clears these, so a city nobody polls any more would keep its
    # last values: the canary forever, and a false alarm on the first empty
    # poll once someone subscribes there again.
    from app.repo import active_subscriptions
    polled = sorted({s.city for s in active_subscriptions(conn)})
    conn.execute(
        "UPDATE city_state SET zero_match_since=NULL, failing_since=NULL "
        "WHERE (zero_match_since IS NOT NULL OR failing_since IS NOT NULL) "
        f"AND city NOT IN ({','.join('?' * len(polled))})", polled)
    # Skip outside typical-load hours (08:00–20:00 Europe/Berlin ≈ 06:00–18:00 UTC)
    if not (6 <= now.hour <= 18):
        return
    hours = cfg.parser_canary_threshold_hours
    threshold = timedelta(hours=hours)

    def old_enough(ts):
        try:
            return now - datetime.fromisoformat(ts) >= threshold
        except (TypeError, ValueError):
            return False

    def alert(name, body, last_at, stamp):
        """Mail once a day per `name`; `stamp(ts)` records the attempt."""
        if last_at:
            try:
                if now - datetime.fromisoformat(last_at) < timedelta(hours=24):
                    return
            except ValueError:
                pass
        try:
            mail_send(conn, cfg.developer_email,
                      f"[buergerwecker] parser canary: {name}", body,
                      idem_key=_idem_key(0, [], f"canary-{name}-{now.date()}"))
            stamp(sql_ts(now))
        except Exception as exc:
            print(f"parser canary mail for {name} failed: {exc!r}", flush=True)
            # Retry in an hour, not every cycle: a mail outage would otherwise
            # walk the whole provider chain each minute.
            stamp(sql_ts(now - timedelta(hours=23)))

    rows = conn.execute(
        "SELECT city, zero_match_since, failing_since, last_canary_alert_at "
        "FROM city_state WHERE city IN "
        f"({','.join('?' * len(polled))})", polled).fetchall()
    by_vendor: dict[str, list] = {}
    for row in rows:
        city = row["city"]
        if row["failing_since"]:
            if old_enough(row["failing_since"]):
                alert(city,
                      f"Parser canary: a poll of '{city}' has failed in every cycle since "
                      f"{row['failing_since']} (> {hours}h). The poller log has "
                      f"the errors (lines starting \"cycle <id>: poll failed "
                      f"{city}/\").",
                      row["last_canary_alert_at"],
                      lambda ts, city=city: conn.execute(
                          "UPDATE city_state SET last_canary_alert_at=? "
                          "WHERE city=?", (ts, city)))
            continue
        try:
            vendor = load_catalog(city).scraper_config["vendor"]
        except CatalogError:
            continue  # a removed city
        by_vendor.setdefault(vendor, []).append(row)
    # One city at zero is usually a booked-out service, while a parser break
    # empties every city on that system at once.
    # shortcut: a break that empties one city or host without raising only
    # shows as zero, so it does not alert; group by host or add a days-long
    # zero backstop if one goes unnoticed.
    for vendor, cities in sorted(by_vendor.items()):
        if len(cities) < 2 or not all(old_enough(r["zero_match_since"]) for r in cities):
            continue
        key = f"canary_vendor_{vendor}"
        last = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        alert(vendor,
              f"Parser canary: vendor '{vendor}' has found no slot in "
              f"{', '.join(r['city'] for r in cities)} since "
              f"{max(r['zero_match_since'] for r in cities)} (> {hours}h). "
              "Their pages still load, so the slot markup may have changed.",
              last and last["value"],
              lambda ts, key=key: conn.execute(
                  "INSERT INTO meta (key, value) VALUES (?, ?) "
                  "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
                  "updated_at=CURRENT_TIMESTAMP", (key, ts)))

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
    """Run _sync_catalogs on its own thread.

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
        try:
            _sync_catalogs(cfg)
        except Exception as exc:
            print(f"catalog sync failed: {exc!r}", flush=True)

    _sync_thread = threading.Thread(target=_run, name="catalog-sync", daemon=True)
    _sync_thread.start()

def wait_for_catalog_sync(timeout: float | None = None) -> None:
    """Block until the background sync (if any) is done. For tests."""
    if _sync_thread is not None:
        _sync_thread.join(timeout)

def _sync_catalogs(cfg):
    """Compare every tenant's catalog with its live site and record, in meta
    `catalog_dead_ids` ({city: [ids]}), the service and office ids that live
    subscriptions use and the site no longer has. Those subscribers are polled
    for something that answers nothing: five TEVIS cities renumbered on
    2026-10-10 and went silent for three days. The ops summary and /admin list
    them (admin.summary_anomalies). Drift itself is only logged.

    Nothing is written to catalog/: it is baked into each image, so a write
    here reached neither the web container nor the next deploy, and only split
    the two containers' ids. A catalog change goes through a PR.

    Gated by CATALOG_SYNC_ENABLED so test environments don't make network calls.
    Failures here must never crash the daily run.
    """
    if not cfg.catalog_sync_enabled:
        return
    import json
    import requests
    from app import catalog_sync
    from app.catalog import load_catalog
    from app.db import connect
    from app.repo import subscribed_ids
    root = Path(__file__).resolve().parent.parent / "catalog"
    if not root.is_dir():
        return

    def _alert(*, city, service_drift, location_drift):
        print(f"catalog drift: {city} services={service_drift} "
              f"locations={location_drift}", flush=True)

    # Its own connection: this runs on the catalog-sync thread.
    conn = connect(cfg.db_path)
    row = conn.execute(
        "SELECT value FROM meta WHERE key='catalog_dead_ids'").fetchone()
    try:
        dead = json.loads(row["value"]) if row else {}
    except ValueError:
        dead = {}
    dead = {c: ids for c, ids in dead.items() if (root / c).is_dir()}
    http = requests.Session()
    for city_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        city = city_dir.name
        try:
            live = catalog_sync.sync_city(city, http, alert_fn=_alert,
                                          write=False).get("live_ids")
            if live is None:
                continue  # skipped or failed: keep what the last run found
            cat = load_catalog(city)
            excluded = {str(s) for s in cat.scraper_config
                        .get("exclude_services") or ()}
            # The coverage map, not locations.json: it drops offices only an
            # excluded service uses, which the sync never probes.
            offices = ({u for ls in cat.service_locations.values() for u in ls}
                       or set(cat.locations.values()))
            wanted = subscribed_ids(conn, city, all_offices=offices)
            gone = sorted(wanted - live - excluded)
        except Exception:
            continue
        if gone:
            dead[city] = gone
        else:
            dead.pop(city, None)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('catalog_dead_ids', ?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, "
        "updated_at=CURRENT_TIMESTAMP", (json.dumps(dead, sort_keys=True),))
    conn.close()


def _send_summary_email(conn, cfg, failed=()):
    """Exception-based ops mail: send only when something is unusual, plus a
    weekly all-clear so silence stays trustworthy. On a quiet, non-heartbeat
    day this sends nothing. Full detail always lives on /admin."""
    from app.admin import stats, summary_anomalies, render_summary_email
    now = datetime.utcnow()
    s = stats(conn, cfg)
    anomalies = summary_anomalies(s, now=now) + list(failed)

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
