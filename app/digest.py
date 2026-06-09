from __future__ import annotations
import sqlite3
from datetime import datetime
from app.i18n import t
from app.models import Subscription, Slot
from app.mail import send, _idem_key

# Weekday abbreviations for the date line (i18n.t is string-only, so the
# per-language lists live here rather than in the JSON bundles). Index 0 = Mon.
_WEEKDAY_ABBR = {
    "de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}


def _format_date(date_str: str, lang: str) -> str:
    """'2026-06-12' -> 'Fr 12.06.'. Falls back to the raw string if unparsable."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    abbr = _WEEKDAY_ABBR.get(lang, _WEEKDAY_ABBR["de"])[d.weekday()]
    return f"{abbr} {d.day:02d}.{d.month:02d}."


def render_digest_text(sub: Subscription, slots: list[Slot], *,
                       unsubscribe_url: str, public_base_url: str,
                       kofi_url: str, catalog=None) -> str:
    lang = sub.language
    # Resolve the catalog for uuid->name lookups. This must never block a
    # notification: an unknown city or missing catalog files degrades to
    # showing raw uuids rather than dropping the email.
    if catalog is None:
        try:
            from app.catalog import load_catalog
            catalog = load_catalog(sub.city)
        except Exception:
            catalog = None

    def svc_label(uuid: str) -> str:
        return catalog.appointment_type_label(uuid, lang) if catalog else uuid

    def loc_label(uuid: str) -> str:
        return catalog.location_label(uuid, lang) if catalog else uuid

    lines = [t(lang, "digest.greeting"), "", t(lang, "digest.intro"), ""]

    # "Deine Auswahl" — echo the subscriber's filter (what they selected).
    f = sub.sub_filter
    services = ", ".join(svc_label(u) for u in f.appointment_types)
    if f.locations == "all":
        locations = t(lang, "digest.all_locations")
    else:
        locations = ", ".join(loc_label(u) for u in f.locations)
    svc_lbl = t(lang, "digest.selection_service_label")
    loc_lbl = t(lang, "digest.selection_locations_label")
    pad = max(len(svc_lbl), len(loc_lbl)) + 1  # width of the longest "label:"
    lines.append(t(lang, "digest.selection_heading"))
    lines.append(f"  {(svc_lbl + ':').ljust(pad)} {services}")
    lines.append(f"  {(loc_lbl + ':').ljust(pad)} {locations}")
    lines.append("")

    # Slots grouped by office (offices sorted by display name); within an
    # office, sorted by day then time. The per-slot service label is shown
    # only when the filter spans more than one type — otherwise the header
    # already names the single service and the line stays uncluttered.
    multi_service = len(f.appointment_types) > 1
    by_office: dict[str, list[Slot]] = {}
    for s in slots:
        by_office.setdefault(s.location_uuid, []).append(s)
    for office_uuid in sorted(by_office, key=loc_label):
        lines.append(loc_label(office_uuid))
        for s in sorted(by_office[office_uuid], key=lambda s: (s.date, s.time_str)):
            go_url = f"{public_base_url}/go/{s.booking_token}"
            date_str = _format_date(s.date, lang)
            if multi_service:
                lines.append(f"  {date_str}  {s.time_str}  ·  "
                             f"{svc_label(s.service_uuid)}  →  {go_url}")
            else:
                lines.append(f"  {date_str}  {s.time_str}  →  {go_url}")
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
    that already have it loaded — never re-read from os.environ here).
    render_digest_text loads the per-city catalog itself for label lookups."""
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
