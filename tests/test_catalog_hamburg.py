from app.catalog import load_catalog, Catalog


def test_load_hamburg_catalog():
    cat = load_catalog("hamburg")
    assert isinstance(cat, Catalog)
    assert len(cat.appointment_types) >= 40   # ~50 bookable Mandant-1 services
    assert len(cat.locations) >= 25            # 29 Standorte


def test_hamburg_scraper_config_is_dtms():
    cfg = load_catalog("hamburg").scraper_config
    assert cfg["vendor"] == "dtms"
    assert cfg["mandant"] == 1
    assert cfg["app_key"]
    assert cfg["base_url"].startswith("https://driveport.de/termineapi")
    assert cfg["portal_url"].startswith("https://driveport.de/termine")


def test_hamburg_known_service_and_location_ids():
    cat = load_catalog("hamburg")
    # DienstleistungID 14 = Personalausweis; StandortID 2 = Altona.
    assert cat.appointment_types["Personalausweis beantragen"] == "14"
    assert cat.appointment_type_name_for("14") == "Personalausweis beantragen"
    assert cat.locations["Standort Altona"] == "2"


def test_hamburg_ids_are_numeric_strings():
    cat = load_catalog("hamburg")
    assert all(v.isdigit() for v in cat.appointment_types.values())
    assert all(v.isdigit() for v in cat.locations.values())
