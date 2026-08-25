from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CATALOG_ROOT = Path(__file__).parent.parent / "catalog"

# The shape of every tenant directory name, and the only thing load_catalog
# will look up. Notably excludes "/", "\" and "." — see load_catalog.
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

_NOTIFY_GRANULARITIES = ("slot", "day")


class CatalogError(Exception):
    pass


def _localized(de_map: dict[str, str], en_map: dict[str, str],
               lang: str) -> dict[str, str]:
    """Return a name→uuid map for display in `lang`.

    The uuid is the stable identity; only the label is language-specific. For
    English we re-key the German map by uuid so the full German set is always
    shown — any uuid the English table is missing falls back to its German
    label rather than dropping the option. Result is sorted by display name.
    """
    if lang != "en" or not en_map:
        return dict(de_map)
    en_by_uuid = {uuid: name for name, uuid in en_map.items()}
    merged = {en_by_uuid.get(uuid, de_name): uuid
              for de_name, uuid in de_map.items()}
    return dict(sorted(merged.items()))


def _label_for(name_to_uuid: dict[str, str], uuid: str) -> str:
    """Reverse a name→uuid map to find the display name for `uuid`.

    Falls back to the uuid itself when absent, so callers never get None.
    """
    return next((n for n, u in name_to_uuid.items() if u == uuid), uuid)


@dataclass(frozen=True)
class Catalog:
    city: str
    appointment_types: dict[str, str]  # name → uuid (German — canonical)
    locations: dict[str, str]          # name → uuid (German — canonical)
    scraper_config: dict               # vendor-specific, opaque to web layer
    appointment_types_en: dict[str, str] = field(default_factory=dict)
    locations_en: dict[str, str] = field(default_factory=dict)
    # Optional per-tenant UI copy from display.json: keys like "label",
    # "heading", "note", "city_name", each a {"de": …, "en": …} map. Missing
    # file or keys → the templates fall back to their built-in default copy.
    display: dict = field(default_factory=dict)
    # Optional service uuid → [location uuids offering it], maintained by
    # catalog_sync. The sign-up form uses it to hide offices that don't offer
    # the selected service. Empty/missing service key = unknown coverage —
    # the form then shows every location for that service.
    service_locations: dict = field(default_factory=dict)
    # Services whose mere selection reveals special-category data under Art. 9
    # GDPR — an STI-counselling appointment is health data, an SBGG declaration
    # is gender-identity data. Subscribing to one needs separate explicit
    # consent (Art. 9(2)(a)), and the service is never named back to the
    # subscriber by email. Declared as `sensitive_services` in
    # scraper_config.json; the ids are the vendor's, same space as
    # `exclude_services`. Such a tenant's display.json `city_name` must stay
    # the bare city — it is the one label that still reaches mail subjects
    # (confirmation, digest), so folding the Amt into it would give the game
    # away there.
    sensitive_services: frozenset = field(default_factory=frozenset)
    # What counts as one piece of news for this tenant, from
    # `notify_granularity` in scraper_config.json:
    #
    #   "slot" (default) — every distinct (day, time, office, service). Right
    #       for a vendor that lists real inventory: each slot is a separate
    #       perishable opportunity, and a subscriber told about a 09:00 that
    #       someone else then books genuinely wants to hear about the 14:00.
    #   "day" — (day, office, service), the time dropped. Right for a tenant
    #       that only ever exposes the *earliest* slot per office (TEVIS): the
    #       "new" slot appearing after someone books is the same inventory
    #       seen a minute later, not new availability, so notifying again says
    #       nothing the subscriber does not already know.
    #
    # Unknown values fall back to "slot", the never-suppress-anything side.
    #
    # Known limitation, and the reason "day" is not simply switched on for
    # every TEVIS tenant: the key cannot tell the earliest slot moving
    # *forward* (someone booked — redundant news) from it moving *back* (a
    # cancellation — real news). Once a day is recorded, an earlier slot
    # opening on that same day is suppressed until housekeeping prunes the row
    # at 7 days.
    #
    # This applies to muenster-kfz too — probed live 2026-08-25, its 2407 was a
    # day out and its 2408 sixteen days out, so the tenant is NOT the same-day
    # -only case an earlier draft of this comment assumed. Accepted knowingly:
    # the loss is confined to a day that was reported, vanished, and reopened
    # inside a week, against 4-8 mails a day telling subscribers about times on
    # a day they had already been told about. Weigh it again for any other
    # tenant, and prefer teaching the key "earlier than last told" over
    # widening the rollout on this trade alone.
    notify_granularity: str = "slot"

    def seen_key(self, slot) -> str:
        """The seen_slots key for `slot` under this tenant's granularity.

        Single source of truth: the cycle filters on it and the flush records
        it, and those two must never disagree — checking one key while
        recording another means either a digest every cycle forever or silence
        forever, both silent.
        """
        return (slot.day_hash() if self.notify_granularity == "day"
                else slot.hash())

    def is_sensitive(self, uuid: str) -> bool:
        """Does subscribing to this service reveal special-category data?

        Answered from the declaration alone, not from what the catalog
        currently offers, so a subscription taken before a service was
        withdrawn keeps its protection in the digest.
        """
        return uuid in self.sensitive_services

    @property
    def has_sensitive(self) -> bool:
        """Is any service currently *offered* by this tenant a sensitive one?

        This is what decides whether the sign-up form shows the extra consent
        box: a tenant whose sensitive services are all excluded needs no box.
        """
        return any(u in self.sensitive_services
                   for u in self.appointment_types.values())

    def display_text(self, key: str, lang: str) -> str | None:
        """Localized display.json text for `key`; falls back to German; None if unset."""
        entry = self.display.get(key) or {}
        return entry.get(lang) or entry.get("de") or None

    def appointment_type_name_for(self, uuid: str) -> str | None:
        return next((n for n, u in self.appointment_types.items() if u == uuid), None)

    def location_name_for(self, uuid: str) -> str | None:
        return next((n for n, u in self.locations.items() if u == uuid), None)

    def appointment_type_uuid_for(self, name: str) -> str | None:
        return self.appointment_types.get(name)

    def location_uuid_for(self, name: str) -> str | None:
        return self.locations.get(name)

    def appointment_types_for(self, lang: str) -> dict[str, str]:
        """name→uuid map for the appointment-type dropdown, localized for `lang`."""
        return _localized(self.appointment_types, self.appointment_types_en, lang)

    def locations_for(self, lang: str) -> dict[str, str]:
        """name→uuid map for the locations list, localized for `lang`."""
        return _localized(self.locations, self.locations_en, lang)

    def appointment_type_label(self, uuid: str, lang: str) -> str:
        """Localized display name for a service uuid; the raw uuid if unknown.

        Never raises and never returns empty: slots can carry an out-of-catalog
        uuid, and a notification must still render (worst case showing the uuid).
        """
        return _label_for(self.appointment_types_for(lang), uuid)

    def location_label(self, uuid: str, lang: str) -> str:
        """Localized display name for a location uuid; the raw uuid if unknown."""
        return _label_for(self.locations_for(lang), uuid)

@lru_cache(maxsize=8)
def load_catalog(city: str) -> Catalog:
    # A tenant slug arrives straight from a URL, so validate its shape before
    # it is joined onto a path: "../.." would walk out of the catalog root, and
    # the lru_cache would then key on whatever was passed. Every tenant
    # directory is lowercase letters, digits and hyphens (test_catalog asserts
    # it), so anything else is not a city we have.
    if not _SLUG_RE.fullmatch(city or ""):
        raise CatalogError(f"Unknown city: {city}")
    city_dir = CATALOG_ROOT / city
    if not city_dir.is_dir():
        raise CatalogError(f"Unknown city: {city}")
    try:
        ats = json.loads((city_dir / "appointment_type.json").read_text(encoding="utf-8"))
        locs = json.loads((city_dir / "locations.json").read_text(encoding="utf-8"))
        scfg = json.loads((city_dir / "scraper_config.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"Missing catalog file for {city}: {exc.filename}") from exc
    # English labels are optional: a city without an *.en.json simply falls
    # back to the German names everywhere (see Catalog.appointment_types_for).
    ats_en = _read_optional_json(city_dir / "appointment_type.en.json")
    locs_en = _read_optional_json(city_dir / "locations.en.json")
    display = _read_optional_json(city_dir / "display.json")
    svc_locs = _read_optional_json(city_dir / "service_locations.json")
    # Services the tenant refuses to carry (Art. 9 GDPR selections, see
    # catalog_sync._drop_excluded). Filtering on read as well as on sync means
    # a hand-edited or stale catalog file still can't offer them.
    excluded = {str(s) for s in (scfg.get("exclude_services") or ())}
    if excluded:
        ats = {n: u for n, u in ats.items() if u not in excluded}
        ats_en = {n: u for n, u in ats_en.items() if u not in excluded}
        svc_locs = {u: v for u, v in svc_locs.items() if u not in excluded}
    sensitive = frozenset(str(s) for s in (scfg.get("sensitive_services") or ()))
    granularity = str(scfg.get("notify_granularity") or "slot")
    if granularity not in _NOTIFY_GRANULARITIES:
        # A typo ("Day", "daily") would otherwise leave the tenant looking
        # correctly configured while the intended suppression never happens.
        print(f"catalog {city}: unknown notify_granularity "
              f"{granularity!r}, using 'slot'", flush=True)
        granularity = "slot"
    return Catalog(city=city, appointment_types=ats, locations=locs,
                   scraper_config=scfg,
                   appointment_types_en=ats_en, locations_en=locs_en,
                   display=display, service_locations=svc_locs,
                   sensitive_services=sensitive,
                   notify_granularity=granularity)


def city_display_name(city: str, lang: str) -> str | None:
    """Localized city_name from the tenant's display.json; None if unset or the
    catalog can't be loaded. Email paths must never fail on a missing catalog."""
    try:
        return load_catalog(city).display_text("city_name", lang)
    except Exception:
        return None


def booking_start_url(scfg: dict, lang: str = "de") -> str:
    """Entry URL of the city's booking flow, per vendor.

    This is what digest emails link to (via /go/<city>). Per-slot deep links
    are not possible on any current vendor: Smart-CJM's /booking endpoint
    rejects requests whose cookie session hasn't walked the
    services→locations→search_results steps, and TEVIS booking is equally
    session-bound — so the start page is the deepest reachable target.
    """
    vendor = scfg.get("vendor")
    if vendor == "smartcjm":
        return f"{scfg['base_url']}/?uid={scfg['uid']}&lang={lang}"
    if vendor == "tevis":
        return f"{scfg['base_url']}/select2?md={scfg['md']}"
    raise CatalogError(f"no booking-start URL for vendor: {vendor}")


def available_cities() -> list[str]:
    """Catalog directory names that hold a complete tenant config, sorted.

    Drives the cross-links between tenants on the sign-up form; a directory
    without a scraper_config.json (e.g. a scaffold) is not offered.
    """
    return sorted(d.name for d in CATALOG_ROOT.iterdir()
                  if d.is_dir() and (d / "scraper_config.json").is_file())


def _read_optional_json(path: Path) -> dict:
    """Optional catalog files (EN labels, display.json) degrade to defaults —
    a missing OR malformed optional file must never take a page down.
    (json.JSONDecodeError subclasses ValueError.)"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
