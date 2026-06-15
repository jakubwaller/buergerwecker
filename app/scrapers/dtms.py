"""DTMS / DrivePort scraper (Hamburg "HS Einwohnerangelegenheiten", Mandant 1).

The Hamburg appointment system is DTMS ("Digitales Terminmanagement System")
operated on DrivePort by Kasse.Hamburg / Dataport OSI — a Blazor WebAssembly SPA
over a JSON REST backend at ``driveport.de/termineapi``. Unlike Leipzig's
Smart-CJM HTML flow, availability is a clean JSON API.

Anonymity note: the citizen UI funnels name/email/phone (a ``PUT
.../termine/checkstamm``) *before* rendering slots, but the slot endpoints carry
no cookie, no Authorization header, and no token derived from that step — the
request body is purely service + location + date-window. We therefore poll
availability without submitting any contact data, mirroring Leipzig's anonymous
posture. We use ``terminvorschlaege/ersteProStandort`` (earliest slot per
location, all locations) so one request per service covers the whole city.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import requests

from app.catalog import load_catalog
from app.models import PollPlan, Slot

try:
    from zoneinfo import ZoneInfo
    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    _BERLIN = None

_DEFAULT_DAYS_AHEAD = 365


def parse_slots(payload, *, service_uuid: str, locations) -> list[Slot]:
    """Parse a DTMS ``terminvorschlaege`` response into Slot records.

    The response shape is ``{"Terminvorschlaege": [{"StandortID", "Von",
    "Prio"}], "Hinweise": ...}``. ``Von`` is a naive Europe/Berlin local datetime
    (e.g. ``"2026-06-05T09:30:00"``). Both ``ersteProStandort`` (earliest per
    location) and ``halbeStunden`` (all half-hour slots) share this element
    shape. The service is stamped from the plan — we POST a single
    Dienstleistung — and there is no per-counter resource (so ``resource_uuid``
    is empty), mirroring the Smart-CJM scraper's service/resource split.

    ``locations`` is the plan's spec: ``"all"`` or a list of StandortID strings.
    We always POST for all locations and filter the response here, so the
    request shape stays identical to the one captured from the live SPA.
    """
    data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    vorschlaege = (data or {}).get("Terminvorschlaege") or []
    slots: list[Slot] = []
    for v in vorschlaege:
        standort = v.get("StandortID")
        von = v.get("Von")
        if standort is None or not von or "T" not in von:
            continue
        loc = str(standort)
        if locations != "all" and loc not in locations:
            continue
        date_part, time_part = von.split("T", 1)
        slots.append(Slot(
            date=date_part,
            time_str=time_part[:5],
            location_uuid=loc,
            service_uuid=service_uuid,
            # No upstream per-slot reservation token (booking completes inside
            # the DrivePort SPA). Synthesize "<loc>@<datetime>" so the
            # slots_cache key is unique per slot; the /go link points at the
            # portal (see cycle._build_upstream_url).
            booking_token=f"{loc}@{von}",
            resource_uuid="",
        ))
    return slots


def _berlin_midnight_iso(d: date) -> str:
    """``YYYY-MM-DDT00:00:00+02:00`` with the Europe/Berlin offset for that date.

    The captured request used a Berlin-local offset; the server treats the
    window at day granularity, so the offset is fidelity, not correctness.
    """
    if _BERLIN is not None:
        off = datetime(d.year, d.month, d.day, tzinfo=_BERLIN).strftime("%z")
        off = off[:3] + ":" + off[3:]  # "+0200" -> "+02:00"
    else:  # pragma: no cover
        off = "+01:00"
    return f"{d.isoformat()}T00:00:00{off}"


def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """Poll Hamburg DTMS availability for one service across all locations.

    Posts the captured ``ersteProStandort`` request — a single Dienstleistung,
    ``StandorteID=null`` (all locations), a today..+``days_ahead`` window — and
    parses the earliest-slot-per-location overview. No PII, cookie, or session
    token is sent (see module docstring). When the plan targets specific
    locations we still POST for all and filter the response, keeping the request
    byte-identical to the observed SPA call.
    """
    catalog = load_catalog(plan.city)
    scfg = catalog.scraper_config
    if scfg.get("vendor") != "dtms":
        raise RuntimeError(
            f"city {plan.city} not configured for dtms scraper "
            f"(vendor={scfg.get('vendor')})"
        )
    base = scfg["base_url"].rstrip("/")
    mandant = scfg["mandant"]
    days_ahead = scfg.get("days_ahead", _DEFAULT_DAYS_AHEAD)
    today = date.today()
    body = {
        "Dienstleistungen": [
            {"DienstleistungID": int(plan.appointment_type), "Anzahl": 1}
        ],
        "StandorteID": None,
        "VonTag": _berlin_midnight_iso(today),
        "BisTag": _berlin_midnight_iso(today + timedelta(days=days_ahead)),
        "Wochentagszeitraeume": [],
        "MaximaleAnzahl": None,
        "LeisteNr": None,
        "Terminsperre": True,
        "Ortsteilschluessel": None,
    }
    url = f"{base}/mandanten/{mandant}/terminvorschlaege/ersteProStandort"
    resp = http.post(
        url,
        params={"appKey": scfg["app_key"], "lang": "de"},
        json=body,
        timeout=30,
    )
    if getattr(resp, "status_code", 200) >= 400:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    return parse_slots(payload, service_uuid=plan.appointment_type,
                       locations=plan.locations)
