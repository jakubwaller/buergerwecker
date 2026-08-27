"""City scraper registry.

EACH SCRAPER MODULE MUST EXPOSE A MODULE-LEVEL FUNCTION:

    def poll(plan: PollPlan, http: requests.Session) -> list[Slot]: ...

This is the only contract. Modules MUST NOT define classes or expect to
be instantiated — `get_scraper(city)` returns the module itself, and the
caller invokes `module.poll(plan, http=http)`. When adding a new city
(e.g., Hamburg ODControls), create `app/scrapers/<vendor>.py` with this
free function signature and add it to `_VENDORS` below. Cities are not
listed here: `get_scraper(city)` reads `vendor` from the tenant's
scraper_config.json, so a new catalog directory polls the moment it exists.
(The old per-city registry had to be edited by hand, and a tenant left out of
it signed people up and silently never polled — leipzig-abh shipped that way.)
"""
from __future__ import annotations
from types import ModuleType
from typing import Protocol
import requests
from app.catalog import CatalogError, load_catalog
from app.models import PollPlan, Slot
from app.scrapers import smartcjm, tevis

class ScraperProtocol(Protocol):
    """Structural type used for documentation / mypy. Not enforced at runtime."""
    def poll(self, plan: PollPlan, http: requests.Session) -> list[Slot]: ...

class UnsupportedCity(Exception):
    pass

_VENDORS: dict[str, ModuleType] = {
    "smartcjm": smartcjm,
    "tevis": tevis,
}

def get_scraper(city: str) -> ModuleType:
    """Return the scraper module for `city`, by the vendor its catalog
    declares. The module's `poll(plan, http)` is the only attribute the caller
    may rely on."""
    try:
        vendor = load_catalog(city).scraper_config.get("vendor")
    except CatalogError as exc:
        raise UnsupportedCity(f"{city}: {exc}") from exc
    module = _VENDORS.get(vendor)
    if module is None:
        raise UnsupportedCity(f"{city}: unknown vendor {vendor!r}")
    return module
