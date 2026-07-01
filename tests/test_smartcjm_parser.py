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


# --- leipzig-abh-h tenant (Ausländerbehörde): no data-testid on slot buttons ---

ABH_SVC = "6d72c63a-31a8-4aca-9d46-c9f3f90d39ec"   # Ausgabe Aufenthaltsdokument
ABH_LOC = "8e470125-efec-47cc-bb60-4e3a072e7e67"


def test_parse_abh_slots_without_testid_markup():
    """The abh-h tenant renders the same appointment_reserve buttons but WITHOUT
    data-testid attributes; the parser must match on the onclick handler. The
    fixture's <script> block contains the appointment_reserve function
    definition — it must not produce a phantom slot."""
    html = (FIXTURES / "leipzig_abh_with_slots.html").read_text(encoding="utf-8")
    slots = parse_slots(html, service_uuid=ABH_SVC)
    assert len(slots) == 3   # exactly the fixture's buttons, nothing from <script>
    assert {(s.date, s.time_str) for s in slots} == {
        ("2026-07-02", "11:50"), ("2026-07-02", "13:20"), ("2026-07-13", "08:30"),
    }
    assert all(s.location_uuid == ABH_LOC for s in slots)
    assert all(s.service_uuid == ABH_SVC for s in slots)
    # booking_token keeps the URL-encoded datetime for the upstream query string
    assert any(s.booking_token == "2026-07-02T11%3a50%3a00%2b02%3a00" for s in slots)
