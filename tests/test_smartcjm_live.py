"""Opt-in canary tests against the live Leipzig Smart-CJM site.

These hit real terminvereinbarung.leipzig.de endpoints. They exist to catch
upstream contract changes (CSRF, rev, form structure, JSON keys) that the
mocked unit tests cannot see by construction.

Run on demand:

    LIVE_TESTS=1 pytest -m live -q

Default `pytest` runs skip them.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import pytest
import requests
from app.models import PollPlan, Filter
from app.scrapers import smartcjm
from app import catalog_sync


REPO_ROOT = Path(__file__).resolve().parent.parent
LEIPZIG_DIR = REPO_ROOT / "catalog" / "leipzig"


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("LIVE_TESTS") != "1",
        reason="LIVE_TESTS!=1; opt-in only — these hit real Leipzig endpoints",
    ),
]


@pytest.fixture(autouse=True)
def _clear_wsid_cache():
    smartcjm._WSID_CACHE.clear()
    yield
    smartcjm._WSID_CACHE.clear()


@pytest.fixture
def scfg():
    return json.loads((LEIPZIG_DIR / "scraper_config.json").read_text())


@pytest.fixture
def appointment_types():
    return json.loads((LEIPZIG_DIR / "appointment_type.json").read_text())


@pytest.fixture
def http():
    s = requests.Session()
    s.headers["User-Agent"] = "termine-notifier/live-test (jakubwaller.eu)"
    return s


def test_live_poll_completes_without_raising(http, scfg, appointment_types):
    """Smoke: a real poll for a common service returns a list (possibly empty) without raising."""
    target_name = "An- oder Ummeldung Wohnsitz"
    assert target_name in appointment_types, \
        f"catalog drift — {target_name!r} missing; run catalog sync"
    target_uid = appointment_types[target_name]
    plan = PollPlan(city="leipzig", appointment_type=target_uid, locations="all")
    slots = smartcjm.poll(plan, http=http)
    assert isinstance(slots, list)
    # Canary for the resource-as-service regression: any returned slot must carry
    # the service we searched for, not the button's resource uuid. This single
    # assertion would have caught the original production outage on any live run
    # that returned at least one slot.
    if slots:
        assert all(s.service_uuid == target_uid for s in slots)


def test_live_get_service_list_endpoint_returns_known_services(http, scfg, appointment_types):
    """Canary: the JSON endpoint still exists and returns the catalog's UUIDs."""
    live = catalog_sync.fetch_services(http, scfg["base_url"], scfg["uid"])
    catalog_uids = set(appointment_types.values())
    live_uids = set(live.values())
    intersection = catalog_uids & live_uids
    assert intersection, \
        f"NO overlap between catalog and live services — catalog is fully stale.\n" \
        f"catalog={catalog_uids}\nlive={live_uids}"
    # Soft warning if drift exists (added/removed)
    added = live_uids - catalog_uids
    removed = catalog_uids - live_uids
    if added or removed:
        pytest.skip(f"catalog drift detected; run sync. added={added}, removed={removed}")


def test_live_locations_probe_returns_known_uids(http, scfg, appointment_types):
    """Canary: probing an appointment type returns location UUIDs that overlap the catalog."""
    target_uid = appointment_types["An- oder Ummeldung Wohnsitz"]
    locs = catalog_sync._probe_one_service(
        http, scfg["base_url"], scfg["uid"],
        target_uid=target_uid,
        all_service_uids=list(appointment_types.values()),
        steps=scfg["steps"],
    )
    catalog_locations = json.loads((LEIPZIG_DIR / "locations.json").read_text())
    catalog_loc_uids = set(catalog_locations.values())
    live_loc_uids = set(locs.keys())
    intersection = catalog_loc_uids & live_loc_uids
    assert intersection, \
        f"NO overlap between catalog locations and live probe — flow may have broken.\n" \
        f"catalog={catalog_loc_uids}\nlive={live_loc_uids}"
