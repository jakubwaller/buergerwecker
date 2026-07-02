from __future__ import annotations
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from app.models import Filter, Slot

# Slot dates are Europe/Berlin local dates (the city sites' timezone), while
# the poller container runs UTC. "Today" for the max_days_ahead window must be
# the Berlin date, or the window silently shrinks by a day between the Berlin
# and UTC midnights (~22:00/23:00–00:00 UTC every night).
_BERLIN = ZoneInfo("Europe/Berlin")

def matches(f: Filter, slot: Slot) -> bool:
    if slot.service_uuid not in f.appointment_types:
        return False
    if f.locations != "all" and slot.location_uuid not in f.locations:
        return False
    try:
        d = date.fromisoformat(slot.date)
    except ValueError:
        return False
    if f.max_days_ahead is not None:
        today = datetime.now(_BERLIN).date()
        if (d - today).days > f.max_days_ahead:
            return False
    if d.isoweekday() not in f.weekdays:
        return False
    try:
        hh, mm = slot.time_str.split(":")
        t = time(int(hh), int(mm))
    except (ValueError, IndexError):
        return False
    if t < f.time_window_start or t > f.time_window_end:
        return False
    return True
