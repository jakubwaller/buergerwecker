from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from app.i18n import t
from app.models import Subscription, Slot
from app.mail import (send, send_batch, maybe_quota_alert, Outgoing,
                      _idem_key)

# Render at most this many slots per digest email (soonest first). Keeps even
# an abundant tenant's digest far under Gmail's ~102KB clipping threshold;
# anything beyond is summarized in a single count line.
MAX_SLOTS_PER_DIGEST = 25

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
    win_lbl = t(lang, "digest.selection_window_label") if f.max_days_ahead else ""
    labels = [l for l in (svc_lbl, loc_lbl, win_lbl) if l]
    pad = max(len(l) for l in labels) + 1  # width of the longest "label:"
    lines.append(t(lang, "digest.selection_heading"))
    lines.append(f"  {(svc_lbl + ':').ljust(pad)} {services}")
    lines.append(f"  {(loc_lbl + ':').ljust(pad)} {locations}")
    if f.max_days_ahead:
        lines.append(f"  {(win_lbl + ':').ljust(pad)} "
                     f"{t(lang, 'digest.window_days', n=f.max_days_ahead)}")
    lines.append("")

    # Cap the rendered slots at the soonest MAX_SLOTS_PER_DIGEST. Abundant
    # tenants (the Ausländerbehörde calendar can hold 1000+ open slots) would
    # otherwise produce a digest past Gmail's ~102KB clipping threshold —
    # hiding the unsubscribe link in the clipped tail. Omitted slots are
    # summarized in one count line; the caller still marks ALL matched slots
    # seen (flush_digests works off the full candidate list), so the omission
    # does not drip-feed follow-up emails.
    omitted = 0
    if len(slots) > MAX_SLOTS_PER_DIGEST:
        omitted = len(slots) - MAX_SLOTS_PER_DIGEST
        slots = sorted(slots, key=lambda s: (s.date, s.time_str))[:MAX_SLOTS_PER_DIGEST]

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
            # Tenant-prefixed to match the slots_cache key (see cycle.py): the
            # bare token is only a datetime and would collide across tenants.
            go_url = f"{public_base_url}/go/{sub.city}:{s.booking_token}"
            date_str = _format_date(s.date, lang)
            if multi_service:
                lines.append(f"  {date_str}  {s.time_str}  ·  "
                             f"{svc_label(s.service_uuid)}  →  {go_url}")
            else:
                lines.append(f"  {date_str}  {s.time_str}  →  {go_url}")
    if omitted:
        lines.append("")
        lines.append(t(lang, "digest.more_available", n=omitted))
    lines.append("")

    lines.append(t(lang, "digest.burst_warning"))
    lines.append("")
    lines.append(t(lang, "digest.unsubscribe", unsubscribe_url=unsubscribe_url))
    lines.append("")
    lines.append(t(lang, "digest.kofi", kofi_url=kofi_url))
    return "\n".join(lines)

@dataclass
class QueuedDigest:
    """A rendered digest staged for batched delivery. Carries the subscription
    and slots so the flush can record seen_slots only for what was delivered."""
    item: Outgoing
    subscription: Subscription
    slots: list[Slot]

def send_digest(*, conn: sqlite3.Connection, subscription: Subscription,
                matched_slots: list[Slot], cycle_id: str, cfg,
                sink: list | None = None) -> None:
    """Render a digest and stage it for delivery. `cfg` is the loaded Config
    (passed in by callers that already have it loaded — never re-read from
    os.environ here). render_digest_text loads the per-city catalog itself.

    With a `sink` list (the normal cycle path), the rendered digest is appended
    for batched delivery via `flush_digests`. Without one, it is delivered
    immediately (used for one-off sends outside a poll cycle)."""
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
    queued = QueuedDigest(
        item=Outgoing(to=subscription.email, subject=subj, body=body,
                      idem_key=key, unsub_url=unsub_url),
        subscription=subscription,
        slots=list(matched_slots),
    )
    if sink is None:
        flush_digests(conn, [queued], cfg)
    else:
        sink.append(queued)

def flush_digests(conn: sqlite3.Connection, sink: list, cfg) -> None:
    """Deliver every staged digest in `sink` via quota-aware batches, then
    record seen_slots + last_notified for the ones that were actually sent.
    Deferred digests are left unrecorded so the next cycle re-sends them."""
    if not sink:
        return
    from app.db import transaction
    from app.repo import record_seen_slot, set_last_notified
    result = send_batch(conn, [q.item for q in sink], cfg)
    for q in sink:
        if q.item.idem_key not in result.delivered:
            continue
        with transaction(conn):
            for slot in q.slots:
                record_seen_slot(conn, q.subscription.id, slot.hash())
            set_last_notified(conn, q.subscription.id)
    maybe_quota_alert(conn, cfg, deferred=result.deferred)
