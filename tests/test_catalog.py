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
