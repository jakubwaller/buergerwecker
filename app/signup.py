"""What a sign-up asks for, validated against the tenant's catalog.

Shared by the website's form (`app/web.py`) and the app's JSON API
(`app/api.py`): the two are different ways of picking a service, and both
have to apply the same rules, or one of them becomes the way around the
other. An unknown appointment_type used to be stored anyway, became a
PollPlan the poller sent upstream every cycle, and counted toward
MAX_PLANS_PER_CITY until real visitors were told the wait-list was full.
"""
from __future__ import annotations
from datetime import time as time_cls

from app.models import Filter


class FormError(Exception):
    """A rejected sign-up or edit; `key` names the message (see
    `web._RESULT_MESSAGES`)."""
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def parse_hhmm(s: str) -> time_cls:
    """'HH:MM' (a trailing ':SS' is tolerated) → time. Raises ValueError on
    anything else, including out-of-range hours; the caller turns that into a
    400 rather than letting it surface as a 500."""
    parts = (s or "").strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"not HH:MM: {s!r}")
    return time_cls(int(parts[0]), int(parts[1]))


def parse_max_days(raw) -> int | None:
    """'Only slots within the next N days'; ''/0/invalid → no limit."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    raw = (str(raw) if raw is not None else "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return None


def build_filter(catalog, *, appointment_type, locations, all_locations: bool,
                 weekdays, time_start, time_end, max_days_ahead,
                 consent_special: bool) -> tuple[Filter, bool]:
    """The Filter a sign-up or an edit asks for. Returns (filter, is_sensitive);
    raises FormError.

    Every id has to be one the catalog offers. `locations` is a list of ids
    (empty, or `all_locations`, means every office). `weekdays` is a list of
    ISO weekday numbers, any form; an empty list means every day.

    Special-category services (Art. 9 GDPR) need the separate explicit
    consent on top of the opt-in. Enforced here rather than in the form
    because the box is hidden by script while an ordinary service is
    selected, and because a request need never have rendered the page.
    """
    atype = str(appointment_type or "").strip()
    if not atype:
        raise FormError("missing_type")
    if atype not in catalog.appointment_types.values():
        raise FormError("unknown_type")
    sensitive = catalog.is_sensitive(atype)
    if sensitive and not consent_special:
        raise FormError("consent_required")
    loc_list = [str(loc) for loc in (locations or [])]
    if not all_locations and loc_list:
        known = set(catalog.locations.values())
        if any(loc not in known for loc in loc_list):
            raise FormError("unknown_location")
    locs = "all" if all_locations or not loc_list else loc_list
    days = []
    for d in weekdays or []:
        s = str(d)
        if s.isdigit() and 1 <= int(s) <= 7:
            days.append(int(s))
    if not days:
        days = [1, 2, 3, 4, 5, 6, 7]
    try:
        start = parse_hhmm(str(time_start or "00:00"))
        end = parse_hhmm(str(time_end or "23:59"))
    except ValueError:
        raise FormError("invalid_time") from None
    if start > end:
        # An empty window can never match; nobody means to ask for one.
        raise FormError("invalid_time")
    return Filter(
        appointment_types=[atype],
        locations=locs,
        weekdays=days,
        time_window_start=start,
        time_window_end=end,
        max_days_ahead=parse_max_days(max_days_ahead),
    ), sensitive
