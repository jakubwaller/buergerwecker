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
