from unittest.mock import MagicMock
import pytest
from app.models import PollPlan
from app.scrapers import smartcjm
from app.scrapers.smartcjm import poll

LEIPZIG_BASE = "https://terminvereinbarung.leipzig.de/m/leipzig-ba/extern/calendar"

SERVICES_PAGE_HTML = (
    '<html><body><form name="x_services" '
    'action="?uid=u&amp;wsid=w&amp;lang=de&amp;rev=test-rev#top">'
    '<input type="hidden" name="__RequestVerificationToken" value="test-csrf" />'
    '</form></body></html>'
)


@pytest.fixture(autouse=True)
def _clear_wsid_cache():
    smartcjm._WSID_CACHE.clear()
    yield
    smartcjm._WSID_CACHE.clear()


def _make_session(*, wsid_redirect_url: str, services_html: str, locations_html: str):
    """Mock requests.Session that handles both GETs (wsid acquire + services-page) and 2 POSTs."""
    sess = MagicMock()

    def _get(url, *a, **kw):
        if "search_result" in url:
            # wsid acquire — returns a response whose .url contains the wsid
            return MagicMock(url=wsid_redirect_url, text="", status_code=200, headers={})
        # services-page GET (for CSRF+rev)
        return MagicMock(url=url, text=SERVICES_PAGE_HTML, status_code=200, headers={})

    sess.get.side_effect = _get
    sess.post.side_effect = [
        MagicMock(text=services_html, status_code=200, url="", headers={}),
        MagicMock(text=locations_html, status_code=200, url="", headers={}),
    ]
    return sess


SVC_ANUM = "29cd0a26-fe7a-4d65-88cd-1e05fd749c71"  # An- oder Ummeldung Wohnsitz


def test_poll_returns_slots_from_locations_response():
    plan = PollPlan(city="leipzig", appointment_type=SVC_ANUM, locations="all")
    redirect = f"{LEIPZIG_BASE}/?wsid=fake-wsid&uid=b76cab25"
    # 4th arg is a RESOURCE uuid, NOT a service (per the upstream JS signature).
    locations_html = (
        '<ol data-testid="month_ol-1">'
        '<li data-testid="slot_button_li-1">'
        '<button onclick="return appointment_reserve(\'2026-06-10T10%3a30%3a00%2b02%3a00\','
        ' \'10\', \'loc-1\', \'res-9\');"></button>'
        '</li></ol>'
    )
    sess = _make_session(wsid_redirect_url=redirect,
                         services_html="",
                         locations_html=locations_html)
    slots = poll(plan, http=sess)
    assert len(slots) == 1
    assert slots[0].date == "2026-06-10"
    assert slots[0].time_str == "10:30"
    assert slots[0].location_uuid == "loc-1"
    # service_uuid is stamped from the plan (the server-side filtered search),
    # while the button's 4th arg is captured as the resource.
    assert slots[0].service_uuid == SVC_ANUM
    assert slots[0].resource_uuid == "res-9"
    assert slots[0].booking_token == "2026-06-10T10%3a30%3a00%2b02%3a00"


def test_post_services_posts_only_the_selected_service():
    """Posting all catalog services at once breaks server-side filtering and makes
    the search return the global 'earliest' slot regardless of the chosen service.
    The services step must submit exactly the one selected service, amount 1."""
    import re
    plan = PollPlan(city="leipzig", appointment_type=SVC_ANUM, locations="all")
    sess = _make_session(wsid_redirect_url=f"{LEIPZIG_BASE}/?wsid=w&uid=b",
                         services_html="", locations_html="<ol></ol>")
    poll(plan, http=sess)
    body = sess.post.call_args_list[0].kwargs["data"]
    submitted = re.findall(r"services=([0-9a-fA-F-]{36})", body)
    assert submitted == [SVC_ANUM]
    assert f"service_{SVC_ANUM}_amount=1" in body
    # exactly one service is given a quantity — never the old "blast every service" body
    assert body.count("_amount=1") == 1


def test_poll_stamps_each_plans_service_when_sharing_a_cached_session():
    """Two plans for different services in the same session must each stamp their
    own service onto their slots — not leak the first plan's service via the cache."""
    plan_a = PollPlan(city="leipzig", appointment_type=SVC_ANUM, locations="all")
    plan_b = PollPlan(city="leipzig",
                      appointment_type="b04658d5-8d85-469a-a635-93337e055b73",
                      locations="all")
    one_slot = (
        '<ol data-testid="month_ol-1"><li data-testid="slot_button_li-1">'
        '<button onclick="return appointment_reserve('
        "'2026-06-17T10%3a10%3a00%2b02%3a00', '10', 'loc-x', 'res-shared');\">"
        '</button></li></ol>'
    )
    sess = MagicMock()

    def _get(url, *a, **kw):
        if "search_result" in url:
            return MagicMock(url=f"{LEIPZIG_BASE}/?wsid=w&uid=b", text="",
                             status_code=200, headers={})
        return MagicMock(url=url, text=SERVICES_PAGE_HTML, status_code=200, headers={})

    sess.get.side_effect = _get
    sess.post.side_effect = [MagicMock(text=h, status_code=200, url="", headers={})
                             for h in ["", one_slot, "", one_slot]]
    slots_a = poll(plan_a, http=sess)
    slots_b = poll(plan_b, http=sess)
    assert [s.service_uuid for s in slots_a] == [SVC_ANUM]
    assert [s.service_uuid for s in slots_b] == ["b04658d5-8d85-469a-a635-93337e055b73"]


def test_returned_slot_matches_subscriber_despite_resource_in_button():
    """Regression: a slot whose button carries a *resource* uuid (not the service)
    must still match a subscriber who wants that service. Before the fix, the
    resource was stored as service_uuid and matches() rejected every slot."""
    from datetime import time
    from app.models import Filter
    from app.filters import matches
    plan = PollPlan(city="leipzig", appointment_type=SVC_ANUM, locations="all")
    locations_html = (
        '<ol data-testid="month_ol-1">'
        '<li data-testid="slot_button_li-1">'
        '<button onclick="return appointment_reserve('
        "'2026-06-17T10%3a10%3a00%2b02%3a00', '10', "
        "'868036ae-d404-4804-a6b3-3ccccac44071', "          # location
        "'6ce5bc5f-20ee-4df3-a139-d4d017468dec');\">"        # RESOURCE, not service
        '</button></li></ol>'
    )
    sess = _make_session(wsid_redirect_url=f"{LEIPZIG_BASE}/?wsid=w&uid=b",
                         services_html="", locations_html=locations_html)
    slots = poll(plan, http=sess)
    assert len(slots) == 1
    f = Filter(appointment_types=[SVC_ANUM], locations="all",
               weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    assert matches(f, slots[0]) is True


def test_poll_sends_csrf_token_in_post_body():
    """The __RequestVerificationToken extracted from the services page must appear in POST bodies."""
    plan = PollPlan(city="leipzig", appointment_type="x", locations="all")
    sess = _make_session(wsid_redirect_url=f"{LEIPZIG_BASE}/?wsid=w&uid=b",
                         services_html="", locations_html="")
    poll(plan, http=sess)
    for call in sess.post.call_args_list:
        body = call.kwargs.get("data") or (call.args[1] if len(call.args) > 1 else "")
        assert "__RequestVerificationToken=test-csrf" in body


def test_poll_uses_dynamic_rev_in_post_url():
    """The rev= query param in POST URLs must come from the services-page form action, not be hardcoded."""
    plan = PollPlan(city="leipzig", appointment_type="x", locations="all")
    sess = _make_session(wsid_redirect_url=f"{LEIPZIG_BASE}/?wsid=w&uid=b",
                         services_html="", locations_html="")
    poll(plan, http=sess)
    for call in sess.post.call_args_list:
        url = call.args[0]
        assert "rev=test-rev" in url
        assert "rev=HL0Ur" not in url


def test_poll_follows_8443_redirect_on_post():
    """If the POST returns 302 to a :8443 URL, the scraper rewrites it and follows."""
    plan = PollPlan(city="leipzig", appointment_type="x", locations="all")
    sess = MagicMock()
    followed_urls: list[str] = []

    def _get(url, *a, **kw):
        followed_urls.append(url)
        if "search_result" in url:
            return MagicMock(url=f"{LEIPZIG_BASE}/?wsid=w&uid=b",
                             text="", status_code=200, headers={})
        return MagicMock(url=url, text=SERVICES_PAGE_HTML, status_code=200, headers={})

    locations_html = '<ol></ol>'  # no slots, but parseable
    sess.get.side_effect = _get
    sess.post.side_effect = [
        # services POST → 302 to :8443
        MagicMock(status_code=302, text="", url="",
                  headers={"Location": f"{LEIPZIG_BASE.replace('https://', 'https://').replace('terminvereinbarung.leipzig.de', 'terminvereinbarung.leipzig.de:8443')}/post-services-redirect"}),
        # locations POST → 302 to :8443 with locations_html as the followed-target content
        MagicMock(status_code=302, text="", url="",
                  headers={"Location": f"{LEIPZIG_BASE.replace('terminvereinbarung.leipzig.de', 'terminvereinbarung.leipzig.de:8443')}/post-locations-redirect"}),
    ]
    poll(plan, http=sess)
    # Any URL we GET as a redirect-follow should NOT contain :8443
    redirected_follows = [u for u in followed_urls if "post-" in u]
    assert redirected_follows, "expected at least one redirect-follow GET"
    for u in redirected_follows:
        assert ":8443/" not in u, f"unrewritten :8443 in {u}"


def test_poll_session_expired_returns_empty_after_retry():
    """Two 'Session abgelaufen' responses → returns [] without further looping."""
    plan = PollPlan(city="leipzig", appointment_type="x", locations="all")
    redirect = f"{LEIPZIG_BASE}/?wsid=fake&uid=b76cab25"
    sess = MagicMock()

    def _get(url, *a, **kw):
        if "search_result" in url:
            return MagicMock(url=redirect, text="", status_code=200, headers={})
        return MagicMock(url=url, text=SERVICES_PAGE_HTML, status_code=200, headers={})

    sess.get.side_effect = _get
    sess.post.side_effect = [
        MagicMock(text="", status_code=200, url="", headers={}),
        MagicMock(text="Session abgelaufen", status_code=200, url="", headers={}),
        MagicMock(text="", status_code=200, url="", headers={}),
        MagicMock(text="Session abgelaufen", status_code=200, url="", headers={}),
    ]
    assert poll(plan, http=sess) == []
