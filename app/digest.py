from __future__ import annotations
import sqlite3
from app.i18n import t
from app.models import Subscription, Slot
from app.mail import send, _idem_key

def render_digest_text(sub: Subscription, slots: list[Slot], *,
                       unsubscribe_url: str, public_base_url: str,
                       kofi_url: str) -> str:
    lang = sub.language
    lines = [t(lang, "digest.greeting"), "", t(lang, "digest.intro"), ""]
    # Group by day
    by_day: dict[str, list[Slot]] = {}
    for s in slots:
        by_day.setdefault(s.date, []).append(s)
    for day in sorted(by_day):
        lines.append(day)
        for s in by_day[day]:
            go_url = f"{public_base_url}/go/{s.booking_token}"
            lines.append(f"  {s.time_str}  →  {go_url}")
        lines.append("")
    lines.append(t(lang, "digest.burst_warning"))
    lines.append("")
    lines.append(t(lang, "digest.unsubscribe", unsubscribe_url=unsubscribe_url))
    lines.append("")
    lines.append(t(lang, "digest.kofi", kofi_url=kofi_url))
    return "\n".join(lines)

def send_digest(*, conn: sqlite3.Connection, subscription: Subscription,
                matched_slots: list[Slot], cycle_id: str, cfg) -> None:
    """Send a digest. `cfg` is the loaded Config (passed in by callers
    that already have it loaded — never re-read from os.environ here)."""
    from app.tokens import sign
    unsub_token = sign(subscription.id, "unsubscribe",
                       primary=cfg.token_secret_primary,
                       previous=cfg.token_secret_previous)
    unsub_url = f"{cfg.public_base_url}/unsubscribe/{unsub_token}"
    body = render_digest_text(subscription, matched_slots,
                              unsubscribe_url=unsub_url,
                              public_base_url=cfg.public_base_url,
                              kofi_url=cfg.kofi_url)
    subj = t(subscription.language, "digest.subject")
    key = _idem_key(subscription.id,
                    [s.hash() for s in matched_slots],
                    cycle_id)
    send(conn, subscription.email, subj, body, idem_key=key)
