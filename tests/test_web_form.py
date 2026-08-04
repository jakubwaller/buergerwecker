import pytest
from app.web import create_app
from app.db import connect, init_schema

@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x" * 32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "1")
    monkeypatch.setenv("MAILJET_API_KEY", "mj")
    monkeypatch.setenv("MAILJET_API_SECRET", "mj")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x")
    monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("RESEND_API_KEY", "re")
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS", "24")
    monkeypatch.setenv("RATE_LIMIT_MINUTES", "15")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE", "10")
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS", "2")
    monkeypatch.setenv("DEVELOPER_EMAIL", "dev@x")
    monkeypatch.setenv("KOFI_URL", "https://k")
    conn = connect(db_path)
    init_schema(conn)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200

def test_form_renders(client):
    r = client.get("/leipzig")
    assert r.status_code == 200
    assert b"E-Mail" in r.data
    assert b"website" in r.data  # honeypot field name

def test_root_is_the_city_picker_not_one_citys_form(client):
    """/ lists the cities and offers no form — no city is the default."""
    body = client.get("/").data.decode()
    assert 'name="email"' not in body                 # no sign-up form here
    assert "class=\"city-grid\"" in body
    assert 'href="/leipzig"' in body and 'href="/nuernberg"' in body
    assert "Wähle deine Stadt" in body

def test_root_picker_speaks_english_too(client):
    body = client.get("/?lang=en").data.decode()
    assert "Pick your city" in body
    assert 'name="email"' not in body

def test_form_offers_de_and_en(client):
    r_de = client.get("/leipzig?lang=de")
    r_en = client.get("/leipzig?lang=en")
    assert r_de.status_code == 200 and r_en.status_code == 200
    assert b"Anmelden" in r_de.data or b"abonnieren" in r_de.data.lower()

def _en_switch_href(html):
    import re
    m = re.search(r'href="([^"]*)"[^>]*hreflang="en"', html)
    assert m, "EN language-switch link not found"
    return m.group(1)

def test_lang_switch_preserves_form_query_params(client):
    """Switching language on the post-subscribe page must keep ?confirmed so
    the banner (and city) survive the switch."""
    en_href = _en_switch_href(client.get("/?confirmed=pending&lang=de").data.decode())
    assert "confirmed=pending" in en_href and "lang=en" in en_href

def test_admin_has_no_language_switcher(client):
    """The admin page is an internal, English-only stats page. The inherited
    DE/EN toggle does nothing there, so it must be hidden."""
    r = client.get("/admin?token=" + "a" * 32)
    assert r.status_code == 200
    assert 'hreflang="en"' not in r.data.decode()  # switcher link absent

def test_form_keeps_language_switcher(client):
    """The public form must still offer the language switcher."""
    assert 'hreflang="en"' in client.get("/").data.decode()

def test_pending_banner_shown_after_subscribe(client):
    """/subscribe redirects to /?confirmed=pending. That page MUST tell the
    user to check their inbox and confirm — otherwise the redirect looks like
    the form just reloaded with no feedback. Regression test."""
    r_de = client.get("/?confirmed=pending&lang=de")
    assert r_de.status_code == 200
    assert "fast geschafft" in r_de.data.decode().lower()
    r_en = client.get("/?confirmed=pending&lang=en")
    assert "almost done" in r_en.data.decode().lower()

def test_no_pending_banner_on_bare_form(client):
    """The pending banner must only appear after subscribing, not on the
    bare form."""
    body = client.get("/leipzig").data.decode().lower()
    assert "fast geschafft" not in body
    body_en = client.get("/leipzig?lang=en").data.decode().lower()
    assert "almost done" not in body_en

def test_form_en_shows_english_service_and_location_labels(client):
    """The English form must render Leipzig's English service/location labels,
    not the German ones (regression: EN page showed a German dropdown)."""
    body = client.get("/leipzig?lang=en").data.decode()
    assert "Applying for an identity card" in body          # EN service label
    assert "Resident Services Office Otto-Schill-Straße" in body  # EN location label
    assert "Personalausweis beantragen" not in body         # German label gone
    assert "Bürgerbüro Otto-Schill-Straße (Zentrum)" not in body


def test_form_de_still_shows_german_labels(client):
    body = client.get("/leipzig?lang=de").data.decode()
    assert "Personalausweis beantragen" in body
    assert "Bürgerbüro Otto-Schill-Straße (Zentrum)" in body
    assert "Applying for an identity card" not in body


def test_manage_page_localizes_labels_to_subscriber_language(client):
    """The /manage page must use the subscriber's stored language for the
    dropdown and locations, just like the public form."""
    import os
    from datetime import time
    from app.db import connect
    from app.models import Filter
    from app.repo import insert_pending, confirm
    from app.tokens import sign
    conn = connect(os.environ["DB_PATH"])
    f = Filter(appointment_types=["b04658d5-8d85-469a-a635-93337e055b73"],
               locations="all", weekdays=[1, 2, 3, 4, 5],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    sid = insert_pending(conn, email="en@x.com", city="leipzig", language="en",
                         filter_=f, ttl_days=90)
    confirm(conn, sid)
    tok = sign(sid, "manage", primary="x" * 32, previous="")
    body = client.get(f"/manage/{tok}").data.decode()
    assert "Applying for an identity card" in body
    assert "Personalausweis beantragen" not in body


def test_subscribe_error_banner_shown(client):
    """When a confirmation email could not be sent, /?subscribe_error=mail
    shows a localized, retryable error banner."""
    r_de = client.get("/?subscribe_error=mail&lang=de")
    assert r_de.status_code == 200
    assert "erneut" in r_de.data.decode().lower()
    r_en = client.get("/?subscribe_error=mail&lang=en")
    assert "try again" in r_en.data.decode().lower()

def test_no_error_banner_on_bare_form(client):
    body = client.get("/leipzig").data.decode().lower()
    assert "leider nicht geklappt" not in body
    body_en = client.get("/leipzig?lang=en").data.decode().lower()
    assert "didn't go through" not in body_en


def test_abh_tenant_form_renders_with_own_copy_and_cross_links(client):
    """/leipzig-abh renders the Ausländerbehörde tenant: its display.json
    heading, the Termin-Code note, its service, and a cross-link back to the
    Bürgerbüro tenant (and vice versa)."""
    abh = client.get("/leipzig-abh").data.decode()
    assert "Abhol-Termine bei der Leipziger Ausländerbehörde" in abh
    assert "Termin-Code" in abh
    assert "Ausgabe  Aufenthaltsdokument" in abh
    assert 'href="/leipzig"' in abh                  # link back
    ba = client.get("/").data.decode()
    assert 'href="/leipzig-abh"' in ba               # link over


def test_unknown_city_is_a_404_with_the_picker(client):
    """A retired or mistyped city must be a real 404, not a soft one that
    redirects to a 200 — but still show the list, which is the way out."""
    r = client.get("/nope-nothing-here")
    assert r.status_code == 404
    body = r.data.decode()
    assert "nope-nothing-here" in body and "Wähle deine Stadt" in body


def test_old_query_urls_redirect_permanently(client):
    """/?city=x is in the press article, on Reddit and in old previews."""
    r = client.get("/?city=nuernberg")
    assert r.status_code == 301
    assert r.headers["Location"] == "/nuernberg"
    r_en = client.get("/?city=nuernberg&lang=en")
    assert r_en.status_code == 301
    assert r_en.headers["Location"] == "/nuernberg?lang=en"


def test_city_switcher_groups_a_multi_tenant_city_into_one_cell(client):
    """One entry per city, not per tenant: a single-tenant city stays a bare
    link, a city with several Ämter becomes one cell listing them by short
    name. The long "Leipzig: …" tenant labels must not reach the grid — they
    are what forced the ellipsis that made the old flat list unreadable."""
    body = client.get("/dresden").data.decode()
    assert '<details class="city-switch"' in body
    assert '>Bochum</a>' in body                       # bare name
    assert 'Bochum: ' not in body                      # not the long label
    assert '<div class="city-cell">' in body
    assert 'Leipzig: Bürgerbüro-Termine' not in body   # long label stays out
    assert '>Bürgerbüro</a>' in body                   # short office names
    assert '>Ausländerbehörde</a>' in body


def test_city_switcher_counts_cities_not_tenants(client):
    """The summary count is what the visitor scans past — it must count places,
    not rows. Münster alone contributes seven tenants and one entry."""
    import re
    from app.catalog import available_cities
    body = client.get("/dresden").data.decode()
    shown = int(re.search(r"Weitere Städte &amp; Ämter \((\d+)\)", body).group(1))
    assert shown < len(available_cities()) - 1
    assert body.count('<div class="city-cell">') >= 2   # Leipzig and Münster


def test_current_city_offices_get_their_own_row(client):
    """Landing on one Amt of a multi-Amt city offers its siblings directly,
    with the current one unlinked — the second step of city → Amt."""
    body = client.get("/muenster").data.decode()
    assert 'class="office-switch"' in body
    assert "Ämter in Münster:" in body
    assert '<span class="current" aria-current="page">Bürgeramt</span>' in body
    assert 'href="/muenster-standesamt"' in body
    # A city with one tenant has no such row, and never lists itself twice.
    single = client.get("/dresden").data.decode()
    assert 'class="office-switch"' not in single

def test_form_has_fairness_faq_in_both_languages(client):
    de = client.get("/leipzig?lang=de").data.decode()
    en = client.get("/leipzig?lang=en").data.decode()
    assert "Ist das fair?" in de
    assert "Bucht das Tool Termine?" in de
    assert "Is this fair?" in en
    assert "Does this book appointments?" in en

def test_form_embeds_service_locations_map_when_present(client, tmp_path, monkeypatch):
    """A catalog with service_locations.json gets the JSON map + filter script;
    the map keys the JS uses must be the raw uuids."""
    import json
    from app import catalog as catalog_mod
    src = catalog_mod.CATALOG_ROOT / "leipzig"
    root = tmp_path / "catalog"
    city = root / "leipzig"
    city.mkdir(parents=True)
    for f in ("appointment_type.json", "locations.json", "scraper_config.json"):
        (city / f).write_text((src / f).read_text())
    loc_uuid = next(iter(json.loads((src / "locations.json").read_text()).values()))
    svc_uuid = next(iter(json.loads((src / "appointment_type.json").read_text()).values()))
    (city / "service_locations.json").write_text(
        json.dumps({svc_uuid: [loc_uuid]}))
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", root)
    catalog_mod.load_catalog.cache_clear()
    try:
        body = client.get("/leipzig").data.decode()
    finally:
        catalog_mod.load_catalog.cache_clear()
    assert 'id="service-locations"' in body
    assert svc_uuid in body and loc_uuid in body

def test_form_omits_filter_script_without_map(client, tmp_path, monkeypatch):
    from app import catalog as catalog_mod
    src = catalog_mod.CATALOG_ROOT / "leipzig"
    root = tmp_path / "catalog"
    city = root / "leipzig"
    city.mkdir(parents=True)
    for f in ("appointment_type.json", "locations.json", "scraper_config.json"):
        (city / f).write_text((src / f).read_text())
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", root)
    catalog_mod.load_catalog.cache_clear()
    try:
        body = client.get("/leipzig").data.decode()
    finally:
        catalog_mod.load_catalog.cache_clear()
    assert 'id="service-locations"' not in body
