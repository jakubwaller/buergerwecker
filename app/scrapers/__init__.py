"""City scraper registry.

EACH SCRAPER MODULE MUST EXPOSE A MODULE-LEVEL FUNCTION:

    def poll(plan: PollPlan, http: requests.Session) -> list[Slot]: ...

This is the only contract. Modules MUST NOT define classes or expect to
be instantiated — `get_scraper(city)` returns the module itself, and the
caller invokes `module.poll(plan, http=http)`. When adding a new city
(e.g., Hamburg ODControls), create `app/scrapers/<vendor>.py` with this
free function signature, then add an entry to `_REGISTRY` below.
"""
from __future__ import annotations
from types import ModuleType
from typing import Protocol
import requests
from app.models import PollPlan, Slot
from app.scrapers import smartcjm

class ScraperProtocol(Protocol):
    """Structural type used for documentation / mypy. Not enforced at runtime."""
    def poll(self, plan: PollPlan, http: requests.Session) -> list[Slot]: ...

class UnsupportedCity(Exception):
    pass

_REGISTRY: dict[str, ModuleType] = {
    "leipzig": smartcjm,
    # When adding Hamburg, etc.:
    #   from app.scrapers import odcontrols
    #   "hamburg": odcontrols,
}

def get_scraper(city: str) -> ModuleType:
    """Return the scraper module for `city`. The module's `poll(plan, http)`
    is the only attribute the caller may rely on."""
    if city not in _REGISTRY:
        raise UnsupportedCity(city)
    return _REGISTRY[city]
