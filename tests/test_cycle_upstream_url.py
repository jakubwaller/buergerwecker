import pytest

from app.cycle import _build_upstream_url
from app.models import Slot


def _slot(**kw) -> Slot:
    base = dict(date="2026-06-05", time_str="09:30", location_uuid="13",
                service_uuid="14", booking_token="13@2026-06-05T09:30:00")
    base.update(kw)
    return Slot(**base)


def test_dtms_upstream_url_points_at_portal_for_the_mandant():
    # DTMS booking happens inside the DrivePort SPA — there is no per-slot deep
    # link — so the /go redirect opens the portal entry for the city's Mandant.
    scfg = {"vendor": "dtms", "portal_url": "https://driveport.de/termine/",
            "mandant": 1}
    assert _build_upstream_url(scfg, _slot()) == "https://driveport.de/termine/?MA=1"


def test_smartcjm_upstream_url_still_uses_appointment_reserve():
    scfg = {"vendor": "smartcjm", "base_url": "https://x/cal", "uid": "u-1"}
    slot = _slot(booking_token="tok-99")
    assert _build_upstream_url(scfg, slot) == "https://x/cal/?uid=u-1&appointment_reserve=tok-99"


def test_unknown_vendor_raises():
    with pytest.raises(RuntimeError):
        _build_upstream_url({"vendor": "nope"}, _slot())
