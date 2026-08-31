"""One-off apology mail for subscriptions that expired without a warning.

The still-looking check-in shipped 2026-08-26 (PR #70). Every term that ended
before that expired the old way: digests just stopped, no mail at all — the
subscriber's picture is a service that died (that is exactly how the first
affected person read it). Anyone still inside EXPIRED_GRACE_DAYS is one
`/renew` click away from turning the digests back on, but never received the
link. This sends it, once, with the apology.

Cohort: confirmed, not deleted, `reminder_sent_at IS NULL` (this term was
never asked), expired, and still inside the grace window. Anyone past the
grace window is left alone — their `/renew` deadline has passed and
housekeeping deletes them on its own schedule. Anyone whose expiry lies ahead
is the check-in's job, not this script's.

Delivery goes through `send_batch`, so the run respects every provider's
remaining quota: what does not fit is deferred with its idempotency claim
released, and a later run picks it up. Delivered subscriptions get
`reminder_sent_at` stamped — the same once-per-term latch the check-in uses,
which `/renew` clears — so a re-run only retries what has not gone out.

Dry run by default; `--send` sends.

    docker compose run --rm poller \
        python scripts/notify_silent_expired.py --db /data/app.db
    docker compose run --rm poller \
        python scripts/notify_silent_expired.py --db /data/app.db --send
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timedelta

# Running `python scripts/notify_silent_expired.py` puts *this* directory on
# sys.path, not the repo root, so `app` would not import — and the poller image
# has no installed copy to fall back on (see backfill_day_keys.py).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.catalog import city_display_name  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import connect  # noqa: E402
from app.i18n import format_date  # noqa: E402
from app.mail import Outgoing, _idem_key, send_batch  # noqa: E402
from app.tokens import sign  # noqa: E402


def cohort(conn, cfg) -> list:
    # The NOT EXISTS drops anyone who already signed up again for the same
    # city: their old row still matches, but reviving it next to the new one
    # would double their digests — and the person plainly needs no invitation
    # back. NOCASE because a re-signup can spell the address differently.
    return conn.execute(
        "SELECT id, email, language, city, expires_at FROM subscriptions "
        "WHERE deleted_at IS NULL AND confirmed_at IS NOT NULL "
        "AND reminder_sent_at IS NULL "
        "AND expires_at < CURRENT_TIMESTAMP "
        "AND expires_at >= datetime('now', ?) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM subscriptions s2 "
        "  WHERE s2.email = subscriptions.email COLLATE NOCASE "
        "  AND s2.city = subscriptions.city AND s2.id != subscriptions.id "
        "  AND s2.deleted_at IS NULL AND s2.expires_at > CURRENT_TIMESTAMP) "
        "ORDER BY expires_at",
        (f"-{cfg.expired_grace_days} days",),
    ).fetchall()


def _apology_mail(lang: str, *, city: str | None, expires_at: str,
                  grace_days: int, renew_url: str,
                  unsub_url: str) -> tuple[str, str]:
    stop = datetime.fromisoformat(expires_at[:19]).date()
    resume_until = stop + timedelta(days=grace_days)
    fmt = lambda d: format_date(d, lang)  # noqa: E731
    where = f" in {city}" if city else ""
    if lang == "en":
        subj = "Your notifications expired, sorry"
        body = (
            f"Your appointment notifications{where} expired on {fmt(stop)}. "
            f"There was no warning beforehand, that was our mistake. Sorry! "
            f"By now a mail asks before the term runs out.\n"
            f"\n"
            f"Still looking?\n"
            f"\n"
            f"Yes, keep looking: {renew_url}\n"
            f"No, I've got one: {unsub_url}\n"
            f"\n"
            f"Until {fmt(resume_until)} the first link switches the "
            f"notifications back on. After that your sign-up is deleted.\n"
        )
    else:
        subj = "Deine Benachrichtigungen sind ausgelaufen, sorry"
        body = (
            f"Deine Termin-Benachrichtigungen{where} sind am {fmt(stop)} "
            f"ausgelaufen. Eine Vorwarnung gab es nicht, das war unser "
            f"Fehler. Sorry! Inzwischen kommt vorher eine Mail mit der "
            f"Frage, ob du noch suchst.\n"
            f"\n"
            f"Suchst du noch?\n"
            f"\n"
            f"Ja, weiter suchen: {renew_url}\n"
            f"Nein, ich habe einen: {unsub_url}\n"
            f"\n"
            f"Bis {fmt(resume_until)} kannst du die Benachrichtigungen mit "
            f"dem ersten Link wieder einschalten. Danach wird deine "
            f"Anmeldung gelöscht.\n"
        )
    return subj, body


def _outgoing(cfg, row) -> Outgoing:
    lang = "en" if row["language"] == "en" else "de"
    links = {}
    for purpose in ("renew", "unsubscribe"):
        tok = sign(row["id"], purpose,
                   primary=cfg.token_secret_primary,
                   previous=cfg.token_secret_previous)
        links[purpose] = f"{cfg.public_base_url}/{purpose}/{tok}"
    subj, body = _apology_mail(
        lang, city=city_display_name(row["city"], lang),
        expires_at=row["expires_at"], grace_days=cfg.expired_grace_days,
        renew_url=links["renew"], unsub_url=links["unsubscribe"])
    # The expiry date in the key for the same reason the check-in carries it:
    # it names the term. Stable across runs, so a re-run inside the
    # sent_idempotency retention cannot double-send; the cohort's grace-window
    # bound is shorter than that retention, so no run can outlive the record.
    return Outgoing(to=row["email"], subject=subj, body=body,
                    idem_key=_idem_key(row["id"], [],
                                       f"expiry-apology-{row['id']}"
                                       f"-{row['expires_at'][:10]}"),
                    unsub_url=links["unsubscribe"])


def run(conn, cfg, *, send: bool) -> dict:
    rows = cohort(conn, cfg)
    stats = {"cohort": len(rows), "delivered": 0, "deferred": 0,
             "undeliverable": 0, "marked": 0, "by_provider": {}}
    if not rows:
        return stats
    for row in rows:
        grace_end = (datetime.fromisoformat(row["expires_at"][:19])
                     + timedelta(days=cfg.expired_grace_days))
        print(f"  #{row['id']:<5} {row['city']:<20} lang={row['language']} "
              f"expired {row['expires_at'][:10]}, "
              f"renewable until {grace_end.date()}")
    if not send:
        return stats

    items = [_outgoing(cfg, row) for row in rows]
    result = send_batch(conn, items, cfg)
    stats["delivered"] = len(result.delivered)
    stats["deferred"] = result.deferred
    stats["undeliverable"] = len(result.undeliverable)
    stats["by_provider"] = result.sent_by_provider

    # Stamp the once-per-term latch by what sent_idempotency records, not by
    # this run's return value: if a previous run died between sending and
    # stamping, its mail is on record and this run must not count the
    # subscriber as unasked forever.
    for row, item in zip(rows, items):
        sent_row = conn.execute(
            "SELECT provider FROM sent_idempotency WHERE idem_key=?",
            (item.idem_key,)).fetchone()
        if sent_row and sent_row["provider"] != "pending":
            cur = conn.execute(
                "UPDATE subscriptions SET reminder_sent_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND reminder_sent_at IS NULL", (row["id"],))
            stats["marked"] += cur.rowcount
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to app.db")
    ap.add_argument("--send", action="store_true",
                    help="send the mails (default: report the cohort only)")
    args = ap.parse_args(argv)

    cfg = load_config()
    conn = connect(args.db)
    # The live poller writes to this DB while the script runs; wait out a held
    # write lock rather than dying on it (connect's default is 5s).
    conn.execute("PRAGMA busy_timeout=30000")
    stats = run(conn, cfg, send=args.send)

    print(f"cohort (expired unwarned, still renewable): {stats['cohort']}")
    if not args.send:
        print("dry run — nothing sent. Re-run with --send.")
        return 0
    by = ", ".join(f"{k}: {v}" for k, v in stats["by_provider"].items())
    print(f"delivered:     {stats['delivered']}" + (f" ({by})" if by else ""))
    print(f"deferred:      {stats['deferred']}")
    print(f"undeliverable: {stats['undeliverable']}")
    print(f"latch stamped: {stats['marked']}")
    if stats["deferred"]:
        print("\nDeferred mails hit a provider quota wall; their claims are "
              "released. Re-run this script once the window frees (check "
              "/admin → Email quota) — it retries exactly the unsent rest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
