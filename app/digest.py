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
                       kofi_url: str, catalog=None,
                       booking_url: str | None = None,
                       manage_url: str | None = None) -> str:
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

    # A subscription to a special-category service (Art. 9 GDPR — STI
    # counselling, an SBGG declaration) must not have that service named back
    # to the subscriber in a mail sitting in an inbox, and neither may the
    # office, which for a single-purpose Amt gives away exactly the same thing.
    # Dates, times and the booking link are unaffected.
    redacted = catalog is not None and any(
        catalog.is_sensitive(u) for u in sub.sub_filter.appointment_types)
    redaction = t(lang, "digest.sensitive_redacted")

    def svc_label(uuid: str) -> str:
        if redacted:
            return redaction
        return catalog.appointment_type_label(uuid, lang) if catalog else uuid

    def loc_label(uuid: str) -> str:
        if redacted:
            return redaction
        return catalog.location_label(uuid, lang) if catalog else uuid

    lines = [t(lang, "digest.greeting"), "", t(lang, "digest.intro"), ""]

    # "Deine Auswahl" — echo the subscriber's filter (what they selected).
    f = sub.sub_filter
    services = (redaction if redacted
                else ", ".join(svc_label(u) for u in f.appointment_types))
    if f.locations == "all":
        locations = t(lang, "digest.all_locations")
    elif redacted:
        locations = redaction
    else:
        locations = ", ".join(loc_label(u) for u in f.locations)
    city_name = catalog.display_text("city_name", lang) if catalog else None
    city_lbl = t(lang, "digest.selection_city_label") if city_name else ""
    svc_lbl = t(lang, "digest.selection_service_label")
    loc_lbl = t(lang, "digest.selection_locations_label")
    win_lbl = t(lang, "digest.selection_window_label") if f.max_days_ahead else ""
    labels = [l for l in (city_lbl, svc_lbl, loc_lbl, win_lbl) if l]
    pad = max(len(l) for l in labels) + 1  # width of the longest "label:"
    lines.append(t(lang, "digest.selection_heading"))
    if city_name:
        lines.append(f"  {(city_lbl + ':').ljust(pad)} {city_name}")
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
    #
    # Times are plain text — no per-slot links. Both current vendors bind
    # booking to a browser session (see catalog.booking_start_url), so a
    # per-slot link could only ever land on the start page while looking
    # like a deep link; one honest booking link below the list replaces it.
    # Redacted digests drop the grouping entirely rather than repeat one
    # placeholder as every office header — a flat list of dates and times.
    multi_service = len(f.appointment_types) > 1 and not redacted
    by_office: dict[str, list[Slot]] = {}
    for s in slots:
        by_office.setdefault("" if redacted else s.location_uuid, []).append(s)
    for office_uuid in sorted(by_office, key=loc_label):
        if not redacted:
            lines.append(loc_label(office_uuid))
        for s in sorted(by_office[office_uuid], key=lambda s: (s.date, s.time_str)):
            date_str = _format_date(s.date, lang)
            if multi_service:
                lines.append(f"  {date_str}  {s.time_str}  ·  "
                             f"{svc_label(s.service_uuid)}")
            else:
                lines.append(f"  {date_str}  {s.time_str}")
    if omitted:
        lines.append("")
        lines.append(t(lang, "digest.more_available", n=omitted))
    lines.append("")

    # One booking link per digest: /go/<city> resolves to the city's booking
    # start page at click time. The instruction names the subscriber's own
    # selection so they can re-select it there; single-location tenants
    # (e.g. leipzig-abh) have no location step, so the location clause is
    # dropped for them.
    # `/go/<city>` spells the tenant slug out in the URL, which for a
    # special-category Amt undoes the redaction above — those digests get an
    # opaque `booking_url` from the caller instead.
    go_url = booking_url or f"{public_base_url}/go/{sub.city}"
    if lang == "en":
        go_url += "?lang=en"
    lines.append(t(lang, "digest.book_link", url=go_url))
    if redacted:
        lines.append(t(lang, "digest.book_instructions_redacted"))
    elif catalog is not None and len(catalog.locations) <= 1:
        lines.append(t(lang, "digest.book_instructions_service_only",
                       services=services))
    else:
        lines.append(t(lang, "digest.book_instructions",
                       services=services, locations=locations))
    lines.append(t(lang, "digest.no_deeplink_note"))
    lines.append("")

    lines.append(t(lang, "digest.burst_warning"))
    lines.append(t(lang, "digest.no_repeat_note"))
    # Somebody holding out for a Thursday afternoon should say so in the
    # filter rather than sit on two digests a day ignoring them — that
    # keeps the still-looking check-in's silence meaning what it says.
    if manage_url:
        lines.append(t(lang, "digest.manage_hint", manage_url=manage_url))
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
    # Slots the filter matched this cycle, already-seen ones included. Recorded
    # on delivery as the subscriber's abundance, which sets their next interval.
    match_count: int | None = None
    # The seen_slots keys to write once this digest is actually delivered, as
    # computed by the caller that decided these slots were unseen. Carried
    # rather than recomputed so the check and the record cannot drift apart
    # under a tenant's notify_granularity. None = per-slot identity.
    seen_keys: list[str] | None = None

def send_digest(*, conn: sqlite3.Connection, subscription: Subscription,
                matched_slots: list[Slot], cycle_id: str, cfg,
                sink: list | None = None,
                match_count: int | None = None,
                seen_keys: list[str] | None = None) -> None:
    """Render a digest and stage it for delivery. `cfg` is the loaded Config
    (passed in by callers that already have it loaded — never re-read from
    os.environ here). render_digest_text loads the per-city catalog itself.

    With a `sink` list (the normal cycle path), the rendered digest is appended
    for batched delivery via `flush_digests`. Without one, it is delivered
    immediately (used for one-off sends outside a poll cycle).

    `seen_keys` are the seen_slots keys to record on delivery, one per slot in
    `matched_slots` as judged unseen by the caller. Omitted (the one-off path)
    they default to per-slot identity, which is the pre-existing behaviour and
    the safe side of a tenant with coarser granularity."""
    from app.tokens import sign
    unsub_token = sign(subscription.id, "unsubscribe",
                       primary=cfg.token_secret_primary,
                       previous=cfg.token_secret_previous)
    unsub_url = f"{cfg.public_base_url}/unsubscribe/{unsub_token}"
    manage_token = sign(subscription.id, "manage",
                        primary=cfg.token_secret_primary,
                        previous=cfg.token_secret_previous)
    manage_url = f"{cfg.public_base_url}/manage/{manage_token}"
    # Special-category subscriptions get a booking link that carries only a
    # signed subscription id, so the Amt isn't spelled out in the URL either.
    catalog = None
    try:
        from app.catalog import load_catalog
        catalog = load_catalog(subscription.city)
    except Exception:
        pass
    booking_url = None
    if catalog is not None and any(
            catalog.is_sensitive(u)
            for u in subscription.sub_filter.appointment_types):
        goto = sign(subscription.id, "goto",
                    primary=cfg.token_secret_primary,
                    previous=cfg.token_secret_previous)
        booking_url = f"{cfg.public_base_url}/go/sub/{goto}"
    body = render_digest_text(subscription, matched_slots,
                              unsubscribe_url=unsub_url,
                              public_base_url=cfg.public_base_url,
                              kofi_url=cfg.kofi_url,
                              catalog=catalog,
                              booking_url=booking_url,
                              manage_url=manage_url)
    from app.catalog import city_display_name
    city_name = city_display_name(subscription.city, subscription.language)
    subj = (t(subscription.language, "digest.subject_city", city=city_name)
            if city_name else t(subscription.language, "digest.subject"))
    key = _idem_key(subscription.id,
                    [s.hash() for s in matched_slots],
                    cycle_id)
    queued = QueuedDigest(
        item=Outgoing(to=subscription.email, subject=subj, body=body,
                      idem_key=key, unsub_url=unsub_url),
        subscription=subscription,
        slots=list(matched_slots),
        match_count=match_count,
        # Falling back to the tenant's own key, not to slot.hash(): recording
        # a key the cycle will never query is how a `day` tenant would mail
        # twice about one day. `catalog` is already loaded above; without it
        # (unknown tenant) per-slot identity is the safe default.
        seen_keys=(list(seen_keys) if seen_keys is not None
                   else [(catalog.seen_key(s) if catalog is not None
                          else s.hash())
                         for s in matched_slots]),
    )
    if sink is None:
        flush_digests(conn, [queued], cfg)
    else:
        sink.append(queued)

def flush_digests(conn: sqlite3.Connection, sink: list, cfg) -> None:
    """Deliver every staged digest in `sink` via quota-aware batches, then
    record seen_slots + last_notified for the ones that were actually sent.
    Deferred digests are left unrecorded so the next cycle re-sends them.

    Longest-waiting subscriber first. `send_batch` fills provider batches in
    list order and defers the tail, so whatever order the cycle happened to
    stage in decides who loses a digest under saturation — and that order is
    stable (city, then subscription id), which would put the same people at the
    back every cycle. Sorting on last_notified_at rotates it: a deferred digest
    never stamps last_notified_at, so anyone passed over keeps their old (or
    absent) timestamp and leads the next cycle. Never-notified subscribers sort
    first, which is also the right answer on the merits.
    """
    if not sink:
        return
    from app.db import transaction
    from app.repo import (record_digest_delivery, record_seen_slot,
                          set_last_notified)
    sink = sorted(sink, key=lambda q: str(q.subscription.last_notified_at or ""))
    result = send_batch(conn, [q.item for q in sink], cfg)
    for q in sink:
        if q.item.idem_key not in result.delivered:
            continue
        with transaction(conn):
            # Several slots can share one key at day granularity (the whole
            # point), so write each distinct key once.
            keys = (q.seen_keys if q.seen_keys is not None
                    else [slot.hash() for slot in q.slots])
            for key in dict.fromkeys(keys):
                record_seen_slot(conn, q.subscription.id, key)
            set_last_notified(conn, q.subscription.id, q.match_count)
            record_digest_delivery(conn, q.subscription.id)
    maybe_quota_alert(conn, cfg, deferred=result.deferred)
