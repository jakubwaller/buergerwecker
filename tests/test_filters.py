from datetime import time, date
from app.models import Filter, Slot
from app.filters import matches

def make_slot(date_str="2026-06-10", time_str="10:30", loc="loc-1", svc="svc-A"):
    return Slot(date=date_str, time_str=time_str, location_uuid=loc,
                service_uuid=svc, booking_token="t")

def make_filter(types=("svc-A",), locations=("loc-1",), weekdays=(1,2,3,4,5),
                start=time(0,0), end=time(23,59)):
    return Filter(
        appointment_types=list(types),
        locations=list(locations),
        weekdays=list(weekdays),
        time_window_start=start,
        time_window_end=end,
    )

def test_match_basic():
    assert matches(make_filter(), make_slot()) is True

def test_no_match_wrong_service():
    assert matches(make_filter(types=("svc-A",)), make_slot(svc="svc-B")) is False

def test_no_match_wrong_location():
    assert matches(make_filter(locations=("loc-1",)), make_slot(loc="loc-2")) is False

def test_match_locations_all():
    f = make_filter(locations=())
    f = Filter(
        appointment_types=["svc-A"],
        locations="all",
        weekdays=[1,2,3,4,5,6,7],
        time_window_start=time(0,0),
        time_window_end=time(23,59),
    )
    assert matches(f, make_slot(loc="loc-anywhere")) is True

def test_no_match_wrong_weekday():
    # 2026-06-13 is a Saturday (ISO weekday 6)
    f = make_filter(weekdays=(1,2,3,4,5))
    assert matches(f, make_slot(date_str="2026-06-13")) is False

def test_match_time_window():
    f = make_filter(start=time(9,0), end=time(17,0))
    assert matches(f, make_slot(time_str="09:00")) is True
    assert matches(f, make_slot(time_str="17:00")) is True
    assert matches(f, make_slot(time_str="08:59")) is False
    assert matches(f, make_slot(time_str="17:01")) is False

def test_invalid_date_string_does_not_match():
    f = make_filter()
    assert matches(f, make_slot(date_str="not-a-date")) is False
