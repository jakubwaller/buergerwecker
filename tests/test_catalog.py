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
