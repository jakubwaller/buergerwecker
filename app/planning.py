from __future__ import annotations
from collections import OrderedDict
from app.models import Filter, PollPlan

def cap_for_city(city: str, default: int) -> int:
    """Per-tenant plan cap: `max_plans` from scraper_config.json, else `default`.

    A tenant with many Anliegen at a single office hits the global
    MAX_PLANS_PER_CITY long before it poses a load problem — Münster's
    Standesamt offers 36 services at one Standort, and the per-type "all"
    collapse frees nothing when there is only one office, so the 17th distinct
    Anliegen anyone subscribes to would be refused with a 503. Raising the
    ceiling for that tenant alone keeps the global default protecting the hosts
    that need it (Bochum 429s well below it).

    Reads the catalog the same way cycle.poll_interval_for does; an unknown or
    unreadable tenant falls back to the default rather than failing a cycle.
    """
    try:
        from app.catalog import load_catalog
        raw = load_catalog(city).scraper_config.get("max_plans")
        return int(raw) if raw else default
    except Exception:
        return default

def plan_for_subscription(city: str, f: Filter) -> list[PollPlan]:
    """A subscription that wants multiple appointment types fans into multiple plans."""
    out = []
    for atype in f.appointment_types:
        out.append(PollPlan(city=city, appointment_type=atype, locations=f.locations))
    return out

def build_plans(subscriptions: list[tuple[str, Filter]],
                *, max_plans_per_city: int) -> list[PollPlan]:
    """Return a deduplicated list of polling plans, collapsing into "all" if cap exceeded."""
    # Step 1: gather all needed plans
    plans: OrderedDict[str, PollPlan] = OrderedDict()
    for city, f in subscriptions:
        for p in plan_for_subscription(city, f):
            plans.setdefault(p.key(), p)
    # Step 2: count per city
    per_city: dict[str, list[PollPlan]] = {}
    for p in plans.values():
        per_city.setdefault(p.city, []).append(p)
    # Step 3: collapse overflow to per-type "all"
    out: list[PollPlan] = []
    for city, city_plans in per_city.items():
        if len(city_plans) <= cap_for_city(city, max_plans_per_city):
            out.extend(city_plans)
            continue
        # Group by appointment_type, replace each group with one "all" plan
        by_type: dict[str, list[PollPlan]] = {}
        for p in city_plans:
            by_type.setdefault(p.appointment_type, []).append(p)
        for atype, _ in by_type.items():
            out.append(PollPlan(city=city, appointment_type=atype, locations="all"))
    return out

def would_exceed_cap(existing: list[tuple[str, Filter]],
                     new_city: str, new_filter: Filter,
                     *, max_plans_per_city: int) -> bool:
    """Predict whether adding (new_city, new_filter) would exceed the cap
    EVEN AFTER the per-type "all" collapse. Used by /subscribe to return 503."""
    augmented = existing + [(new_city, new_filter)]
    plans = build_plans(augmented, max_plans_per_city=max_plans_per_city)
    per_city: dict[str, int] = {}
    for p in plans:
        per_city[p.city] = per_city.get(p.city, 0) + 1
    return any(count > cap_for_city(city, max_plans_per_city)
               for city, count in per_city.items())
