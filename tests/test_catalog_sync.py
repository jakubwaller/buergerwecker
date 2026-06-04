from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest
import requests

from app import catalog_sync


LEIPZIG_BASE = "https://terminvereinbarung.leipzig.de/m/leipzig-ba/extern/calendar"
LEIPZIG_UID = "b76cab25-49bd-44e3-950d-aab715881ea7"
STEPS = "serviceslocationssearch_resultsbookingfinish"


# ---------- fetch_services ----------

def test_fetch_services_parses_response_into_name_uid_dict():
    http = MagicMock()
    http.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "success": True,
            "results": [
                {"uid": "u1", "display_name": "Personalausweis beantragen"},
                {"uid": "u2", "display_name": "Reisepass beantragen"},
            ],
        },
    )
    de, en = catalog_sync.fetch_services(http, LEIPZIG_BASE, LEIPZIG_UID)
    assert de == {
        "Personalausweis beantragen": "u1",
        "Reisepass beantragen": "u2",
    }
    # No data.display_name_en in the response → English falls back to German.
    assert en == de


def test_fetch_services_extracts_english_from_data_display_name_en():
    """English service labels live in result.data.display_name_en (the lang
    query param is ignored by get_service_list); missing → fall back to German."""
    http = MagicMock()
    http.get.return_value = MagicMock(status_code=200, json=lambda: {
        "success": True, "results": [
            {"uid": "u1", "display_name": "Personalausweis beantragen",
             "data": {"display_name_en": "Applying for an identity card"}},
            {"uid": "u2", "display_name": "Reisepass beantragen", "data": {}},
        ]})
    de, en = catalog_sync.fetch_services(http, LEIPZIG_BASE, LEIPZIG_UID)
    assert de == {"Personalausweis beantragen": "u1", "Reisepass beantragen": "u2"}
    assert en == {"Applying for an identity card": "u1", "Reisepass beantragen": "u2"}


def test_fetch_services_strips_trailing_whitespace_from_names():
    """Live API returns 'An- oder Ummeldung Wohnsitz ' with trailing space."""
    http = MagicMock()
    http.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"success": True, "results": [
            {"uid": "u1", "display_name": "An- oder Ummeldung Wohnsitz "},
        ]},
    )
    de, _en = catalog_sync.fetch_services(http, LEIPZIG_BASE, LEIPZIG_UID)
    assert "An- oder Ummeldung Wohnsitz" in de  # no trailing space


def test_fetch_services_raises_on_success_false():
    http = MagicMock()
    http.get.return_value = MagicMock(
        status_code=200,
        json=lambda: {"success": False, "results": []},
    )
    with pytest.raises(RuntimeError):
        catalog_sync.fetch_services(http, LEIPZIG_BASE, LEIPZIG_UID)


# ---------- fetch_locations ----------

def _services_page_html(csrf: str = "csrf-xyz", rev: str = "rev-abc") -> str:
    return (
        f'<html><body><form name="x_services" '
        f'action="?uid=u&amp;wsid=w&amp;lang=de&amp;rev={rev}#top">'
        f'<input type="hidden" name="__RequestVerificationToken" value="{csrf}" />'
        f'</form></body></html>'
    )


def _locations_page_html(locations: dict) -> str:
    """Build HTML where the locations-step form has a checkbox per (uid, name)."""
    parts = ['<html><body><form>']
    for uid, name in locations.items():
        cb_id = f"location_{uid}"
        parts.append(f'<input type="checkbox" name="locations" '
                     f'value="{uid}" id="{cb_id}" />')
        parts.append(f'<label for="{cb_id}">\n\t\t{name}\n\t\tSome Street 1</label>')
    parts.append('</form></body></html>')
    return ''.join(parts)


def _build_probe_http(services_page: str, locations_pages_by_target_uid: dict):
    """Mock http that simulates wsid GET, services-page GET, services-step POST, follow.
    locations_pages_by_target_uid: maps target_service_uid -> dict[loc_uid, loc_name]."""
    http = MagicMock()

    def _get(url, *a, **kw):
        if "search_result" in url:
            return MagicMock(status_code=200,
                             url=f"{LEIPZIG_BASE}/?wsid=fake-wsid&uid={LEIPZIG_UID}",
                             text="", headers={})
        return MagicMock(status_code=200, text=services_page, headers={}, url=url)

    posted_targets: list[str] = []

    def _post(url, data=None, *a, **kw):
        # Identify which service was POSTed with amount=1
        import re as _re
        m = _re.search(r'service_([0-9a-zA-Z-]+)_amount=1', data or "")
        target = m.group(1) if m else "unknown"
        posted_targets.append(target)
        loc_html = _locations_page_html(
            locations_pages_by_target_uid.get(target, {})
        )
        return MagicMock(status_code=200, text=loc_html, headers={}, url=url)

    http.get.side_effect = _get
    http.post.side_effect = _post
    http._posted_targets = posted_targets  # for test introspection
    return http


def test_fetch_locations_returns_union_across_services():
    """Two services with overlapping but distinct location sets → union by uid."""
    svc_a = "aaaa-aaaa"
    svc_b = "bbbb-bbbb"
    http = _build_probe_http(
        services_page=_services_page_html(),
        locations_pages_by_target_uid={
            svc_a: {"loc-1": "Bürgerbüro Eins", "loc-2": "Bürgerbüro Zwei"},
            svc_b: {"loc-2": "Bürgerbüro Zwei", "loc-3": "Bürgerbüro Drei"},
        },
    )
    out = catalog_sync.fetch_locations(http, LEIPZIG_BASE, LEIPZIG_UID,
                                       service_uids=[svc_a, svc_b], steps=STEPS)
    assert out == {
        "Bürgerbüro Drei": "loc-3",
        "Bürgerbüro Eins": "loc-1",
        "Bürgerbüro Zwei": "loc-2",
    }


def test_fetch_locations_passes_lang_to_the_wizard():
    """English location labels come from the wizard rendered with lang=en, so
    fetch_locations must thread the requested language into its GET/POST URLs."""
    http = _build_probe_http(
        services_page=_services_page_html(),
        locations_pages_by_target_uid={"svc": {"loc-1": "Resident Services Office X"}},
    )
    catalog_sync.fetch_locations(http, LEIPZIG_BASE, LEIPZIG_UID,
                                 service_uids=["svc"], steps=STEPS, lang="en")
    urls = ([c.args[0] for c in http.get.call_args_list]
            + [c.args[0] for c in http.post.call_args_list])
    assert any("lang=en" in u for u in urls), "wizard requests must carry lang=en"
    assert not any("lang=de" in u for u in urls)


def test_parse_location_checkboxes_collapses_internal_whitespace():
    """The English wizard emits labels like 'Resident Services Office  Leutzsch'
    with a double space; collapse runs of whitespace to a single space."""
    html = ('<form>'
            '<input type="checkbox" name="locations" value="loc-1" id="l1"/>'
            '<label for="l1">\n\t\tResident Services Office  Leutzsch\n\t\tStreet 1</label>'
            '</form>')
    out = catalog_sync._parse_location_checkboxes(html)
    assert out == {"loc-1": "Resident Services Office Leutzsch"}


def test_sync_city_writes_english_service_file_on_drift(tmp_catalog_root):
    """When the service catalog drifts, the English label file is rewritten
    alongside the German one so the two never diverge."""
    http = _build_probe_http(
        services_page=_services_page_html(),
        locations_pages_by_target_uid={
            "u1": {"loc-1": "Bürgerbüro Eins"},
            "u2": {"loc-1": "Bürgerbüro Eins"},
        },
    )
    http.get.side_effect = _stack_get([
        MagicMock(status_code=200, json=lambda: {"success": True, "results": [
            {"uid": "u1", "display_name": "Personalausweis beantragen",
             "data": {"display_name_en": "Applying for an identity card"}},
            {"uid": "u2", "display_name": "Reisepass beantragen",
             "data": {"display_name_en": "Applying for a passport"}},
        ]}),
    ], probe_get_side_effect=http.get.side_effect)
    catalog_sync.sync_city("leipzig", http, alert_fn=lambda *a, **k: None,
                           catalog_root=tmp_catalog_root)
    en = json.loads((tmp_catalog_root / "leipzig" / "appointment_type.en.json").read_text())
    assert en == {"Applying for a passport": "u2",
                  "Applying for an identity card": "u1"}


def test_fetch_locations_handles_8443_redirect():
    """POST returning 302 to a :8443 host → follow with port-rewritten URL."""
    http = MagicMock()
    loc_html = _locations_page_html({"loc-1": "Bürgerbüro Eins"})

    def _get(url, *a, **kw):
        if "search_result" in url:
            return MagicMock(status_code=200,
                             url=f"{LEIPZIG_BASE}/?wsid=w&uid={LEIPZIG_UID}",
                             text="", headers={})
        if ":8443/" in url:
            pytest.fail(f"Should have rewritten :8443 in url={url}")
        return MagicMock(status_code=200, text=loc_html if "rev=" not in url else _services_page_html(),
                         headers={}, url=url)

    def _post(url, data=None, *a, **kw):
        return MagicMock(
            status_code=302,
            text="",
            headers={"Location": f"{LEIPZIG_BASE.replace('https://','https://').replace('/m/','')}".replace(
                "terminvereinbarung.leipzig.de", "terminvereinbarung.leipzig.de:8443") + "/redirected"},
            url=url,
        )

    http.get.side_effect = _get
    http.post.side_effect = _post
    # Just verify we attempt to follow the rewritten URL (no :8443) — function should not raise.
    out = catalog_sync.fetch_locations(http, LEIPZIG_BASE, LEIPZIG_UID,
                                       service_uids=["svc"], steps=STEPS)
    # No assertion on returned dict — it's the redirected GET response that contains the location HTML.
    # The pytest.fail in _get would fire if rewrite didn't happen.
    assert isinstance(out, dict)


# ---------- sync_city ----------

@pytest.fixture
def tmp_catalog_root(tmp_path):
    """Build a minimal catalog/leipzig/ directory with seed files."""
    root = tmp_path / "catalog"
    city = root / "leipzig"
    city.mkdir(parents=True)
    (city / "scraper_config.json").write_text(json.dumps({
        "vendor": "smartcjm",
        "base_url": LEIPZIG_BASE,
        "uid": LEIPZIG_UID,
        "steps": STEPS,
    }))
    (city / "appointment_type.json").write_text(json.dumps({
        "Personalausweis beantragen": "u1",
    }))
    (city / "locations.json").write_text(json.dumps({
        "Bürgerbüro Eins": "loc-1",
    }))
    return root


def test_sync_city_no_drift_makes_no_writes(tmp_catalog_root):
    """If live data matches files, mtime should not change."""
    svc_file = tmp_catalog_root / "leipzig" / "appointment_type.json"
    loc_file = tmp_catalog_root / "leipzig" / "locations.json"
    svc_mtime_before = svc_file.stat().st_mtime
    loc_mtime_before = loc_file.stat().st_mtime
    http = _build_probe_http(
        services_page=_services_page_html(),
        locations_pages_by_target_uid={
            "u1": {"loc-1": "Bürgerbüro Eins"},
        },
    )
    http.get.side_effect = _stack_get([
        # get_service_list → matches catalog
        MagicMock(status_code=200, json=lambda: {"success": True, "results": [
            {"uid": "u1", "display_name": "Personalausweis beantragen"}
        ]}),
    ], probe_get_side_effect=http.get.side_effect)
    alerts: list = []
    result = catalog_sync.sync_city("leipzig", http,
                                    alert_fn=lambda *a, **k: alerts.append((a, k)),
                                    catalog_root=tmp_catalog_root)
    assert result["service_drift"] == {}
    assert result["location_drift"] == {}
    assert alerts == []
    assert svc_file.stat().st_mtime == svc_mtime_before
    assert loc_file.stat().st_mtime == loc_mtime_before


def test_sync_city_writes_and_alerts_on_service_drift(tmp_catalog_root):
    """Live returns a new service not in catalog → file rewritten + alert called."""
    http = _build_probe_http(
        services_page=_services_page_html(),
        locations_pages_by_target_uid={
            "u1": {"loc-1": "Bürgerbüro Eins"},
            "u2": {"loc-1": "Bürgerbüro Eins"},
        },
    )
    http.get.side_effect = _stack_get([
        MagicMock(status_code=200, json=lambda: {"success": True, "results": [
            {"uid": "u1", "display_name": "Personalausweis beantragen"},
            {"uid": "u2", "display_name": "Reisepass beantragen"},
        ]}),
    ], probe_get_side_effect=http.get.side_effect)
    alerts: list = []
    result = catalog_sync.sync_city("leipzig", http,
                                    alert_fn=lambda *a, **k: alerts.append((a, k)),
                                    catalog_root=tmp_catalog_root)
    assert result["service_drift"].get("added") == ["Reisepass beantragen"]
    written = json.loads((tmp_catalog_root / "leipzig" / "appointment_type.json").read_text())
    assert written == {"Personalausweis beantragen": "u1", "Reisepass beantragen": "u2"}
    assert len(alerts) == 1


def test_sync_city_tolerates_network_error(tmp_catalog_root):
    """If get_service_list raises a network error, return cleanly without rewrite."""
    http = MagicMock()
    http.get.side_effect = requests.ConnectionError("simulated network failure")
    svc_file = tmp_catalog_root / "leipzig" / "appointment_type.json"
    svc_mtime_before = svc_file.stat().st_mtime
    alerts: list = []
    result = catalog_sync.sync_city("leipzig", http,
                                    alert_fn=lambda *a, **k: alerts.append((a, k)),
                                    catalog_root=tmp_catalog_root)
    assert result["error"]
    assert svc_file.stat().st_mtime == svc_mtime_before
    assert alerts == []


# ---------- helper ----------

def _stack_get(prefix_responses, probe_get_side_effect):
    """Compose: first N calls return prefix_responses; subsequent calls fall through to probe_get_side_effect."""
    state = {"i": 0}

    def _side(url, *a, **kw):
        if state["i"] < len(prefix_responses):
            r = prefix_responses[state["i"]]
            state["i"] += 1
            return r
        return probe_get_side_effect(url, *a, **kw)
    return _side
