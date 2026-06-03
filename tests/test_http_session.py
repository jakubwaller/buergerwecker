from unittest.mock import patch
from app.http_session import CountingSession


def test_counting_session_starts_at_zero():
    assert CountingSession().request_count == 0


def test_counting_session_counts_get_post_and_request():
    s = CountingSession()
    # Patch the parent so no real network call happens; CountingSession.request
    # (the subclass override) still runs and increments.
    with patch("requests.sessions.Session.request", return_value="resp") as parent:
        s.get("http://example.test")
        s.post("http://example.test")
        s.request("GET", "http://example.test")
    assert s.request_count == 3
    assert parent.call_count == 3
