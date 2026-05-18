from __future__ import annotations
from datetime import date, time
from app.models import Filter, Slot

def matches(f: Filter, slot: Slot) -> bool:
    if slot.service_uuid not in f.appointment_types:
        return False
    if f.locations != "all" and slot.location_uuid not in f.locations:
        return False
    try:
        d = date.fromisoformat(slot.date)
    except ValueError:
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
