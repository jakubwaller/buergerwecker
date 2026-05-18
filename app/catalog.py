from __future__ import annotations
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CATALOG_ROOT = Path(__file__).parent.parent / "catalog"

class CatalogError(Exception):
    pass

@dataclass(frozen=True)
class Catalog:
    city: str
    appointment_types: dict[str, str]  # name → uuid
    locations: dict[str, str]          # name → uuid
    scraper_config: dict               # vendor-specific, opaque to web layer

    def appointment_type_name_for(self, uuid: str) -> str | None:
        return next((n for n, u in self.appointment_types.items() if u == uuid), None)

    def location_name_for(self, uuid: str) -> str | None:
        return next((n for n, u in self.locations.items() if u == uuid), None)

    def appointment_type_uuid_for(self, name: str) -> str | None:
        return self.appointment_types.get(name)

    def location_uuid_for(self, name: str) -> str | None:
        return self.locations.get(name)

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
    return Catalog(city=city, appointment_types=ats, locations=locs,
                   scraper_config=scfg)
