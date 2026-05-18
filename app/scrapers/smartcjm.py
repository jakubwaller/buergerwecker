from __future__ import annotations
import re
import urllib.parse
from bs4 import BeautifulSoup
from app.models import Slot

APPOINTMENT_RESERVE_RE = re.compile(
    r"appointment_reserve\(\s*"
    r"'([^']+)'\s*,\s*"   # encoded datetime
    r"'(\d+)'\s*,\s*"     # duration minutes
    r"'([^']+)'\s*,\s*"   # location uuid
    r"'([^']+)'\s*\)"     # service uuid
)
SLOT_LI_TESTID_RE = re.compile(r"^slot_button_li-\d+$")

def parse_slots(html: str) -> list[Slot]:
    """Parse Smart-CJM search-result HTML into Slot records."""
    if "Session abgelaufen" in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []
    for li in soup.find_all("li", attrs={"data-testid": SLOT_LI_TESTID_RE}):
        btn = li.find("button")
        if not btn:
            continue
        onclick = btn.get("onclick", "")
        m = APPOINTMENT_RESERVE_RE.search(onclick)
        if not m:
            continue
        encoded_dt, _duration, location_uuid, service_uuid = m.groups()
        dt = urllib.parse.unquote(encoded_dt)
        if "T" not in dt:
            continue
        date_part, time_part = dt.split("T", 1)
        time_str = time_part[:5]  # "HH:MM"
        slots.append(Slot(
            date=date_part,
            time_str=time_str,
            location_uuid=location_uuid,
            service_uuid=service_uuid,
            booking_token=encoded_dt,
        ))
    return slots


import requests
from app.models import PollPlan
from app.catalog import load_catalog

def _acquire_wsid(http: requests.Session, base_url: str, uid: str) -> str:
    r = http.get(
        f"{base_url}/search_result?search_mode=earliest&uid={uid}",
        timeout=30, allow_redirects=True,
    )
    if "wsid=" not in r.url:
        raise RuntimeError("wsid not found in redirect URL")
    return r.url.split("wsid=", 1)[1].split("&", 1)[0]

def _post_services(http: requests.Session, wsid: str, plan: PollPlan,
                   catalog, scfg: dict) -> None:
    parts = []
    for code in catalog.appointment_types.values():
        amount = "1" if code == plan.appointment_type else ""
        parts.append(f"services={code}")
        parts.append(f"service_{code}_amount={amount}")
    body = (
        f"action_type=&steps={scfg['steps']}&"
        "step_current=services&step_current_index=0&step_goto=%2B1&services=&"
        + "&".join(parts)
    )
    http.post(
        f"{scfg['base_url']}/?uid={scfg['uid']}&wsid={wsid}&lang=de&rev=HL0Ur#top",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body, timeout=30,
    )

def _post_locations(http: requests.Session, wsid: str, plan: PollPlan,
                    catalog, scfg: dict) -> str:
    if plan.locations == "all":
        locations_all = "1"
        loc_uuids = list(catalog.locations.values())
    else:
        locations_all = ""
        loc_uuids = list(plan.locations)
    loc_parts = "&".join(f"locations={u}" for u in loc_uuids)
    body = (
        f"action_type=search&steps={scfg['steps']}&"
        "step_current=locations&step_current_index=1&step_goto=%2B1&"
        f"locations_selected_all={locations_all}&{loc_parts}"
    )
    r = http.post(
        f"{scfg['base_url']}/?uid={scfg['uid']}&wsid={wsid}&lang=de&",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body, timeout=30,
    )
    return r.text

def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """Run the 3-step Smart-CJM flow against the city's tenant. Returns parsed slots."""
    catalog = load_catalog(plan.city)
    scfg = catalog.scraper_config
    if scfg.get("vendor") != "smartcjm":
        raise RuntimeError(
            f"city {plan.city} not configured for smartcjm scraper "
            f"(vendor={scfg.get('vendor')})"
        )
    wsid = _acquire_wsid(http, scfg["base_url"], scfg["uid"])
    _post_services(http, wsid, plan, catalog, scfg)
    html = _post_locations(http, wsid, plan, catalog, scfg)
    return parse_slots(html)
