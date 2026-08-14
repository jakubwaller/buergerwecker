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
from app.scrapers import smartcjm, tevis

class ScraperProtocol(Protocol):
    """Structural type used for documentation / mypy. Not enforced at runtime."""
    def poll(self, plan: PollPlan, http: requests.Session) -> list[Slot]: ...

class UnsupportedCity(Exception):
    pass

_REGISTRY: dict[str, ModuleType] = {
    "leipzig": smartcjm,
    "leipzig-abh": smartcjm,
    "dresden": tevis,
    "bochum": smartcjm,
    # Bochum Straßenverkehrsamt (today the Büro für Kfz-Angelegenheiten),
    # added 2026-08 on an r/bochum request: two calendars on the same
    # Smart-CJM host as the Bürgerbüro, Mandant /m/kfz-angelegenheiten/,
    # no locations step (one office each).
    "bochum-fuehrerschein": smartcjm,
    "bochum-kfz": smartcjm,
    "bonn": smartcjm,
    # TEVIS wave 2026-07 (vendor survey Tier 1): per-city config lives in
    # catalog/<slug>/; all share the Dresden scraper.
    "augsburg": tevis,
    "bottrop": tevis,
    "braunschweig": tevis,
    "darmstadt": tevis,
    "duesseldorf": tevis,
    "hagen": tevis,
    "ingolstadt": tevis,
    "kaiserslautern": tevis,
    "kassel": tevis,
    "kiel": tevis,
    "ludwigshafen": tevis,
    "luebeck": tevis,
    "mainz": tevis,
    "moenchengladbach": tevis,
    "moers": tevis,
    "muenster": tevis,
    "neuss": tevis,
    "nuernberg": tevis,
    "oberhausen": tevis,
    "oldenburg": tevis,
    "paderborn": tevis,
    "remscheid": tevis,
    "saarbruecken": tevis,
    "salzgitter": tevis,
    "trier": tevis,
    # Münster Fachämter, added 2026-08 at the city's own request: separate
    # Mandanten (md) on the same TEVIS instance as the Bürgeramt, so one
    # tenant each. The Gesundheitsamt (md 23) is deliberately absent — its
    # single Anliegen is STI counselling, i.e. Art. 9 GDPR data, and waits for
    # the explicit-consent flow.
    "muenster-einbuergerung": tevis,
    "muenster-energieberatung": tevis,
    "muenster-gewerbe": tevis,
    "muenster-kfz": tevis,
    "muenster-rente": tevis,
    "muenster-standesamt": tevis,
}

def get_scraper(city: str) -> ModuleType:
    """Return the scraper module for `city`. The module's `poll(plan, http)`
    is the only attribute the caller may rely on."""
    if city not in _REGISTRY:
        raise UnsupportedCity(city)
    return _REGISTRY[city]
