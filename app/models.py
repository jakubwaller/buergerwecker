from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Union

LocationsSpec = Union[list[str], str]  # list of UUIDs, or "all"

@dataclass(frozen=True)
class Filter:
    appointment_types: list[str]
    locations: LocationsSpec
    weekdays: list[int]                # ISO 8601: 1=Mon … 7=Sun
    time_window_start: time
    time_window_end: time
    # Only notify about slots within the next N days (None = no limit). A slot
    # further out is deliberately not marked seen: if it is still free once it
    # enters the window, a later cycle notifies about it then.
    max_days_ahead: int | None = None

    def to_json(self) -> str:
        return json.dumps({
            "appointment_types": list(self.appointment_types),
            "locations": self.locations if self.locations == "all"
                         else list(self.locations),
            "weekdays": list(self.weekdays),
            "time_window": {
                "start": self.time_window_start.strftime("%H:%M"),
                "end":   self.time_window_end.strftime("%H:%M"),
            },
            "max_days_ahead": self.max_days_ahead,
        })

    @classmethod
    def from_json(cls, s: str) -> "Filter":
        d = json.loads(s)
        tw = d.get("time_window", {"start": "00:00", "end": "23:59"})
        return cls(
            appointment_types=list(d["appointment_types"]),
            locations="all" if d["locations"] == "all" else list(d["locations"]),
            weekdays=list(d.get("weekdays", [1, 2, 3, 4, 5, 6, 7])),
            time_window_start=_parse_hhmm(tw["start"]),
            time_window_end=_parse_hhmm(tw["end"]),
            # Absent in rows written before this field existed → no limit.
            # 0 also normalizes to no-limit: the form can't produce it, and the
            # consumers (matches() vs digest/manage truthiness) would otherwise
            # disagree on what 0 means.
            max_days_ahead=d.get("max_days_ahead") or None,
        )

def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

@dataclass(frozen=True)
class Slot:
    date: str          # YYYY-MM-DD
    time_str: str      # HH:MM
    location_uuid: str
    service_uuid: str  # appointment type this slot was searched for (from the plan)
    booking_token: str # opaque, session-bound — excluded from hash
    resource_uuid: str = ""  # the counter/staff resource the upstream button carries

    def hash(self) -> str:
        # Identity is (day, time, office, service). The resource is deliberately
        # excluded: two counters offering the same service slot at the same office
        # and minute are one notification for the subscriber. booking_token is
        # session-bound and also excluded.
        payload = f"{self.date}|{self.time_str}|{self.location_uuid}|{self.service_uuid}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class Subscription:
    id: int
    email: str
    city: str
    language: str       # 'de' or 'en'
    sub_filter: Filter  # named 'sub_filter' to avoid shadowing builtin filter()
    created_at: datetime
    confirmed_at: datetime | None
    last_notified_at: datetime | None
    expires_at: datetime
    reminder_sent_at: datetime | None
    heartbeat_30d_at: datetime | None
    heartbeat_60d_at: datetime | None
    deleted_at: datetime | None

@dataclass(frozen=True)
class PollPlan:
    """A polling unit shared across subscriptions with the same scrape needs."""
    city: str
    appointment_type: str
    locations: LocationsSpec  # list of UUIDs OR "all"

    def key(self) -> str:
        if self.locations == "all":
            locs = "all"
        else:
            locs = ",".join(sorted(self.locations))
        return f"{self.city}|{self.appointment_type}|{locs}"
