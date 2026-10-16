from unittest.mock import MagicMock
import pytest
from app.models import PollPlan
from app.scrapers import konsentas, get_scraper
from app.catalog import load_catalog, booking_start_url

BASE = "https://stuttgart.konsentas.de"

# A getTimeslot `data`: the booked-out day placeholder is captured verbatim
# (2026-10-16); the free rows follow the shape the browser reads in
# brick ota_termin _createEvent (yeardate, time in minutes, places/places_used).
TIMESLOTS = {"termins": [
    {"recno": 20261016, "places": 0, "duration": 0, "places_used": 0,
     "yeardate": 20261016, "time": "", "day_ids": ["12::20261016"],
     "name": None, "order": None, "debug_info": None,
     "resource_group": None, "className": "termin-booked-out"},
    {"recno": 501, "yeardate": 20261020, "time": 450, "duration": 20,
     "places": 1, "places_used": 0},
    # a second staff row at the same minute: one slot, as the browser merges it
    {"recno": 502, "yeardate": 20261020, "time": 450, "duration": 20,
     "places": 1, "places_used": 0},
    {"recno": 503, "yeardate": 20261020, "time": "485", "duration": 20,
     "places": 2, "places_used": 1},
    # taken
    {"recno": 504, "yeardate": 20261021, "time": 600, "duration": 20,
     "places": 1, "places_used": 1},
]}


@pytest.fixture(autouse=True)
def _no_cached_signup():
    konsentas._SESSIONS.clear()
    yield
    konsentas._SESSIONS.clear()


def _resp(data, status=200, code=3):
    return MagicMock(status_code=status,
                     json=MagicMock(return_value={"code": code, "msg": "",
                                                  "data": data}))


def _startup():
    return _resp({"ota_jwt": "jwt-1", "op_id": "id2",
                  "op_type": "ota_processes"})


def _at(op_type, op_id):
    return _resp({"op_id": op_id, "op_type": op_type})


def _plan(service="9_26"):
    return PollPlan(city="stuttgart-fuehrerschein", appointment_type=service,
                    locations="all")


def test_parse_keeps_free_slots_only_and_merges_staff_rows():
    slots = konsentas.parse_slots(TIMESLOTS, service_id="9_26",
                                  location_id="loewentorbogen-11")
    assert [(s.date, s.time_str) for s in slots] == [
        ("2026-10-20", "07:30"), ("2026-10-20", "08:05")]
    assert {s.service_uuid for s in slots} == {"9_26"}
    assert {s.location_uuid for s in slots} == {"loewentorbogen-11"}


def test_parse_fully_booked_answer_yields_nothing():
    assert konsentas.parse_slots(
        {"termins": TIMESLOTS["termins"][:1]}, service_id="9_26",
        location_id="x") == []
    assert konsentas.parse_slots({}, service_id="9_26", location_id="x") == []


def test_poll_walks_one_signup_to_the_slot_step():
    http = MagicMock()
    http.get.side_effect = [_startup(), _resp(TIMESLOTS)]
    http.post.side_effect = [_at("ota_termin", "id127")]
    slots = konsentas.poll(_plan(), http=http)
    assert len(slots) == 2
    startup, timeslots = http.get.call_args_list
    assert startup.args[0] == f"{BASE}/api/getOtaStartUp/"
    assert startup.kwargs["params"]["signupform_id"] == 3
    assert timeslots.args[0] == f"{BASE}/api/brick_ota_termin_getTimeslot/"
    assert timeslots.kwargs["headers"]["Authorization"] == "Bearer jwt-1"
    (step,) = http.post.call_args_list
    assert step.args[0] == f"{BASE}/api/postOtaNextStep/"
    assert step.kwargs["files"]["formdata[processes][0]"] == (None, "9_26")
    assert step.kwargs["files"]["op_id"] == (None, "id2")
    # the shared poller session never carries one tenant's token
    assert not http.headers.__setitem__.called


def test_next_service_reuses_the_signup_instead_of_opening_another():
    http = MagicMock()
    http.get.side_effect = [_startup(), _resp(TIMESLOTS), _resp(TIMESLOTS)]
    http.post.side_effect = [_at("ota_termin", "id127"),
                             _at("ota_processes", "id2"),
                             _at("ota_termin", "id127")]
    konsentas.poll(_plan("9_26"), http=http)
    konsentas.poll(_plan("9_362"), http=http)
    gets = [c.args[0] for c in http.get.call_args_list]
    assert gets.count(f"{BASE}/api/getOtaStartUp/") == 1
    back, forward = http.post.call_args_list[1:]
    assert back.args[0] == f"{BASE}/api/postOtaPreStep/"
    assert back.kwargs["files"]["op_id"] == (None, "id127")
    assert forward.kwargs["files"]["formdata[processes][0]"] == (None, "9_362")


def test_signup_is_renewed_before_the_token_runs_out(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(konsentas, "_now", lambda: clock[0])
    http = MagicMock()
    http.get.side_effect = [_startup(), _resp(TIMESLOTS),
                            _startup(), _resp(TIMESLOTS)]
    http.post.side_effect = [_at("ota_termin", "id127"),
                             _at("ota_termin", "id127")]
    konsentas.poll(_plan(), http=http)
    clock[0] += konsentas._TOKEN_TTL_SECONDS
    konsentas.poll(_plan(), http=http)
    gets = [c.args[0] for c in http.get.call_args_list]
    assert gets.count(f"{BASE}/api/getOtaStartUp/") == 2
    # an expired signup is left where it is, not walked back first
    assert all(c.args[0].endswith("/postOtaNextStep/")
               for c in http.post.call_args_list)


def test_a_service_that_skips_the_slot_step_is_never_pushed_further():
    """A service leading to a review step must not be stepped on: the step
    after review submits. The signup is dropped and nothing is retried."""
    http = MagicMock()
    http.get.side_effect = [_startup()]
    http.post.side_effect = [_at("ota_review", "id119")]
    with pytest.raises(RuntimeError, match="ota_review"):
        konsentas.poll(_plan(), http=http)
    assert http.post.call_count == 1
    assert konsentas._SESSIONS == {}


def test_a_stale_signup_is_replaced_once():
    http = MagicMock()
    http.get.side_effect = [_startup(), _resp(TIMESLOTS),
                            _startup(), _resp(TIMESLOTS)]
    http.post.side_effect = [_at("ota_termin", "id127"),
                             _resp({}, status=401, code=0),  # PreStep refused
                             _at("ota_termin", "id127")]
    konsentas.poll(_plan(), http=http)
    assert len(konsentas.poll(_plan("9_362"), http=http)) == 2
    gets = [c.args[0] for c in http.get.call_args_list]
    assert gets.count(f"{BASE}/api/getOtaStartUp/") == 2


def test_stuttgart_tenant_is_wired_to_konsentas():
    assert get_scraper("stuttgart-fuehrerschein") is konsentas
    scfg = load_catalog("stuttgart-fuehrerschein").scraper_config
    assert booking_start_url(scfg) == f"{BASE}/form/3/?signup_new=1"
