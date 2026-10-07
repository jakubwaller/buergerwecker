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

def app_share(cap: int) -> int:
    """How many of a city's services app subscriptions may hold on their own:
    half the plan cap, rounded down."""
    return cap // 2


def cap_refuses(mail_services: set[str], app_services: set[str],
                wanted: list[str], *, cap: int, push: bool,
                own: set[str] = frozenset()) -> bool:
    """Would a subscription to `wanted` be turned away by the city's plan cap?

    The cap counts services, not plans: past the cap build_plans collapses a
    service's office variants into one "all" plan, so what is left over is one
    plan per distinct service. `mail_services` are the services at least one
    live mail subscription watches (*mail-held*), `app_services` those at
    least one live app subscription watches; a service watched by app
    subscriptions and by no mail subscription is *app-held*. See
    `app.repo.city_services` for "live". The caller leaves out the
    subscription being edited or renewed, and passes what it watches while
    live as `own`: those services are polled now, so keeping them is never
    "new" (without it, a city above the cap refused its own subscribers'
    edits and renewals — security review of #109).

    Mail: refused only for a service nobody polls yet, and only when the
    mail-held services, plus any app-held services past the app's share,
    would then exceed the cap. App-held services within the share never
    count against a mail subscriber, however many devices hold them, and a
    service with a mail subscriber on it (or any polled service) is never
    refused to one.

    App: refused when
    - the service is not polled yet and everything polled would then exceed
      the cap (the app never gets a plan the website would be refused), or
    - the service is not mail-held and the app-held services, counting it,
      would exceed `app_share(cap)`. Joining or renewing on an app-held
      service is judged too, not only adding one.
    Joining a mail-held service is never refused to the app.

    App-held services past the share exist only by *conversion*: app
    subscriptions joined a mail-held service and its last mail subscriber
    left. They must count against mail: otherwise mail fills its cap, the app
    joins every service, mail leaves, mail fills its cap again, and the city
    is polled for another cap's worth of services every round (security
    review 2026-10-07: 16 → 96 Bonn services in six rounds). Counted, the
    invariant holds: a city is never polled for more than cap + cap // 2
    services (the app took its half first, mail then filled its own cap;
    conversions change who holds a service, never how many are polled).
    The excess drains: no renewal on an app-held service is accepted while
    app-held services are past the share, so they end with their terms."""
    wanted_set = set(wanted)
    polled = mail_services | app_services
    new = wanted_set - polled - own
    if not push:
        app_held = app_services - mail_services
        excess = max(0, len(app_held) - app_share(cap))
        return bool(new) and len(mail_services) + excess + len(new) > cap
    not_mail_held = wanted_set - mail_services
    app_held_after = (app_services - mail_services) | not_mail_held
    if not_mail_held and len(app_held_after) > app_share(cap):
        return True
    return bool(new) and len(polled) + len(new) > cap


def refused_by_plan_cap(conn, city: str, f: Filter, *, max_plans_per_city: int,
                        push: bool, exclude_id: int | None = None) -> bool:
    """`cap_refuses` against the database, for a sign-up (or an edit or a
    renewal, leaving out the subscription itself: `exclude_id`) of `f` in
    `city`. The counts are database rows, so they hold across workers."""
    from app.repo import city_services, own_live_services
    mail, app = city_services(conn, city, exclude_id=exclude_id)
    return cap_refuses(mail, app, list(f.appointment_types),
                       cap=cap_for_city(city, max_plans_per_city), push=push,
                       own=own_live_services(conn, exclude_id))
