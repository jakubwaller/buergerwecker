"""Kommunix TEVIS scraper (Dresden Bürgerbüros).

Notify-only, deliberately shallow: TEVIS's office-selection page (`/location`)
already carries "Nächster Termin ab <date>, <time>" per office, so one GET per
appointment type yields the earliest free slot at every office. We never walk
into the calendar/suggest steps and never touch the booking flow (which is
where TEVIS's Sicherheitscode/captcha lives — by design out of our path).

Earliest-slot-only semantics: subscribers are notified about the first free
slot per office, not the full slot list. Filters (weekdays, time window,
max_days_ahead) therefore apply to that earliest slot. The tenant's
display.json note explains this on the sign-up page.

The office list renders only for a session that has visited the Anliegen page
(`/select2?md=…`) first; a cookie-less request gets a generic help page. The
poller hands each cycle a fresh session, so poll() bootstraps the cookie once
per session and retries once if the office list is missing (expired session —
TEVIS idles out after ~20 minutes).
"""
from __future__ import annotations
import re
from bs4 import BeautifulSoup
import requests
from app.models import Slot, PollPlan
from app.catalog import load_catalog

# "Nächster Termin ab 14.07.2026, 08:15 Uhr" (list card text). The map-marker
# variant of each office carries the date only in a title= attribute, which
# get_text() never sees — so each office matches exactly once.
_NEXT_SLOT_RE = re.compile(
    r"Termine?\s+ab\s+(\d{1,2})\.(\d{1,2})\.(\d{4}),\s*(\d{1,2}):(\d{2})")


def parse_slots(html: str, *, service_id: str,
                locations) -> list[Slot]:
    """Parse the TEVIS /location page into one earliest Slot per office.

    Office cards are <form>s carrying a hidden `loc` input; the card text
    holds the office name and next-free-slot line. Offices without a parseable
    "Termine ab" line (no free slots, or map-marker duplicates with empty
    text) are skipped. `locations` is the plan's spec: list of loc ids or
    "all".
    """
    soup = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []
    seen_locs: set[str] = set()
    for form in soup.find_all("form"):
        loc_inp = form.find("input", attrs={"name": "loc"})
        if loc_inp is None:
            continue
        loc = (loc_inp.get("value") or "").strip()
        if not loc or loc in seen_locs:
            continue
        m = _NEXT_SLOT_RE.search(form.get_text(" ", strip=True))
        if not m:
            continue
        seen_locs.add(loc)
        if locations != "all" and loc not in locations:
            continue
        day, month, year, hh, mm = m.groups()
        date_iso = f"{year}-{int(month):02d}-{int(day):02d}"
        time_str = f"{int(hh):02d}:{mm}"
        slots.append(Slot(
            date=date_iso,
            time_str=time_str,
            location_uuid=loc,
            service_uuid=service_id,
            # TEVIS has no per-slot deep link (booking is session-bound);
            # digests link to the booking start page instead (see
            # catalog.booking_start_url). The token just keeps the Slot shape
            # unique per (slot, office).
            booking_token=f"{date_iso}T{time_str}@{loc}",
        ))
    return slots


def _bootstrap_session(http: requests.Session, base: str, md: str) -> None:
    """Visit the Anliegen page so TEVIS issues the session cookie.

    Tracked per session object (not module-global like smartcjm's wsid):
    TEVIS state lives in the cookie jar, and the poller creates a fresh
    session every cycle, so a module-level TTL would go stale immediately.
    """
    ready: set = getattr(http, "_tevis_ready", None) or set()
    if (base, md) in ready:
        return
    http.get(f"{base}/select2", params={"md": md}, timeout=30)
    ready.add((base, md))
    http._tevis_ready = ready


def _fetch_location_page(http: requests.Session, base: str, scfg: dict,
                         plan: PollPlan) -> str:
    r = http.get(
        f"{base}/location",
        params={"mdt": str(scfg["mdt"]), "select_cnc": "1",
                f"cnc-{plan.appointment_type}": "1"},
        timeout=30,
    )
    return r.text


def _has_office_list(html: str) -> bool:
    """Cheap session-validity probe: the real page carries office forms with
    hidden `loc` inputs; the cookie-less/expired variant is a generic help
    page without any."""
    return 'name="loc"' in html


def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """Fetch the earliest free slot per office for the plan's appointment type."""
    catalog = load_catalog(plan.city)
    scfg = catalog.scraper_config
    if scfg.get("vendor") != "tevis":
        raise RuntimeError(
            f"city {plan.city} not configured for tevis scraper "
            f"(vendor={scfg.get('vendor')})"
        )
    base = scfg["base_url"].rstrip("/")
    md = str(scfg["md"])
    _bootstrap_session(http, base, md)
    html = _fetch_location_page(http, base, scfg, plan)
    if not _has_office_list(html):
        # Session expired between requests — re-acquire the cookie and retry
        # once. A service with genuinely zero free slots still renders the
        # office forms, so this does not loop on empty results.
        http._tevis_ready = set()
        _bootstrap_session(http, base, md)
        html = _fetch_location_page(http, base, scfg, plan)
    return parse_slots(html, service_id=plan.appointment_type,
                       locations=plan.locations)
