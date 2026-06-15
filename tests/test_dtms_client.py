import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.models import PollPlan
from app.scrapers.dtms import poll

FIXTURES = Path(__file__).parent / "fixtures"
SVC = "14"  # Personalausweis beantragen


def _payload() -> dict:
    return json.loads(
        (FIXTURES / "hamburg_erste_pro_standort.json").read_text(encoding="utf-8")
    )


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def test_poll_posts_single_service_and_parses_overview():
    plan = PollPlan(city="hamburg", appointment_type=SVC, locations="all")
    sess = MagicMock()
    sess.post.return_value = _resp(_payload())

    slots = poll(plan, http=sess)

    # parsed the overview
    assert len(slots) == 4
    assert {s.location_uuid for s in slots} == {"13", "7", "27", "16"}
    assert all(s.service_uuid == SVC for s in slots)

    # request shape mirrors the captured ersteProStandort call
    assert sess.post.call_count == 1
    call = sess.post.call_args
    url = call.args[0] if call.args else call.kwargs["url"]
    assert url.endswith("/mandanten/1/terminvorschlaege/ersteProStandort")
    body = call.kwargs["json"]
    assert body["Dienstleistungen"] == [{"DienstleistungID": 14, "Anzahl": 1}]
    assert body["StandorteID"] is None
    assert body["Terminsperre"] is True
    assert "VonTag" in body and "BisTag" in body
    params = call.kwargs["params"]
    assert params["appKey"]
    assert params["lang"] == "de"


def test_poll_filters_to_requested_locations_but_still_posts_all():
    plan = PollPlan(city="hamburg", appointment_type=SVC, locations=["27"])
    sess = MagicMock()
    sess.post.return_value = _resp(_payload())

    slots = poll(plan, http=sess)

    assert [s.location_uuid for s in slots] == ["27"]
    # Always POST StandorteID=null (the captured request shape) and filter here.
    assert sess.post.call_args.kwargs["json"]["StandorteID"] is None


def test_poll_http_error_returns_empty():
    plan = PollPlan(city="hamburg", appointment_type=SVC, locations="all")
    sess = MagicMock()
    sess.post.return_value = _resp({}, status=500)
    assert poll(plan, http=sess) == []


def test_poll_rejects_non_dtms_city():
    plan = PollPlan(city="leipzig", appointment_type="x", locations="all")
    with pytest.raises(RuntimeError):
        poll(plan, http=MagicMock())
