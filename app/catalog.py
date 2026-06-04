from __future__ import annotations
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CATALOG_ROOT = Path(__file__).parent.parent / "catalog"

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


@dataclass(frozen=True)
class Catalog:
    city: str
    appointment_types: dict[str, str]  # name → uuid (German — canonical)
    locations: dict[str, str]          # name → uuid (German — canonical)
    scraper_config: dict               # vendor-specific, opaque to web layer
    appointment_types_en: dict[str, str] = field(default_factory=dict)
    locations_en: dict[str, str] = field(default_factory=dict)

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

@lru_cache(maxsize=8)
def load_catalog(city: str) -> Catalog:
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
    return Catalog(city=city, appointment_types=ats, locations=locs,
                   scraper_config=scfg,
                   appointment_types_en=ats_en, locations_en=locs_en)


def _read_optional_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
