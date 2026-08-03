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

def test_would_exceed_cap_signals_overflow():
    from app.planning import would_exceed_cap
    # Cap of 3; existing has 3 distinct types each with one "all" plan
    existing = [
        ("leipzig", make_filter(["A"], "all")),
        ("leipzig", make_filter(["B"], "all")),
        ("leipzig", make_filter(["C"], "all")),
    ]
    new = make_filter(["D"], "all")
    assert would_exceed_cap(existing, "leipzig", new, max_plans_per_city=3) is True
    # Same as existing → no overflow
    same = make_filter(["A"], "all")
    assert would_exceed_cap(existing, "leipzig", same, max_plans_per_city=3) is False

def test_per_tenant_max_plans_lifts_the_global_cap(monkeypatch):
    """Münster's Standesamt offers 34 Anliegen at a single Standort, so the
    per-type "all" collapse frees nothing and the global cap of 16 would 503
    the 17th distinct subscription. `max_plans` in scraper_config raises it for
    that tenant alone."""
    from app.planning import cap_for_city, would_exceed_cap
    assert cap_for_city("muenster-standesamt", 16) == 40
    assert cap_for_city("muenster", 16) == 16          # no override → global
    assert cap_for_city("no-such-tenant", 16) == 16    # unreadable → global

    subs = [("muenster-standesamt", make_filter([f"svc-{i}"], "all"))
            for i in range(20)]
    plans = build_plans(subs, max_plans_per_city=16)
    assert len(plans) == 20                            # not collapsed
    assert would_exceed_cap(subs, "muenster-standesamt",
                            make_filter(["svc-99"], "all"),
                            max_plans_per_city=16) is False
