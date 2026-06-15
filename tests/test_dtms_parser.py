import json
from pathlib import Path

from app.scrapers.dtms import parse_slots

FIXTURES = Path(__file__).parent / "fixtures"
SVC = "14"  # Personalausweis beantragen (Mandant 1, DienstleistungID 14)


def _payload() -> dict:
    return json.loads(
        (FIXTURES / "hamburg_erste_pro_standort.json").read_text(encoding="utf-8")
    )


def test_parse_stamps_service_from_arg_and_splits_datetime():
    # DTMS returns {StandortID, Von, Prio} per Terminvorschlag; the service is
    # stamped from the plan (we POST a single Dienstleistung), not the response.
    slots = parse_slots(_payload(), service_uuid=SVC, locations="all")
    assert len(slots) == 4
    s = next(x for x in slots if x.location_uuid == "13")
    assert s.date == "2026-06-05"
    assert s.time_str == "09:30"
    assert s.service_uuid == SVC
    assert s.resource_uuid == ""  # DTMS has no per-counter resource
    assert all(x.service_uuid == SVC for x in slots)


def test_parse_filters_to_requested_locations():
    slots = parse_slots(_payload(), service_uuid=SVC, locations=["27", "16"])
    assert sorted(x.location_uuid for x in slots) == ["16", "27"]


def test_parse_empty_returns_no_slots():
    assert (
        parse_slots(
            {"Terminvorschlaege": [], "Hinweise": None},
            service_uuid=SVC,
            locations="all",
        )
        == []
    )


def test_parse_accepts_raw_json_string():
    raw = (FIXTURES / "hamburg_erste_pro_standort.json").read_text(encoding="utf-8")
    assert len(parse_slots(raw, service_uuid=SVC, locations="all")) == 4


def test_parse_booking_token_is_unique_per_location():
    # No per-slot reservation token exists upstream; the token we synthesize must
    # still be unique per slot so the slots_cache key never collides.
    slots = parse_slots(_payload(), service_uuid=SVC, locations="all")
    tokens = [s.booking_token for s in slots]
    assert len(set(tokens)) == len(tokens)
