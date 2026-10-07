from datetime import time
from app.models import Filter
from app.planning import build_plans, plan_for_subscription

def make_filter(types, locations):
    return Filter(
        appointment_types=list(types),
        locations="all" if locations == "all" else list(locations),
        weekdays=[1,2,3,4,5,6,7],
        time_window_start=time(0,0),
        time_window_end=time(23,59),
    )

def test_build_plans_merges_same_filters():
    f = make_filter(["svc-A"], "all")
    subs = [
        ("leipzig", f), ("leipzig", f), ("leipzig", f),
    ]
    plans = build_plans(subs, max_plans_per_city=10)
    assert len(plans) == 1
    assert plans[0].city == "leipzig"
    assert plans[0].appointment_type == "svc-A"

def test_build_plans_splits_by_type():
    plans = build_plans(
        [("leipzig", make_filter(["A"], "all")),
         ("leipzig", make_filter(["B"], "all"))],
        max_plans_per_city=10,
    )
    types = sorted(p.appointment_type for p in plans)
    assert types == ["A", "B"]

def test_build_plans_collapses_to_all_when_cap_exceeded():
    """11 unique (type, location) combinations collapse to per-type "all" plans."""
    subs = []
    for i in range(11):
        subs.append(("leipzig",
                     make_filter(["svc-A"], [f"loc-{i}"])))
    plans = build_plans(subs, max_plans_per_city=10)
    # Should collapse the 11 single-location plans into one "all" plan for svc-A.
    assert len(plans) == 1
    assert plans[0].locations == "all"
    assert plans[0].appointment_type == "svc-A"

def test_a_new_service_past_the_cap_is_refused_and_a_polled_one_never():
    from app.planning import cap_refuses
    # Cap of 3; three distinct services already polled, all mail-held.
    mail = {"A", "B", "C"}
    assert cap_refuses(mail, set(), ["D"], cap=3, push=False) is True
    assert cap_refuses(mail, set(), ["A"], cap=3, push=False) is False
    # A service already polled adds no plan: never refused, even over the
    # cap (an operator lowered it), to mail or app.
    assert cap_refuses(mail | {"E"}, set(), ["A"], cap=3, push=False) is False
    assert cap_refuses(mail | {"E"}, set(), ["A"], cap=3, push=True) is False

def test_per_tenant_max_plans_lifts_the_global_cap(monkeypatch):
    """Münster's Standesamt offers 34 Anliegen at a single Standort, so the
    per-type "all" collapse frees nothing and the global cap of 16 would 503
    the 17th distinct subscription. `max_plans` in scraper_config raises it for
    that tenant alone."""
    from app.planning import cap_for_city, cap_refuses
    assert cap_for_city("muenster-standesamt", 16) == 40
    assert cap_for_city("muenster", 16) == 16          # no override → global
    assert cap_for_city("no-such-tenant", 16) == 16    # unreadable → global

    subs = [("muenster-standesamt", make_filter([f"svc-{i}"], "all"))
            for i in range(20)]
    plans = build_plans(subs, max_plans_per_city=16)
    assert len(plans) == 20                            # not collapsed
    mail = {f"svc-{i}" for i in range(20)}
    assert cap_refuses(mail, set(), ["svc-99"],
                       cap=cap_for_city("muenster-standesamt", 16), push=False) is False
    assert cap_refuses(mail, set(), ["svc-99"],
                       cap=cap_for_city("muenster", 16), push=False) is True


# ---------------------------------------------------------------------------
# The app's share of a city (security review 2026-10-07: two verified devices
# held all 16 Bonn services, and every website visitor got waitlist_full)

def _svcs(prefix, n):
    return {f"{prefix}{i}" for i in range(n)}


def test_app_held_services_never_count_against_a_mail_subscriber():
    from app.planning import cap_refuses
    # The app holds its whole share, 8 of 16; mail has 8 of its own: 16
    # services polled. The website still has its full cap.
    app = _svcs("a", 8)
    mail = _svcs("m", 8)
    assert cap_refuses(mail, app, ["m-new"], cap=16, push=False) is False
    # Mail fills its own 16, so 24 are polled; the 17th mail service is
    # refused by mail alone.
    mail = _svcs("m", 16)
    assert cap_refuses(mail, app, ["m-new"], cap=16, push=False) is True
    assert cap_refuses(mail, app, ["m-new"], cap=16 + 1, push=False) is False


def test_a_mail_subscriber_may_join_any_polled_service():
    from app.planning import cap_refuses
    mail = _svcs("m", 16)                       # mail at its cap
    app = _svcs("a", 8)
    assert cap_refuses(mail, app, ["m0"], cap=16, push=False) is False   # mail-held
    assert cap_refuses(mail, app, ["a0"], cap=16, push=False) is False   # app-held


def test_the_app_holds_at_most_half_the_cap_on_its_own():
    from app.planning import app_share, cap_refuses
    assert app_share(16) == 8 and app_share(5) == 2 and app_share(1) == 0
    app = _svcs("a", 7)
    assert cap_refuses(set(), app, ["a-new"], cap=16, push=True) is False    # the 8th
    app = _svcs("a", 8)
    assert cap_refuses(set(), app, ["a-new"], cap=16, push=True) is True     # the 9th
    # Joining a service the app already holds is fine within the share, and
    # joining a mail-held one is always fine.
    assert cap_refuses(set(), app, ["a0"], cap=16, push=True) is False
    assert cap_refuses({"m0"}, app, ["m0"], cap=16, push=True) is False
    # A service with a mail subscriber on it is mail-held, not the app's.
    shared = _svcs("a", 8)
    assert cap_refuses({"a0"}, shared, ["a-new"], cap=16, push=True) is False


def test_the_app_never_gets_a_plan_the_website_would_be_refused():
    from app.planning import cap_refuses
    mail = _svcs("m", 15)
    assert cap_refuses(mail, set(), ["a-new"], cap=16, push=True) is False
    mail = _svcs("m", 16)
    assert cap_refuses(mail, set(), ["a-new"], cap=16, push=True) is True
    # ...counting what the app already polls: 12 mail + 4 app-held = 16.
    assert cap_refuses(_svcs("m", 12), _svcs("a", 4), ["a-new"],
                       cap=16, push=True) is True


def test_app_held_services_past_the_share_drain_instead_of_renewing():
    """A service turns app-held when its last mail subscriber leaves. Over
    the share, an app subscription on an app-held service is not renewed
    (nor joined); a mail-held one still is."""
    from app.planning import cap_refuses
    app = _svcs("a", 9)                          # 8 of the app's own + 1 left by mail
    mail = {"m0"}
    for svc in ("a0", "a8"):
        assert cap_refuses(mail, app, [svc], cap=16, push=True) is True
    assert cap_refuses(mail, app | {"m0"}, ["m0"], cap=16, push=True) is False
    # Mail is not touched by any of it.
    assert cap_refuses(mail, app, ["a0"], cap=16, push=False) is False
