from pathlib import Path
from app.scrapers.smartcjm import parse_slots

FIXTURES = Path(__file__).parent / "fixtures"

# The slot search is server-side filtered to one service, so the service the
# slots belong to is the one we searched for — passed in by the caller. The
# button's own 4th arg is a *resource*, not the service (see the upstream JS
# signature: appointment_reserve(datetime, duration, location, resource)).
SVC = "29cd0a26-fe7a-4d65-88cd-1e05fd749c71"


def test_parse_with_slots_sets_service_from_arg_and_resource_from_button():
    html = (FIXTURES / "leipzig_with_slots.html").read_text(encoding="utf-8")
    slots = parse_slots(html, service_uuid=SVC)
    assert len(slots) > 0
    s = slots[0]
    assert s.date            # ISO date YYYY-MM-DD
    assert ":" in s.time_str
    assert s.booking_token   # opaque
    # service_uuid comes from the search context, not the button
    assert all(x.service_uuid == SVC for x in slots)
    # the button's 4th arg is captured as the resource, distinct from the service
    assert s.resource_uuid
    assert all(x.resource_uuid != SVC for x in slots)


def test_parse_no_slots():
    html = (FIXTURES / "leipzig_no_slots.html").read_text(encoding="utf-8")
    assert parse_slots(html, service_uuid=SVC) == []


def test_session_expired_returns_empty():
    html = (FIXTURES / "leipzig_session_expired.html").read_text(encoding="utf-8")
    assert parse_slots(html, service_uuid=SVC) == []
