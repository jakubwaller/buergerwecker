import json
import pytest
from app.catalog import load_catalog, CatalogError, Catalog

def test_load_leipzig_catalog():
    cat = load_catalog("leipzig")
    assert isinstance(cat, Catalog)
    assert len(cat.appointment_types) > 0
    assert len(cat.locations) > 0
    # appointment_types and locations are name → uuid maps
    sample_name, sample_uuid = next(iter(cat.appointment_types.items()))
    assert isinstance(sample_name, str)
    assert len(sample_uuid) == 36  # UUID

def test_load_unknown_city_raises():
    with pytest.raises(CatalogError):
        load_catalog("atlantis")

def test_catalog_lookup_helpers():
    cat = load_catalog("leipzig")
    name = next(iter(cat.appointment_types.keys()))
    uuid = cat.appointment_types[name]
    assert cat.appointment_type_name_for(uuid) == name
    assert cat.appointment_type_uuid_for(name) == uuid


# ---------- English localization ----------

def test_leipzig_catalog_loads_english_names():
    cat = load_catalog("leipzig")
    assert cat.appointment_types_en, "expected English service names to load"
    assert cat.locations_en, "expected English location names to load"
    # Same uuid set as German — English files only differ in the display labels.
    assert set(cat.appointment_types_en.values()) == set(cat.appointment_types.values())
    assert set(cat.locations_en.values()) == set(cat.locations.values())


def test_appointment_types_for_en_returns_english_labels():
    cat = load_catalog("leipzig")
    de = cat.appointment_types_for("de")
    en = cat.appointment_types_for("en")
    assert de == cat.appointment_types  # de view is the German map verbatim
    # Known mapping: Personalausweis → "Applying for an identity card".
    uid = "b04658d5-8d85-469a-a635-93337e055b73"
    assert en["Applying for an identity card"] == uid
    assert "Personalausweis beantragen" not in en  # German label replaced


def test_locations_for_en_returns_english_labels():
    cat = load_catalog("leipzig")
    en = cat.locations_for("en")
    assert "Resident Services Office Otto-Schill-Straße" in en
    # English view keeps the full German uuid set (labels swapped, set unchanged).
    assert set(en.values()) == set(cat.locations.values())


def test_for_lang_falls_back_to_german_per_missing_uuid():
    """A uuid present in German but missing from the English table must still
    appear (labeled in German) rather than disappear from the dropdown."""
    cat = Catalog(
        city="x",
        appointment_types={"DE A": "u1", "DE B": "u2"},
        locations={},
        scraper_config={},
        appointment_types_en={"EN A": "u1"},  # u2 has no English label
        locations_en={},
    )
    en = cat.appointment_types_for("en")
    assert en == {"DE B": "u2", "EN A": "u1"}  # u2 falls back to its German label


def test_for_lang_with_no_english_table_returns_german():
    cat = Catalog(
        city="x",
        appointment_types={"DE A": "u1"},
        locations={"DE L": "l1"},
        scraper_config={},
    )
    assert cat.appointment_types_for("en") == {"DE A": "u1"}
    assert cat.locations_for("en") == {"DE L": "l1"}


# ---------- uuid → label lookups (for email rendering) ----------

def _label_catalog():
    return Catalog(
        city="x",
        appointment_types={"Personalausweis": "u1"},
        locations={"Bürgerbüro Mitte": "l1", "Bürgerbüro Nord": "l2"},
        scraper_config={},
        appointment_types_en={"Identity card": "u1"},
        locations_en={"Citizen office centre": "l1"},
    )


def test_appointment_type_label_localizes():
    cat = _label_catalog()
    assert cat.appointment_type_label("u1", "de") == "Personalausweis"
    assert cat.appointment_type_label("u1", "en") == "Identity card"


def test_location_label_localizes_with_german_fallback_per_uuid():
    cat = _label_catalog()
    assert cat.location_label("l1", "de") == "Bürgerbüro Mitte"
    assert cat.location_label("l1", "en") == "Citizen office centre"
    # l2 has no English label — fall back to its German name, not the uuid.
    assert cat.location_label("l2", "en") == "Bürgerbüro Nord"


def test_labels_fall_back_to_uuid_when_unknown():
    """A uuid absent from the catalog must render as the raw uuid, never crash
    or blank — slots can carry an out-of-catalog uuid (real prod failure mode)."""
    cat = _label_catalog()
    assert cat.appointment_type_label("ghost-uuid", "de") == "ghost-uuid"
    assert cat.location_label("ghost-uuid", "en") == "ghost-uuid"


def test_excluded_services_are_never_offered(tmp_path, monkeypatch):
    """`exclude_services` must hold on read too, not just during sync: a stale
    or hand-edited appointment_type.json can otherwise put an Anliegen back on
    the sign-up form."""
    import json
    from app import catalog as catalog_mod
    city = tmp_path / "testcity"
    city.mkdir()
    (city / "scraper_config.json").write_text(json.dumps(
        {"vendor": "tevis", "base_url": "https://x", "md": 13, "mdt": 217,
         "exclude_services": ["2471", "2472"]}), encoding="utf-8")
    (city / "appointment_type.json").write_text(json.dumps(
        {"Eheschließung": "2431", "SBGG-Erklärung": "2471"}), encoding="utf-8")
    (city / "locations.json").write_text(json.dumps({"Standesamt": "254"}),
                                         encoding="utf-8")
    (city / "service_locations.json").write_text(json.dumps(
        {"2431": ["254"], "2471": ["254"]}), encoding="utf-8")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        cat = catalog_mod.load_catalog("testcity")
        assert cat.appointment_types == {"Eheschließung": "2431"}
        assert "2471" not in cat.service_locations
    finally:
        catalog_mod.load_catalog.cache_clear()


def test_shipped_muenster_standesamt_offers_only_the_approved_sbgg_anliegen():
    """Stadt Münster's ruling of 2026-08-13: the Anmeldung (2471) may be
    listed, the Abgabe (2472) must not — it carries special statutory
    deadlines, per the Standesamt. 2472's exclusion is a decision by the
    city, not a pending approval."""
    from app.catalog import load_catalog
    cat = load_catalog("muenster-standesamt")
    assert set(cat.scraper_config["exclude_services"]) == {"2472"}
    assert "2471" in cat.appointment_types.values()
    assert "2472" not in cat.appointment_types.values()


def test_shipped_muenster_standesamt_declares_the_sbgg_anliegen_sensitive():
    """Both ids carry the Art. 9 marking — 2472 too, so a subscription from
    before its withdrawal keeps its redaction, and so does 2471's consent
    box on the form."""
    from app.catalog import load_catalog
    cat = load_catalog("muenster-standesamt")
    assert cat.sensitive_services == frozenset({"2471", "2472"})
    assert cat.is_sensitive("2471") and cat.is_sensitive("2472")
    # 2471 is offered, so the form needs the extra consent box.
    assert cat.has_sensitive


def test_sensitive_services_survive_a_service_being_withdrawn(tmp_path,
                                                              monkeypatch):
    """`is_sensitive` answers from the declaration, not from what is currently
    offered: a subscription taken before the Anliegen was withdrawn must keep
    its redaction in the digest."""
    import json
    from app import catalog as catalog_mod
    city = tmp_path / "testcity"
    city.mkdir()
    (city / "scraper_config.json").write_text(json.dumps(
        {"vendor": "tevis", "base_url": "https://x", "md": 13, "mdt": 217,
         "sensitive_services": ["2471"], "exclude_services": ["2471"]}),
        encoding="utf-8")
    (city / "appointment_type.json").write_text(json.dumps(
        {"Eheschließung": "2431", "SBGG-Erklärung": "2471"}), encoding="utf-8")
    (city / "locations.json").write_text(json.dumps({"Standesamt": "254"}),
                                         encoding="utf-8")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        cat = catalog_mod.load_catalog("testcity")
        assert cat.is_sensitive("2471")     # still protected
        assert not cat.has_sensitive        # but no longer offered
        assert not cat.is_sensitive("2431")
    finally:
        catalog_mod.load_catalog.cache_clear()


def test_only_city_approved_tenants_offer_a_sensitive_service():
    """Guards the promise made to the cities: a special-category Anliegen goes
    live only after the city has agreed. Münster approved the Standesamt's
    SBGG-Anmeldung on 2026-08-13; every other tenant (including the still
    unbuilt Gesundheitsamt) needs its own approval before joining this list."""
    from app.catalog import available_cities, load_catalog, CatalogError
    offering = []
    for city in available_cities():
        try:
            if load_catalog(city).has_sensitive:
                offering.append(city)
        except CatalogError:
            continue
    assert offering == ["muenster-standesamt"]


def test_shipped_bochum_kfz_tenants_protect_the_shared_host():
    """The Straßenverkehrsamt tenants share termine.bochum.de with a
    Bürgerbüro that already 429s at 180s, so both must poll at the slow
    cadence; and the Führerscheinstelle's 15 Anliegen at one office sit a
    single upstream addition away from the global cap of 16, so it carries
    its own headroom (the per-type "all" collapse frees nothing there)."""
    from urllib.parse import urlsplit
    from app.catalog import load_catalog
    buergerbuero_host = urlsplit(
        load_catalog("bochum").scraper_config["base_url"]).netloc
    for slug in ("bochum-kfz", "bochum-fuehrerschein"):
        cat = load_catalog(slug)
        scfg = cat.scraper_config
        assert urlsplit(scfg["base_url"]).netloc == buergerbuero_host
        assert scfg["poll_interval_seconds"] >= 300
        assert "locations" not in scfg["steps"]   # single-office flow
        assert len(cat.locations) == 1
    fs = load_catalog("bochum-fuehrerschein")
    assert fs.scraper_config["max_plans"] > len(fs.appointment_types)


# ---------- a broken tenant is skipped, not fatal ----------

def _write_tenant(root, slug):
    city = root / slug
    city.mkdir()
    (city / "scraper_config.json").write_text(json.dumps(
        {"vendor": "tevis", "base_url": "https://x", "md": 1, "mdt": 2}),
        encoding="utf-8")
    (city / "appointment_type.json").write_text(json.dumps({"Perso": "1"}),
                                                encoding="utf-8")
    (city / "locations.json").write_text(json.dumps({"Amt": "9"}),
                                         encoding="utf-8")
    return city


def test_malformed_required_file_is_a_catalog_error(tmp_path, monkeypatch):
    """A JSONDecodeError used to walk past every `except CatalogError`, so one
    hand-edited file took down every tenant's page and the sitemap."""
    from app import catalog as catalog_mod
    city = _write_tenant(tmp_path, "broken")
    (city / "appointment_type.json").write_text('{"Perso": "1",}',
                                                encoding="utf-8")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        with pytest.raises(CatalogError, match="appointment_type.json"):
            catalog_mod.load_catalog("broken")
    finally:
        catalog_mod.load_catalog.cache_clear()


def test_rewritten_catalog_file_is_noticed_without_a_cache_clear(tmp_path, monkeypatch):
    """catalog_sync rewrites files in the poller; the web workers are other
    processes, so the cache has to notice on its own."""
    import os
    from app import catalog as catalog_mod
    city = _write_tenant(tmp_path, "livecity")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", tmp_path)
    catalog_mod.load_catalog.cache_clear()
    try:
        assert catalog_mod.load_catalog("livecity").appointment_types == {"Perso": "1"}
        path = city / "appointment_type.json"
        path.write_text(json.dumps({"Perso": "1", "Reisepass": "2"}),
                        encoding="utf-8")
        # Same second as the first write is possible; force a distinct mtime.
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert catalog_mod.load_catalog("livecity").appointment_types == {
            "Perso": "1", "Reisepass": "2"}
    finally:
        catalog_mod.load_catalog.cache_clear()


def test_every_tenant_loads_from_one_cache(monkeypatch):
    """lru_cache(maxsize=8) over 38 tenants missed on nearly every call."""
    from app import catalog as catalog_mod
    from app.catalog import available_cities
    catalog_mod.load_catalog.cache_clear()
    cities = available_cities()
    for c in cities:
        load_catalog(c)
    calls = []
    monkeypatch.setattr(catalog_mod, "_read_catalog",
                        lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("re-read")))
    for c in cities:
        load_catalog(c)
    assert calls == []
