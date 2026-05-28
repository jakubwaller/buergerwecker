from unittest.mock import MagicMock
import pytest
from app.models import PollPlan
from app.scrapers import smartcjm
from app.scrapers.smartcjm import poll

LEIPZIG_BASE = "https://terminvereinbarung.leipzig.de/m/leipzig-ba/extern/calendar"
LEIPZIG_UID = "b76cab25-49bd-44e3-950d-aab715881ea7"

SERVICES_PAGE_HTML = (
    '<html><body><form name="x_services" '
    'action="?uid=u&amp;wsid=w&amp;lang=de&amp;rev=test-rev#top">'
    '<input type="hidden" name="__RequestVerificationToken" value="test-csrf" />'
    '</form></body></html>'
)

ONE_SLOT_HTML = (
    '<ol data-testid="month_ol-1">'
    '<li data-testid="slot_button_li-1">'
    '<button onclick="return appointment_reserve(\'2026-06-10T10%3a30%3a00%2b02%3a00\','
    ' \'10\', \'loc-1\', \'svc-1\');"></button>'
    '</li></ol>'
)


@pytest.fixture(autouse=True)
def _clear_cache():
    smartcjm._WSID_CACHE.clear()
    yield
    smartcjm._WSID_CACHE.clear()


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(smartcjm, "_now", lambda: state["t"])
    return state


def _make_get_handler(redirect_urls: list[str] | str):
    """Return a side_effect that dispatches GETs based on URL.

    For 'search_result' calls, returns the next entry from `redirect_urls`
    (rotating once exhausted). For other GETs, returns the services-step HTML.
    """
    if isinstance(redirect_urls, str):
        redirect_urls = [redirect_urls]
    state = {"i": 0}

    def _get(url, *a, **kw):
        if "search_result" in url:
            r_url = redirect_urls[min(state["i"], len(redirect_urls) - 1)]
            state["i"] += 1
            return MagicMock(url=r_url, text="", status_code=200, headers={})
        return MagicMock(url=url, text=SERVICES_PAGE_HTML, status_code=200, headers={})

    return _get


def _plan():
    return PollPlan(city="leipzig",
                    appointment_type="29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
                    locations="all")


def test_wsid_reused_within_ttl(fake_clock):
    """Two consecutive polls inside TTL → only one cache-miss GET pair."""
    sess = MagicMock()
    sess.get.side_effect = _make_get_handler(
        f"{LEIPZIG_BASE}/?wsid=cached&uid={LEIPZIG_UID}"
    )
    sess.post.side_effect = [MagicMock(text=h, status_code=200, url="", headers={})
                             for h in ["", ONE_SLOT_HTML, "", ONE_SLOT_HTML]]
    poll(_plan(), http=sess)
    fake_clock["t"] += 60.0
    poll(_plan(), http=sess)
    # Cache hit on second poll → only one full session-state acquisition
    # (= 1 wsid-acquire GET + 1 services-page GET)
    assert sess.get.call_count == 2
    assert sess.post.call_count == 4


def test_wsid_reacquired_after_ttl(fake_clock):
    """Polls more than TTL apart → two cache-miss GET pairs."""
    sess = MagicMock()
    sess.get.side_effect = _make_get_handler([
        f"{LEIPZIG_BASE}/?wsid=first&uid={LEIPZIG_UID}",
        f"{LEIPZIG_BASE}/?wsid=second&uid={LEIPZIG_UID}",
    ])
    sess.post.side_effect = [MagicMock(text=h, status_code=200, url="", headers={})
                             for h in ["", ONE_SLOT_HTML, "", ONE_SLOT_HTML]]
    poll(_plan(), http=sess)
    fake_clock["t"] += smartcjm._WSID_TTL_SECONDS + 1.0
    poll(_plan(), http=sess)
    assert sess.get.call_count == 4  # 2 acquisitions × (wsid GET + services-page GET)


def test_session_expired_triggers_reacquire_and_retry(fake_clock):
    """Locations response = 'Session abgelaufen' → invalidate, re-acquire, retry once."""
    sess = MagicMock()
    sess.get.side_effect = _make_get_handler([
        f"{LEIPZIG_BASE}/?wsid=stale&uid={LEIPZIG_UID}",
        f"{LEIPZIG_BASE}/?wsid=fresh&uid={LEIPZIG_UID}",
    ])
    sess.post.side_effect = [
        MagicMock(text="", status_code=200, url="", headers={}),
        MagicMock(text="Session abgelaufen", status_code=200, url="", headers={}),
        MagicMock(text="", status_code=200, url="", headers={}),
        MagicMock(text=ONE_SLOT_HTML, status_code=200, url="", headers={}),
    ]
    slots = poll(_plan(), http=sess)
    assert len(slots) == 1
    assert sess.get.call_count == 4
    assert sess.post.call_count == 4


def test_double_session_expired_returns_empty_no_loop(fake_clock):
    sess = MagicMock()
    sess.get.side_effect = _make_get_handler([
        f"{LEIPZIG_BASE}/?wsid=a&uid={LEIPZIG_UID}",
        f"{LEIPZIG_BASE}/?wsid=b&uid={LEIPZIG_UID}",
    ])
    sess.post.side_effect = [
        MagicMock(text="", status_code=200, url="", headers={}),
        MagicMock(text="Session abgelaufen", status_code=200, url="", headers={}),
        MagicMock(text="", status_code=200, url="", headers={}),
        MagicMock(text="Session abgelaufen", status_code=200, url="", headers={}),
    ]
    assert poll(_plan(), http=sess) == []
    assert sess.get.call_count == 4
    assert sess.post.call_count == 4


def test_session_expired_invalidates_cache_for_subsequent_polls(fake_clock):
    """After retry succeeds, the next poll reuses the fresh cached state — no extra GETs."""
    sess = MagicMock()
    sess.get.side_effect = _make_get_handler([
        f"{LEIPZIG_BASE}/?wsid=stale&uid={LEIPZIG_UID}",
        f"{LEIPZIG_BASE}/?wsid=fresh&uid={LEIPZIG_UID}",
    ])
    sess.post.side_effect = [
        MagicMock(text="", status_code=200, url="", headers={}),                  # svc 1
        MagicMock(text="Session abgelaufen", status_code=200, url="", headers={}),# loc 1
        MagicMock(text="", status_code=200, url="", headers={}),                  # svc 2 retry
        MagicMock(text=ONE_SLOT_HTML, status_code=200, url="", headers={}),       # loc 2 retry
        MagicMock(text="", status_code=200, url="", headers={}),                  # svc 3 next poll
        MagicMock(text=ONE_SLOT_HTML, status_code=200, url="", headers={}),       # loc 3
    ]
    poll(_plan(), http=sess)
    fake_clock["t"] += 60.0
    poll(_plan(), http=sess)
    assert sess.get.call_count == 4  # NOT 6 — fresh state cached after retry
    assert sess.post.call_count == 6
