from pathlib import Path
from unittest.mock import MagicMock
import pytest
from app.models import PollPlan
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
    every loc id the parser finds is labeled, and vice versa."""
    catalog = load_catalog("dresden")
    parsed = {s.location_uuid
              for s in tevis.parse_slots(FIXTURE, service_id="487",
                                         locations="all")}
    assert parsed == set(catalog.locations.values())
