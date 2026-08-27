import pytest
from app.scrapers import get_scraper, UnsupportedCity

def test_get_scraper_leipzig():
    scraper = get_scraper("leipzig")
    assert hasattr(scraper, "poll")

def test_get_scraper_unknown():
    with pytest.raises(UnsupportedCity):
        get_scraper("atlantis")


def test_dispatch_follows_the_catalog_vendor(tmp_path, monkeypatch):
    """A new catalog directory polls without any registry edit."""
    import json
    from app import catalog as catalog_mod
    from app.scrapers import smartcjm, tevis
    for slug, vendor in (("newtown", "tevis"), ("othertown", "smartcjm")):
        city = tmp_path / slug
        city.mkdir()
        (city / "scraper_config.json").write_text(json.dumps({"vendor": vendor}))
        (city / "appointment_type.json").write_text("{}")
        (city / "locations.json").write_text("{}")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        assert get_scraper("newtown") is tevis
        assert get_scraper("othertown") is smartcjm
    finally:
        catalog_mod.load_catalog.cache_clear()


def test_unknown_vendor_is_unsupported(tmp_path, monkeypatch):
    import json
    from app import catalog as catalog_mod
    city = tmp_path / "vois"
    city.mkdir()
    (city / "scraper_config.json").write_text(json.dumps({"vendor": "vois"}))
    (city / "appointment_type.json").write_text("{}")
    (city / "locations.json").write_text("{}")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        with pytest.raises(UnsupportedCity, match="vois"):
            get_scraper("vois")
    finally:
        catalog_mod.load_catalog.cache_clear()
