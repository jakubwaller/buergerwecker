"""Kommunix TEVIS scraper (Dresden Bürgerbüros).

Notify-only, deliberately shallow: TEVIS's office-selection page (`/location`)
already carries "Nächster Termin ab <date>, <time>" per office, so one GET per
appointment type yields the earliest free slot at every office. We never touch
the booking flow (which is where TEVIS's Sicherheitscode/captcha lives — by
design out of our path).

Earliest-slot-only semantics: subscribers are notified about the first free
slot per office, not the full slot list. Filters (weekdays, time window,
max_days_ahead) therefore apply to that earliest slot. The tenant's
display.json note explains this on the sign-up page.

`read_times` (scraper_config.json, opt-in per tenant; Stadt Münster confirmed
on 2026-11-10 that reading the free times holds nothing, only selecting one
does) goes one wizard step further for each office the plan watches that has a
free slot: it selects the office as a browser does (POST /location, answered
with a redirect) and reads the suggestion list (GET /suggest), so the poll
yields every free time the list shows on that office's earliest free day and
the time window can match any of them. That is 1 + 2 × offices requests per
appointment type instead of 1 (3 for muenster-kfz's one office). It stops at
the list: a time is chosen by POSTing one of the list's own forms, which
nothing here does. If the step fails or parses to nothing, the office falls
back to its earliest slot. Only the earliest day is kept, so seen_slots stays
one day key per office as before (see Catalog.seen_key).

The office list renders only for a session that has visited the Anliegen page
(`/select2?md=…`) first; a cookie-less request gets a generic help page. The
poller reuses ONE long-lived session for the whole process (see poller.main),
so poll() bootstraps the cookie once per (base, md) and leans on the retry
path for expiry: when the office list is missing (TEVIS idles sessions out
after ~20 minutes), it drops the stale cookie, re-bootstraps, and refetches
once.
"""
from __future__ import annotations
import re
from dataclasses import replace
from urllib.parse import urlparse
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
    TEVIS state lives in the cookie jar, so the flag must live and die with
    the session object that holds it. The poller's session spans the whole
    process, so this flag can outlive the ~20-minute server-side idle-out —
    poll()'s retry path covers that case.
    """
    ready: set = getattr(http, "_tevis_ready", None) or set()
    if (base, md) in ready:
        return
    http.get(f"{base}/select2", params={"md": md},
             timeout=30).raise_for_status()
    ready.add((base, md))
    http._tevis_ready = ready


def _location_params(scfg: dict, plan: PollPlan) -> dict:
    return {"mdt": str(scfg["mdt"]), "select_cnc": "1",
            f"cnc-{plan.appointment_type}": "1"}


def _fetch_location_page(http: requests.Session, base: str, scfg: dict,
                         plan: PollPlan) -> str:
    r = http.get(f"{base}/location", params=_location_params(scfg, plan),
                 timeout=30)
    r.raise_for_status()
    return r.text


def parse_times(html: str, earliest: Slot) -> list[Slot]:
    """Parse the TEVIS suggestion list (/suggest) into one Slot per free time
    on its first day, for `earliest`'s office and service.

    A free time is a <form class="suggestion_form"> with a hidden `date`
    (YYYYMMDD) and the time as its button text; a taken time is a disabled
    button outside any form, so it never matches. The list is capped by TEVIS
    and can run into later days; only the first is kept, since a later day
    would be a new day key and a mail the earliest-slot reading never sent.
    [] unless the page's filter form names exactly this office and service, so
    a list left over from another plan can never be read as this one's.
    """
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="suggest_filter_form")
    if form is None:
        return []
    fields = {i.get("name"): (i.get("value") or "").strip()
              for i in form.find_all("input", attrs={"type": "hidden"})}
    cncs = {k for k in fields if (k or "").startswith("cnc-")}
    if (fields.get("loc") != earliest.location_uuid
            or cncs != {f"cnc-{earliest.service_uuid}"}):
        return []
    found = set()
    for f in soup.find_all("form", class_="suggestion_form"):
        date, button = f.find("input", attrs={"name": "date"}), f.find("button")
        d = re.fullmatch(r"(\d{4})(\d{2})(\d{2})",
                         (date.get("value") or "").strip()) if date else None
        t = re.fullmatch(r"(\d{1,2}):(\d{2})",
                         button.get_text(strip=True)) if button else None
        if d and t:
            found.add((f"{d[1]}-{d[2]}-{d[3]}", f"{int(t[1]):02d}:{t[2]}"))
    if not found:
        return []
    day = min(found)[0]
    loc = earliest.location_uuid
    return [replace(earliest, date=d, time_str=t, booking_token=f"{d}T{t}@{loc}")
            for d, t in sorted(found) if d == day]


def _office_form_data(html: str, loc: str) -> dict[str, str]:
    """What a browser posts on "Weiter" for office `loc`: its form's hidden
    fields (loc, gps_lat, gps_long) and the submit button."""
    for form in BeautifulSoup(html, "html.parser").find_all("form"):
        if form.find("input", attrs={"name": "loc", "value": loc}):
            data = {i["name"]: i.get("value", "")
                    for i in form.find_all("input", attrs={"type": "hidden"})
                    if i.get("name")}
            return {**data, "select_location": "Weiter"}
    raise ValueError(f"no office form for loc {loc}")


def _read_times(http: requests.Session, base: str, scfg: dict, plan: PollPlan,
                html: str, earliest: Slot) -> list[Slot]:
    """The free times on `earliest`'s day at its office, or [earliest] when
    they cannot be read: the extra step must never make a tenant go blind.
    Two requests, both before any time is chosen (see the module docstring)."""
    try:
        # 10 s, not 30: this step is optional and must not hold up the cycle.
        http.post(f"{base}/location", params=_location_params(scfg, plan),
                  data=_office_form_data(html, earliest.location_uuid),
                  timeout=10, allow_redirects=False).raise_for_status()
        r = http.get(f"{base}/suggest", timeout=10)
        r.raise_for_status()
        times = parse_times(r.text, earliest)
        if times:
            return times
        reason = "no free time parsed"
    except Exception as exc:
        reason = repr(exc)
    print(f"tevis {plan.city}/{plan.appointment_type}: times list not read "
          f"({reason}), reporting the earliest slot only", flush=True)
    return [earliest]


def _has_office_list(html: str) -> bool:
    """Cheap session-validity probe: the real page carries office forms with
    hidden `loc` inputs; the cookie-less/expired variant is a generic help
    page without any."""
    return 'name="loc"' in html


def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """Fetch the earliest free slot per office for the plan's appointment type
    (with `read_times`, the free times of that slot's day)."""
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
        # Session expired — drop the stale cookie and retry once. The cookie
        # must go first: TEVIS may not mint a new session for a request that
        # presents a dead session id, which would make the re-bootstrap a
        # no-op and every poll return [] until process restart. A service with
        # genuinely zero free slots still renders the office forms, so this
        # does not loop on empty results.
        # Only this host's: the poller's session is shared by every tenant.
        host = urlparse(base).hostname
        for c in list(http.cookies):
            if c.domain.lstrip(".") == host:
                http.cookies.clear(c.domain, c.path, c.name)
        # Every tenant on this host lost its cookie with them, so each must
        # bootstrap again; a stale ready mark would poll with no session and
        # clear the host for the next sibling, every cycle.
        http._tevis_ready = {k for k in http._tevis_ready
                             if urlparse(k[0]).hostname != host}
        _bootstrap_session(http, base, md)
        html = _fetch_location_page(http, base, scfg, plan)
        if not _has_office_list(html):
            raise RuntimeError(f"tevis {plan.city}: no office list after re-login")
    slots = parse_slots(html, service_id=plan.appointment_type,
                        locations=plan.locations)
    if scfg.get("read_times"):
        slots = [t for s in slots
                 for t in _read_times(http, base, scfg, plan, html, s)]
    return slots
