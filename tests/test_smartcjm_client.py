from unittest.mock import patch, MagicMock
from app.models import PollPlan
from app.scrapers.smartcjm import poll

LEIPZIG_BASE = "https://terminvereinbarung.leipzig.de/m/leipzig-ba/extern/calendar"

def _mock_session(redirect_url: str, services_html: str, locations_html: str):
    sess = MagicMock()
    # Each .get / .post returns an object with .url and .text
    sess.get.return_value = MagicMock(url=redirect_url, text="", status_code=200)
    sess.post.side_effect = [
        MagicMock(text=services_html, status_code=200, url=""),
        MagicMock(text=locations_html, status_code=200, url=""),
    ]
    return sess

def test_poll_returns_slots_from_locations_response():
    plan = PollPlan(city="leipzig",
                    appointment_type="29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
                    locations="all")
    redirect = f"{LEIPZIG_BASE}/?wsid=fake-wsid&uid=b76cab25"
    locations_html = (
        '<ol data-testid="month_ol-1">'
        '<li data-testid="slot_button_li-1">'
        '<button onclick="return appointment_reserve(\'2026-06-10T10%3a30%3a00%2b02%3a00\','
        ' \'10\', \'loc-1\', \'svc-1\');"></button>'
        '</li></ol>'
    )
    sess = _mock_session(redirect_url=redirect,
                         services_html="",
                         locations_html=locations_html)
    slots = poll(plan, http=sess)
    assert len(slots) == 1
    assert slots[0].date == "2026-06-10"
    assert slots[0].time_str == "10:30"
    assert slots[0].location_uuid == "loc-1"
    assert slots[0].service_uuid == "svc-1"
    assert slots[0].booking_token == "2026-06-10T10%3a30%3a00%2b02%3a00"

def test_poll_session_expired_returns_empty():
    plan = PollPlan(city="leipzig", appointment_type="x", locations="all")
    redirect = f"{LEIPZIG_BASE}/?wsid=fake&uid=b76cab25"
    sess = _mock_session(redirect_url=redirect,
                         services_html="",
                         locations_html="Session abgelaufen")
    assert poll(plan, http=sess) == []
