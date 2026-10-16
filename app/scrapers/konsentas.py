"""konsentas scraper (Girona Software; Stuttgart Führerscheinstelle).

Notify-only. The public booking page is a JSON API behind a guest token:
`getOtaStartUp` opens a guest signup and hands out the token,
`postOtaNextStep` with one service moves that signup to the slot step, and
`brick_ota_termin_getTimeslot` lists the slots in a date range. We stop at the
slot step: choosing a slot, personal data and confirmation stay on the city's
page, with the person booking.

Every getOtaStartUp opens a signup record on the city's side (the token
carries its `signup_id`), so ONE signup per form is kept for most of the
token's 8-hour life and walked back (`postOtaPreStep`) and forward to each
service in turn, rather than a fresh one per poll. A service that fails is
that service's problem: the signup is walked back and kept for the others.
Only a token the city no longer accepts is replaced, and never more than once
per `_REOPEN_SECONDS`, whatever goes wrong, so a fault can cost the city a
couple of records an hour but never one per poll. The poller polls plans one
after another in one process, so this module-level state is never raced.
"""
from __future__ import annotations
import time as _time
import requests
from app.models import Slot, PollPlan
from app.catalog import load_catalog

# The token lives 8 h (exp - iat); renew an hour early rather than meet a 401.
_TOKEN_TTL_SECONDS = 7 * 3600
# Floor between two getOtaStartUp calls per form, failed attempts included.
_REOPEN_SECONDS = 30 * 60
# The city shows three weeks ahead; a wider window costs nothing.
_HORIZON_SECONDS = 42 * 86400
# Codes for a token the city does not accept, probed 2026-10-16: 345 for none
# or an unknown one (HTTP 403), 731 for a failed signature check (HTTP 500).
_STALE_CODES = {345, 731}
# (base_url, form_id) -> {"token", "op_id", "op_type", "at"}
_SESSIONS: dict[tuple[str, int], dict] = {}
# (base_url, form_id) -> when getOtaStartUp was last called
_OPENED: dict[tuple[str, int], float] = {}


class _StaleSignup(RuntimeError):
    """The city no longer accepts our token."""


def _now() -> float:
    return _time.monotonic()


def parse_slots(data: dict, *, service_id: str, location_id: str) -> list[Slot]:
    """Free slots from a getTimeslot answer's `data`.

    Every day in the range comes back; a fully booked day is a placeholder with
    places 0 and time "" (className termin-booked-out). A real slot has `time`
    in minutes after midnight and is free while places > places_used, the
    browser's own test (_createEvent). Several staff rows can carry the same
    minute; the browser merges them (_dedupeTermins), and so do we.
    """
    slots: list[Slot] = []
    seen: set[tuple[str, str]] = set()
    for t in data.get("termins") or []:
        minutes = str(t.get("time", ""))
        if not minutes.isdigit():
            continue
        if int(t.get("places") or 0) <= int(t.get("places_used") or 0):
            continue
        yd = str(t["yeardate"])
        date_iso = f"{yd[:4]}-{yd[4:6]}-{yd[6:8]}"
        time_str = f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"
        if (date_iso, time_str) in seen:
            continue
        seen.add((date_iso, time_str))
        slots.append(Slot(
            date=date_iso,
            time_str=time_str,
            location_uuid=location_id,
            service_uuid=service_id,
            # No per-slot deep link: the slot belongs to our guest signup, so
            # digests link to the booking start page (catalog.booking_start_url).
            booking_token=f"{date_iso}T{time_str}",
        ))
    return slots


def _data(r) -> dict:
    """The `data` of a konsentas answer; anything but HTTP 200 + code 3 raises,
    as _StaleSignup when it is the token that was refused."""
    try:
        body = r.json()
    except ValueError:  # an HTML error page from a proxy or firewall
        body = {}
    msg = (f"konsentas HTTP {r.status_code} code {body.get('code')}: "
           f"{body.get('msg')}")
    if r.status_code in (401, 403) or body.get("code") in _STALE_CODES:
        raise _StaleSignup(msg)
    if r.status_code != 200 or body.get("code") != 3:
        raise RuntimeError(msg)
    return body.get("data") or {}


def _headers(s: dict) -> dict:
    # Per request, never on the session: the poller's session is shared by
    # every tenant and vendor.
    return {"Authorization": f"Bearer {s['token']}",
            "X-Requested-With": "XMLHttpRequest"}


def _step(http: requests.Session, base: str, s: dict, endpoint: str,
          fields: dict) -> None:
    # Multipart, as the browser sends it (otaquery requServerWithFiles).
    try:
        r = http.post(f"{base}/api/{endpoint}/", headers=_headers(s),
                      files={k: (None, v) for k, v in fields.items()},
                      timeout=30)
    except requests.RequestException:
        # The city may have taken the step anyway; we no longer know where
        # the signup stands, so the next poll abandons it.
        s["op_type"] = None
        raise
    data = _data(r)
    s["op_id"], s["op_type"] = data.get("op_id"), data.get("op_type")


def _back(http: requests.Session, base: str, s: dict) -> None:
    _step(http, base, s, "postOtaPreStep",
          {"op_id": s["op_id"], "navigation": "-1"})


def _signup_at_services(http: requests.Session, base: str,
                        form_id: int) -> dict:
    """The cached signup walked back to the service step, or a fresh one.

    Only ever step forward from the service step: a signup anywhere else is
    abandoned, never pushed on, because past a review step comes a submission.
    """
    key = (base, form_id)
    s = _SESSIONS.get(key)
    if s is not None and _now() - s["at"] < _TOKEN_TTL_SECONDS:
        try:
            if s["op_type"] == "ota_termin":
                _back(http, base, s)
            if s["op_type"] == "ota_processes":
                return s
        except RuntimeError:
            pass  # the signup, not one service, is broken: replace it below
    _SESSIONS.pop(key, None)
    opened = _OPENED.get(key)
    if opened is not None and _now() - opened < _REOPEN_SECONDS:
        raise RuntimeError(f"konsentas: a signup was opened "
                           f"{int(_now() - opened)} s ago, not opening another")
    _OPENED[key] = _now()
    r = http.get(f"{base}/api/getOtaStartUp/",
                 params={"signupform_id": form_id, "userauth": "",
                         "queryParameter[signup_new]": "1"},
                 headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
    data = _data(r)
    if data.get("op_type") != "ota_processes":
        raise RuntimeError(f"konsentas: a new signup starts at step "
                           f"{data.get('op_type')!r}, not the service step")
    s = {"token": data["ota_jwt"], "op_id": data.get("op_id"),
         "op_type": "ota_processes", "at": _now()}
    _SESSIONS[key] = s
    return s


def _fetch(http: requests.Session, base: str, form_id: int,
           process: str) -> dict:
    s = _signup_at_services(http, base, form_id)
    _step(http, base, s, "postOtaNextStep",
          {"formdata[processes][0]": process, "op_id": s["op_id"],
           "navigation": "1"})
    if s["op_type"] != "ota_termin":
        # Walk back so the other services keep this signup; if that does not
        # land on the service step, the next poll abandons it.
        landed = s["op_type"]
        if landed != "ota_processes":  # still on the service step: refused
            _back(http, base, s)
        raise RuntimeError(f"konsentas: service {process} led to step "
                           f"{landed!r}, not the slot step")
    start = int(_time.time())
    r = http.get(f"{base}/api/brick_ota_termin_getTimeslot/",
                 params={"start": start, "end": start + _HORIZON_SECONDS},
                 headers=_headers(s), timeout=30)
    return _data(r)


def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """Every free slot for the plan's service in the next weeks."""
    catalog = load_catalog(plan.city)
    scfg = catalog.scraper_config
    if scfg.get("vendor") != "konsentas":
        raise RuntimeError(
            f"city {plan.city} not configured for konsentas scraper "
            f"(vendor={scfg.get('vendor')})"
        )
    base = scfg["base_url"].rstrip("/")
    form_id = int(scfg["form_id"])
    try:
        data = _fetch(http, base, form_id, plan.appointment_type)
    except _StaleSignup:
        # The city dropped our token early: once more with a fresh signup,
        # which _REOPEN_SECONDS refuses if this one was itself just opened.
        _SESSIONS.pop((base, form_id), None)
        data = _fetch(http, base, form_id, plan.appointment_type)
    # One office per form; the subscriber's location filter applies downstream.
    location_id = next(iter(catalog.locations.values()))
    return parse_slots(data, service_id=plan.appointment_type,
                       location_id=location_id)
