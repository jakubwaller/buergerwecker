import ast
import base64
import inspect
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from freezegun import freeze_time

from app.catalog import booking_start_url, load_catalog
from app.http_session import CountingSession
from app.models import PollPlan
from app.scrapers import get_scraper, jcc

CUSTOMER = "29d913b4-c346-43a3-8a6a-0191e3645806"
ANMELDUNG = "b3360d40-76af-4e46-9628-188e216edbbb"
ALTENESSEN = "8e7c9e0c-7a68-423e-8247-9efb35f314de"
GILDEHOF = "79ea260d-2b42-4465-9a81-57c7d045bb51"
STEELE = "117f1223-daad-49f4-9f29-a437a46c8995"
TOKEN = "00000000-0000-4000-8000-000000000001"  # synthetic, not the city's

# firstAvailableAppointmentTime for Anmeldung and all 7 offices, as the
# browser received it on 2026-11-23.
ANSWER = {"data": [
    {"locationId": ALTENESSEN, "firstAvailableTime": "2026-11-23T13:00:00"},
    {"locationId": "9646ae4d-443a-4913-9d4d-b2424c758b61", "firstAvailableTime": "2026-11-25T07:35:00"},
    {"locationId": GILDEHOF, "firstAvailableTime": "2026-11-23T10:45:00"},
    {"locationId": "10b7854b-0a25-4ca5-ab2b-3745ee682db5", "firstAvailableTime": "2026-11-23T10:30:00"},
    {"locationId": "f1c0c659-7897-466b-859a-d7c3173145f8", "firstAvailableTime": "2026-11-23T10:55:00"},
    {"locationId": STEELE, "firstAvailableTime": "2026-11-23T10:50:00"},
    {"locationId": "94a973fd-337b-4843-894c-e095cec86891", "firstAvailableTime": "2026-11-24T09:05:00"},
], "success": True}


def _encrypt(text: str) -> str:
    """CryptoJS AES.encrypt as the bundle's encryptUsingAES256 does it."""
    padder = padding.PKCS7(128).padder()
    data = padder.update(text.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(jcc._AES_KEY), modes.CBC(jcc._AES_IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


class _Portal(requests.adapters.BaseAdapter):
    """Canned answers by URL fragment; the last answer of a list repeats."""

    def __init__(self, earliest):
        super().__init__()
        self.sent = []
        self.routes = {"/version.json": ["639271567410000000"],
                       "/config-639271567410000000.json":
                           [{"flow-api-token": _encrypt(TOKEN)}],
                       "/firstAvailableAppointmentTime": list(earliest)}

    def send(self, request, **kwargs):
        self.sent.append(request)
        queue = next(q for k, q in self.routes.items() if k in request.url)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        r = requests.Response()
        r.status_code, r.url, r.request = status, request.url, request
        r._content = (body if isinstance(body, str) else json.dumps(body)).encode()
        return r

    def close(self):
        pass

    def urls(self, fragment):
        return [p for p in self.sent if fragment in p.url]


def _session(*earliest, token=None):
    http = CountingSession()
    portal = _Portal(earliest)
    http.mount("https://", portal)
    if token:
        http._jcc_tokens = {CUSTOMER: token}
    return http, portal


def _plan(locations="all", service=ANMELDUNG):
    return PollPlan(city="essen", appointment_type=service, locations=locations)


def _query(prepared):
    return parse_qs(urlsplit(prepared.url).query)


# a. token derivation
def test_a_config_value_decrypts_to_the_token():
    assert jcc.decrypt_token(_encrypt(TOKEN)) == TOKEN


def test_the_token_is_derived_once_from_the_public_config_and_cached():
    http, portal = _session(ANSWER)
    jcc.poll(_plan(), http)
    jcc.poll(_plan(), http)
    assert [p.url for p in portal.sent[:2]] == [
        f"https://termine.essen.de/customer_assets/{CUSTOMER}/version.json",
        f"https://termine.essen.de/customer_assets/{CUSTOMER}/config-639271567410000000.json"]
    assert len(portal.sent) == 4  # 2 for the token, then 1 per poll
    assert portal.sent[-1].headers["Authorization"] == f"Bearer {TOKEN}"


# b. one Slot per office
def test_b_seven_offices_give_seven_slots():
    http, _ = _session(ANSWER, token=TOKEN)
    slots = jcc.poll(_plan(), http)
    assert len(slots) == 7
    s = next(s for s in slots if s.location_uuid == ALTENESSEN)
    assert (s.date, s.time_str, s.service_uuid, s.booking_token) == (
        "2026-11-23", "13:00", ANMELDUNG, f"2026-11-23T13:00@{ALTENESSEN}")
    assert {s.service_uuid for s in slots} == {ANMELDUNG}
    assert {s.location_uuid for s in slots} == set(
        load_catalog("essen").locations.values())


# c. no time, no slot
def test_c_an_office_without_a_time_gives_no_slot():
    answer = {"success": True, "data": [
        {"locationId": ALTENESSEN, "firstAvailableTime": None},
        {"locationId": GILDEHOF, "firstAvailableTime": "2026-11-24T08:00:00"}]}
    http, _ = _session(answer, token=TOKEN)
    assert [s.location_uuid for s in jcc.poll(_plan(), http)] == [GILDEHOF]


def test_c_an_empty_list_gives_no_slots():
    http, _ = _session({"success": True, "data": []}, token=TOKEN)
    assert jcc.poll(_plan(), http) == []


# d. selected offices only
def test_d_a_plan_with_selected_offices_asks_for_those_only():
    http, portal = _session(ANSWER, token=TOKEN)
    slots = jcc.poll(_plan([GILDEHOF, STEELE]), http)
    assert sorted(_query(portal.sent[0])["locationNumber"]) == ["5", "8"]
    assert {s.location_uuid for s in slots} == {GILDEHOF, STEELE}


# e. request shape
def test_e_one_poll_with_a_cached_token_is_one_get():
    http, portal = _session(ANSWER, token=TOKEN)
    jcc.poll(_plan(), http)
    assert [(p.method, urlsplit(p.url).path) for p in portal.sent] == [
        ("GET", "/Afspraakgeleiding/Api/api/proxy/warp/appointment/"
                "firstAvailableAppointmentTime")]
    q = _query(portal.sent[0])
    assert q["activityId"] == [ANMELDUNG] and q["amount"] == ["1"]
    assert sorted(map(int, q["locationNumber"])) == [5, 6, 8, 9, 10, 30, 43]


def test_e_the_module_never_writes_or_reads_the_times_list():
    tree = ast.parse(inspect.getsource(jcc))
    for node in ast.walk(tree):  # docstrings describe what is never done
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                and ast.get_docstring(node) is not None):
            node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree).lower()
    for banned in ("availabletimelist", "filefield", ".post(", ".put(",
                   ".delete(", "/login"):
        assert banned not in code, banned


# f. headers
def test_f_the_page_headers_are_sent_and_the_user_agent_is_ours():
    http, portal = _session(ANSWER, token=TOKEN)
    jcc.poll(_plan(), http)
    h = portal.sent[0].headers
    assert h["Authorization"] == f"Bearer {TOKEN}"
    assert h["id"] == CUSTOMER
    assert h["warp-api-version"] == "1"
    assert h["language"] == "de"
    assert h["connection-source-id"] == "f59ddbf9-9158-42d0-ac39-f6065c003da4"
    assert h["User-Agent"] == "Buergerwecker/1.0 (+https://buergerwecker.de)"
    assert "Authorization" not in http.headers  # per request, not per session


# g. refusal
@pytest.mark.parametrize("status", [401, 403])
def test_g_a_refused_token_is_derived_again_and_retried_once(status):
    http, portal = _session((status, ""), ANSWER, token="stale")
    assert len(jcc.poll(_plan(), http)) == 7
    assert len(portal.urls("/config-")) == 1
    gets = portal.urls("/firstAvailableAppointmentTime")
    assert [g.headers["Authorization"] for g in gets] == [
        "Bearer stale", f"Bearer {TOKEN}"]


def test_g_a_second_refusal_raises():
    http, portal = _session((401, ""), token="stale")
    with pytest.raises(RuntimeError):
        jcc.poll(_plan(), http)
    assert len(portal.urls("/firstAvailableAppointmentTime")) == 2


# h. could not read is not "nothing free"
@pytest.mark.parametrize("answer", [
    {"success": False, "data": []},
    (500, {"success": False}),
    (400, "Not a valid request"),
    "<!DOCTYPE html><html>maintenance</html>",
    {"success": True},
    {"success": True, "data": [{"locationId": ALTENESSEN,
                                "firstAvailableTime": "morgen"}]},
])
def test_h_a_broken_answer_raises(answer):
    http, _ = _session(answer, token=TOKEN)
    with pytest.raises((RuntimeError, requests.HTTPError, ValueError)):
        jcc.poll(_plan(), http)


# i. horizon
@freeze_time("2026-11-23 23:30:00")  # UTC; already 24 November in Essen
def test_i_to_date_is_today_in_berlin_plus_the_horizon():
    http, portal = _session(ANSWER, token=TOKEN)
    jcc.poll(_plan(), http)
    assert _query(portal.sent[0])["toDate"] == ["2027-1-23"]


# j. local times stay as given
def test_j_a_late_time_keeps_its_date():
    answer = {"success": True, "data": [
        {"locationId": STEELE, "firstAvailableTime": "2026-11-23T23:30:00"}]}
    http, _ = _session(answer, token=TOKEN)
    [s] = jcc.poll(_plan(), http)
    assert (s.date, s.time_str) == ("2026-11-23", "23:30")


# k. tenant
def test_k_essen_tenant():
    assert get_scraper("essen") is jcc
    cat = load_catalog("essen")
    scfg = cat.scraper_config
    assert len(cat.appointment_types) == 19
    assert set(cat.appointment_types_en.values()) == set(
        cat.appointment_types.values())
    for service in cat.appointment_types.values():
        offices = cat.service_locations.get(service) or list(cat.locations.values())
        assert offices and set(offices) <= set(scfg["location_numbers"]), service
    for key in ("city_name", "label", "heading", "note"):
        assert cat.display[key]["de"] and cat.display[key]["en"], key
    assert cat.notify_granularity == "day"
    assert scfg["poll_interval_seconds"] == 180
    assert scfg["horizon_days"] == 60


# l. booking link
def test_l_booking_start_url_is_the_portal_start_page():
    scfg = load_catalog("essen").scraper_config
    assert booking_start_url(scfg) == "https://termine.essen.de/"
