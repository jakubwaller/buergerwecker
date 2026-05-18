from pathlib import Path
from app.scrapers.smartcjm import parse_slots

FIXTURES = Path(__file__).parent / "fixtures"

def test_parse_with_slots():
    html = (FIXTURES / "leipzig_with_slots.html").read_text(encoding="utf-8")
    slots = parse_slots(html)
    assert len(slots) > 0
    s = slots[0]
    assert s.date  # ISO date YYYY-MM-DD
    assert ":" in s.time_str
    assert s.booking_token  # opaque

def test_parse_no_slots():
    html = (FIXTURES / "leipzig_no_slots.html").read_text(encoding="utf-8")
    assert parse_slots(html) == []

def test_session_expired_returns_empty():
    html = (FIXTURES / "leipzig_session_expired.html").read_text(encoding="utf-8")
    assert parse_slots(html) == []
