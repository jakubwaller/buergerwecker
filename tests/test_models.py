from datetime import datetime, time
from app.models import Subscription, Slot, PollPlan, Filter

def test_filter_from_json():
    f = Filter.from_json('{"appointment_types": ["uuid-a"], "locations": "all", '
                         '"weekdays": [1,2,3,4,5], "time_window": {"start":"08:00","end":"18:00"}}')
    assert f.appointment_types == ["uuid-a"]
    assert f.locations == "all"
    assert f.weekdays == [1, 2, 3, 4, 5]
    assert f.time_window_start == time(8, 0)
    assert f.time_window_end == time(18, 0)

def test_filter_to_json_roundtrip():
    f = Filter(
        appointment_types=["a", "b"],
        locations=["loc-1"],
        weekdays=[1, 7],
        time_window_start=time(9, 0),
        time_window_end=time(17, 30),
    )
    s = f.to_json()
    f2 = Filter.from_json(s)
    assert f2.appointment_types == f.appointment_types
    assert f2.locations == f.locations
    assert f2.weekdays == f.weekdays
    assert f2.time_window_start == f.time_window_start
    assert f2.time_window_end == f.time_window_end

def test_slot_hash_is_deterministic():
    s1 = Slot(
        date="2026-06-10", time_str="10:30",
        location_uuid="loc-1", service_uuid="svc-1",
        booking_token="abc",
    )
    s2 = Slot(
        date="2026-06-10", time_str="10:30",
        location_uuid="loc-1", service_uuid="svc-1",
        booking_token="def",  # different token, same logical slot
    )
    assert s1.hash() == s2.hash()
    s3 = Slot(date="2026-06-11", time_str="10:30",
              location_uuid="loc-1", service_uuid="svc-1",
              booking_token="abc")
    assert s1.hash() != s3.hash()

def test_slot_resource_uuid_is_captured_and_excluded_from_hash():
    # The booking button's 4th arg is a RESOURCE (a counter/staff resource),
    # NOT the appointment service. `service_uuid` is the appointment type the
    # slot was searched for; `resource_uuid` is what the button carries.
    a = Slot(date="2026-06-10", time_str="10:30", location_uuid="loc-1",
             service_uuid="svc-A", booking_token="t1", resource_uuid="res-1")
    b = Slot(date="2026-06-10", time_str="10:30", location_uuid="loc-1",
             service_uuid="svc-A", booking_token="t2", resource_uuid="res-2")
    assert a.resource_uuid == "res-1"
    # Same logical slot (date/time/location/service) at two different resources
    # is one notification for the subscriber — resource must not affect the hash.
    assert a.hash() == b.hash()
    # A different service IS a different slot.
    c = Slot(date="2026-06-10", time_str="10:30", location_uuid="loc-1",
             service_uuid="svc-B", booking_token="t1", resource_uuid="res-1")
    assert a.hash() != c.hash()
