from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.models import SeenKey, per_slot_key

CATALOG_ROOT = Path(__file__).parent.parent / "catalog"

# The shape of every tenant directory name, and the only thing load_catalog
# will look up. Notably excludes "/", "\" and "." — see load_catalog.
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

_NOTIFY_GRANULARITIES = ("slot", "day")


def _default_granularity(scfg: dict) -> str:
    """The granularity a tenant gets by saying nothing.

    TEVIS is "day" because earliest-slot-only is how *our* scraper reads it
    (tevis.parse_slots: one earliest Slot per office), so every TEVIS tenant
    has the same shape and the same redundancy. Everything else lists real
    inventory and keeps per-slot identity.
    """
    return "day" if scfg.get("vendor") == "tevis" else "slot"


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
    # What counts as one piece of news for this tenant. Defaults by vendor
    # (see _default_granularity); `notify_granularity` in scraper_config.json
    # overrides it for one tenant:
    #
    #   "slot" — every distinct (day, time, office, service). Right
    #       for a vendor that lists real inventory: each slot is a separate
    #       perishable opportunity, and a subscriber told about a 09:00 that
    #       someone else then books genuinely wants to hear about the 14:00.
    #   "day" — (day, office, service), and the row remembers the earliest
    #       time already reported on that day. Right for a tenant that only
    #       ever exposes the *earliest* slot per office (TEVIS): the "new" slot
    #       appearing after someone books is the same inventory seen a minute
    #       later, not new availability, so a same-or-later time on a reported
    #       day stays quiet. A strictly *earlier* time can only mean a
    #       cancellation opened a better slot, and that goes out. See
    #       `SeenKey` in app.models for the rule and `repo.has_seen_slot` for
    #       where it is applied.
    #
    # Unknown values fall back to "slot", the never-suppress-anything side.
    #
    # Why the time is remembered at all: a plain date key cannot tell the
    # earliest slot moving *forward* (someone booked — redundant) from it
    # moving *back* (a cancellation — real news), and the first version of
    # "day" suppressed both. That was accepted for muenster-kfz against 4-8
    # mails a day, but it is a real loss on any tenant with a multi-day
    # horizon — and Münster's own 2408 stood sixteen days out when probed on
    # 2026-08-25 — so the horizon was never the discriminator. What decides
    # whether a tenant should be "day" is only whether its vendor shows one
    # slot per office — and for TEVIS that is not a tenant property but how
    # our scraper is built (tevis.parse_slots yields one earliest slot per
    # office), which is why it is the vendor default rather than 30 config
    # keys.
    notify_granularity: str = "slot"

    def seen_key(self, slot) -> SeenKey:
        """The seen_slots key for `slot` under this tenant's granularity.

        Single source of truth: the cycle filters on it and the flush records
        it, and those two must never disagree — checking one key while
        recording another means either a digest every cycle forever or silence
        forever, both silent.
        """
        if self.notify_granularity == "day":
            return SeenKey(slot.day_hash(), best_time=slot.time_str)
        return per_slot_key(slot)

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

# Every file a tenant directory can hold. The cache signature below stats each
# of them, so a rewrite of any one (catalog_sync's atomic replace, a hand edit)
# is noticed on the next load.
_CATALOG_FILES = ("appointment_type.json", "locations.json",
                  "scraper_config.json", "appointment_type.en.json",
                  "locations.en.json", "display.json",
                  "service_locations.json")

# city → (file signature, Catalog). A plain dict rather than lru_cache: the
# cache must (a) hold every tenant — an lru_cache(maxsize=8) over 38 tenants
# missed on nearly every call, re-reading and re-parsing up to seven JSON
# files per miss, and the tenant switcher loads all of them per page view —
# and (b) notice when catalog_sync rewrites a file. The poller runs the sync,
# but the web workers are separate processes that never restart for it, so
# "clear the cache after syncing" cannot reach them; a per-load stat of the
# files (cheap, no parse) can. `load_catalog.cache_clear()` is kept for tests
# that swap CATALOG_ROOT.
_CACHE: dict[str, tuple[tuple, "Catalog"]] = {}


def _signature(city_dir: Path) -> tuple:
    sig = []
    for name in _CATALOG_FILES:
        try:
            st = (city_dir / name).stat()
            sig.append((name, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            sig.append((name, None, None))
    return tuple(sig)


def load_catalog(city: str) -> Catalog:
    # A tenant slug arrives straight from a URL, so validate its shape before
    # it is joined onto a path: "../.." would walk out of the catalog root, and
    # the cache would then key on whatever was passed. Every tenant directory
    # is lowercase letters, digits and hyphens (test_catalog asserts it), so
    # anything else is not a city we have.
    if not _SLUG_RE.fullmatch(city or ""):
        raise CatalogError(f"Unknown city: {city}")
    city_dir = CATALOG_ROOT / city
    if not city_dir.is_dir():
        raise CatalogError(f"Unknown city: {city}")
    sig = _signature(city_dir)
    cached = _CACHE.get(city)
    if cached is not None and cached[0] == sig:
        return cached[1]
    catalog = _read_catalog(city, city_dir)
    _CACHE[city] = (sig, catalog)
    return catalog


load_catalog.cache_clear = _CACHE.clear  # type: ignore[attr-defined]


def _read_catalog(city: str, city_dir: Path) -> Catalog:
    try:
        ats = _read_required_json(city_dir / "appointment_type.json")
        locs = _read_required_json(city_dir / "locations.json")
        scfg = _read_required_json(city_dir / "scraper_config.json")
    except FileNotFoundError as exc:
        raise CatalogError(f"Missing catalog file for {city}: {exc.filename}") from exc
    except ValueError as exc:
        # A required file that exists but does not parse is as much "not a
        # tenant we can serve" as a missing one. Left as a JSONDecodeError it
        # walked past every `except CatalogError` — the tenant switcher, the
        # sitemap, /subscribe — and one hand-edited file took down every
        # tenant's page instead of hiding the broken one.
        raise CatalogError(f"Malformed catalog file for {city}: {exc}") from exc
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
    granularity = str(scfg.get("notify_granularity")
                      or _default_granularity(scfg))
    if granularity not in _NOTIFY_GRANULARITIES:
        # A typo ("Day", "daily") would otherwise leave the tenant looking
        # correctly configured while the intended suppression never happens.
        # Falling back to 'slot' rather than the vendor default keeps the
        # failure on the side that can only send *more* mail.
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


def _read_required_json(path: Path) -> dict:
    """A required catalog file. Raises FileNotFoundError or ValueError (a
    JSONDecodeError is one); load_catalog turns both into CatalogError, naming
    the file, so a broken tenant is skipped rather than fatal."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def _read_optional_json(path: Path) -> dict:
    """Optional catalog files (EN labels, display.json) degrade to defaults —
    a missing OR malformed optional file must never take a page down.
    (json.JSONDecodeError subclasses ValueError.)"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
