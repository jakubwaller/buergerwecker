from pathlib import Path
from unittest.mock import MagicMock
import pytest
import requests
from app.models import PollPlan, Slot
from app.scrapers import tevis, get_scraper
from app.catalog import load_catalog, booking_start_url

FIXTURE = (Path(__file__).parent / "fixtures"
           / "dresden_location_page.html").read_text(encoding="utf-8")


def _plan(locations="all", service="487"):
    return PollPlan(city="dresden", appointment_type=service,
                    locations=locations)


def test_parse_one_earliest_slot_per_office():
    slots = tevis.parse_slots(FIXTURE, service_id="487", locations="all")
    # The fixture (captured 2026-07-13) shows all 14 offices with a next slot.
    assert len(slots) == 14
    assert len({s.location_uuid for s in slots}) == 14


def test_parse_fields_and_token():
    slots = {s.location_uuid: s
             for s in tevis.parse_slots(FIXTURE, service_id="487",
                                        locations="all")}
    blasewitz = slots["61"]
    assert (blasewitz.date, blasewitz.time_str) == ("2026-07-14", "08:15")
    assert blasewitz.service_uuid == "487"
    assert blasewitz.booking_token == "2026-07-14T08:15@61"
    plauen = slots["67"]  # latest next-slot in the fixture: 20.07.2026, 09:00
    assert (plauen.date, plauen.time_str) == ("2026-07-20", "09:00")


def test_parse_respects_location_filter():
    slots = tevis.parse_slots(FIXTURE, service_id="487", locations=["61", "67"])
    assert sorted(s.location_uuid for s in slots) == ["61", "67"]


def test_parse_generic_page_yields_nothing():
    assert tevis.parse_slots("<html><body>Hilfe</body></html>",
                             service_id="487", locations="all") == []


def _http(pages):
    """MagicMock session whose GETs return the given page texts in order."""
    http = MagicMock()
    http.get.side_effect = [MagicMock(text=t, status_code=200) for t in pages]
    return http


def test_poll_bootstraps_session_then_fetches():
    http = _http(["<html>anliegen</html>", FIXTURE])
    slots = tevis.poll(_plan(), http=http)
    assert len(slots) == 14
    (first, _), (second, second_kw) = [c[:2] for c in http.get.call_args_list]
    assert first[0].endswith("/select2")
    assert second[0].endswith("/location")
    assert second_kw["params"]["cnc-487"] == "1"
    assert second_kw["params"]["mdt"] == "14"


def test_poll_reacquires_session_once_on_generic_page():
    generic = "<html>Session abgelaufen o.ä. — keine Standortliste</html>"
    http = _http(["anliegen", generic, "anliegen again", FIXTURE])
    slots = tevis.poll(_plan(), http=http)
    assert len(slots) == 14
    assert http.get.call_count == 4


def test_poll_session_reused_across_plans():
    http = _http(["anliegen", FIXTURE, FIXTURE])
    # Same session object: second plan must NOT re-visit select2. Attribute
    # tracking works on MagicMock like on a real requests.Session.
    http._tevis_ready = None
    tevis.poll(_plan(service="487"), http=http)
    tevis.poll(_plan(service="489"), http=http)
    calls = [c[0][0] for c in http.get.call_args_list]
    assert len([u for u in calls if u.endswith("/select2")]) == 1


def test_poll_rejects_wrong_vendor():
    with pytest.raises(RuntimeError, match="not configured for tevis"):
        tevis.poll(PollPlan(city="leipzig", appointment_type="x",
                            locations="all"), http=MagicMock())


def test_registry_and_booking_start_url():
    assert get_scraper("dresden") is tevis
    scfg = load_catalog("dresden").scraper_config
    assert (booking_start_url(scfg)
            == "https://termine-buergerbuero.dresden.de/select2?md=2")


def test_catalog_sync_probes_tevis_tenant_with_gets_only():
    """TEVIS tenants are drift-synced too (select2 + /location probes), but
    strictly read-only: no POSTs anywhere near the booking flow. An
    unparseable page is an error, never treated as 'city deleted everything'."""
    from app.catalog_sync import sync_city
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=200, text="<html></html>")
    result = sync_city("dresden", http, alert_fn=MagicMock())
    assert result["error"]
    http.get.assert_called()
    http.post.assert_not_called()


def test_catalog_locations_match_fixture():
    """The hand-maintained locations.json must agree with the captured page —
    every loc id the parser finds is labeled, and vice versa. The fixture
    predates Dresden's 2026-10-10 renumbering, so its ids go through the
    committed remap first."""
    import json
    remap = json.loads((Path(__file__).parents[1] / "scripts"
                        / "catalog_remap_2026-10-10.json").read_text())
    catalog = load_catalog("dresden")
    parsed = {remap["dresden"]["locations"][s.location_uuid]
              for s in tevis.parse_slots(FIXTURE, service_id="487",
                                         locations="all")}
    assert parsed == set(catalog.locations.values())


def _resp(status, text=""):
    r = requests.Response()
    r.status_code, r._content = status, text.encode()
    return r


def test_poll_error_page_is_a_failed_poll_not_zero_slots():
    http = MagicMock()
    http.get.side_effect = [_resp(200), _resp(503, FIXTURE)]
    with pytest.raises(requests.HTTPError):
        tevis.poll(_plan(), http=http)


def test_poll_without_office_list_after_relogin_raises_and_keeps_other_hosts_cookies():
    http = requests.Session()
    http.cookies.set("sid", "mine",
                     domain="termine-buergerbuero.dresden.de")
    http.cookies.set("sid", "other", domain="other.example.com")
    http.get = MagicMock(return_value=_resp(200, "<html>Hilfe</html>"))
    with pytest.raises(RuntimeError, match="no office list"):
        tevis.poll(_plan(), http=http)
    assert [c.domain for c in http.cookies] == ["other.example.com"]


def test_a_relogin_rebootstraps_every_tenant_on_its_host():
    """Tenants on one host share its cookies (path-scoped, or one at /). Once a
    re-login clears them, each sibling must bootstrap again, or it polls with
    no session and clears the host for the next one, every cycle."""
    plan = _plan()
    base = load_catalog(plan.city).scraper_config["base_url"].rstrip("/")
    http = requests.Session()
    http._tevis_ready = {(base, "sibling"), ("https://other.example.com", "1")}
    http.get = MagicMock(return_value=_resp(200, "<html>Hilfe</html>"))
    with pytest.raises(RuntimeError):
        tevis.poll(plan, http=http)
    assert (base, "sibling") not in http._tevis_ready
    assert ("https://other.example.com", "1") in http._tevis_ready


# --- read_times: the suggestion list (wizard step 4), muenster-kfz only -----

SUGGEST = (Path(__file__).parent / "fixtures"
           / "tevis_suggest_page.html").read_text(encoding="utf-8")

# The office card as the /location page renders it (hidden fields + "Weiter").
MUENSTER_LOCATION = """<html><body>
<form method="post"><input type="hidden" name="loc" value="243" />
<input type="hidden" name="gps_lat" value="51.9" />
<input type="hidden" name="gps_long" value="7.6" />
Name KFZ-Zulassungsstelle Nächster Termin ab 01.12.2026, 08:10 Uhr
<input type="submit" name="select_location" value="Weiter" /></form>
</body></html>"""


def _earliest(service="2407"):
    return Slot("2026-12-01", "08:10", "243", service, "2026-12-01T08:10@243")


def _kfz(service="2407"):
    return PollPlan(city="muenster-kfz", appointment_type=service, locations="all")


def test_parse_times_keeps_the_free_times_of_the_first_day_only():
    """Taken times are disabled buttons, not forms. 02.12 07:50 is free but on
    a later day: a new day key, i.e. a mail the earliest-slot reading never
    sent, so it is dropped."""
    slots = tevis.parse_times(SUGGEST, _earliest())
    assert [(s.date, s.time_str) for s in slots] == [("2026-12-01", "08:10"),
                                                     ("2026-12-01", "09:30")]
    assert {(s.location_uuid, s.service_uuid) for s in slots} == {("243", "2407")}
    assert slots[1].booking_token == "2026-12-01T09:30@243"


@pytest.mark.parametrize("earliest", [_earliest(service="2408"),
                                      Slot("2026-12-01", "08:10", "244", "2407", "t")])
def test_parse_times_refuses_a_list_for_another_service_or_office(earliest):
    assert tevis.parse_times(SUGGEST, earliest) == []


def test_only_muenster_kfz_reads_times():
    assert [c for c in sorted(p.parent.name for p in
                              (Path(__file__).parents[1] / "catalog")
                              .glob("*/scraper_config.json"))
            if load_catalog(c).scraper_config.get("read_times")] == ["muenster-kfz"]


def test_read_times_selects_the_office_reads_the_list_and_never_a_time():
    http = _http(["anliegen", MUENSTER_LOCATION, SUGGEST])
    slots = tevis.poll(_kfz(), http=http)
    assert [s.time_str for s in slots] == ["08:10", "09:30"]
    # The one POST is the office's "Weiter", to /location, nothing of a time.
    (args, kw), = [c[:2] for c in http.post.call_args_list]
    assert args[0].endswith("/location")
    assert kw["params"]["cnc-2407"] == "1"
    assert kw["data"] == {"loc": "243", "gps_lat": "51.9", "gps_long": "7.6",
                          "select_location": "Weiter"}
    assert kw["allow_redirects"] is False
    assert [c[0][0].rsplit("/", 1)[1] for c in http.get.call_args_list] == [
        "select2", "location", "suggest"]


@pytest.mark.parametrize("post, suggest", [
    (requests.Timeout("slow"), SUGGEST),         # the step times out
    (None, "<html>Sitzung abgelaufen</html>"),   # the list parses to nothing
    (None, SUGGEST.replace("cnc-2407", "cnc-2408")),  # another plan's list
])
def test_read_times_falls_back_to_the_earliest_slot(post, suggest):
    http = _http(["anliegen", MUENSTER_LOCATION, suggest])
    http.post.side_effect = post
    slots = tevis.poll(_kfz(), http=http)
    assert [(s.date, s.time_str, s.booking_token) for s in slots] == [
        ("2026-12-01", "08:10", "2026-12-01T08:10@243")]


def test_a_tenant_without_the_flag_makes_no_extra_request():
    http = _http(["anliegen", FIXTURE])
    tevis.poll(_plan(), http=http)
    assert http.get.call_count == 2
    http.post.assert_not_called()
