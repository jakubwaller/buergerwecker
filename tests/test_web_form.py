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
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
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
    assert '>Bonn</a>' in body                         # bare name
    assert 'Bonn: ' not in body                        # not the long label
    assert '<div class="city-cell">' in body
    assert 'Leipzig: Bürgerbüro-Termine' not in body   # long label stays out
    assert '>Bürgerbüro</a>' in body                   # short office names
    assert '>Ausländerbehörde</a>' in body
    # Bochum grew from one tenant to three (Straßenverkehrsamt request), so it
    # is a cell now, not a bare link.
    assert '>Bochum</a>' not in body
    assert '>Kfz-Zulassungsstelle</a>' in body
    assert '>Führerscheinstelle</a>' in body


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

def test_faq_tells_subscribers_about_the_daily_mail_cap(client, monkeypatch):
    de = client.get("/leipzig?lang=de").data.decode()
    en = client.get("/leipzig?lang=en").data.decode()
    assert "Wie viele Mails bekomme ich?" in de
    assert "Höchstens 2 in 24 Stunden" in de
    assert "How many emails will I get?" in en
    assert "At most 2 in any 24 hours" in en
    # With the cap switched off there is nothing to promise.
    monkeypatch.setenv("MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY", "0")
    from app.web import create_app
    off = create_app().test_client().get("/leipzig?lang=de").data.decode()
    assert "Wie viele Mails bekomme ich?" not in off


@pytest.mark.parametrize("lang, text", [
    ("de", "Gerade sind viele Anmeldungen da"),
    ("en", "Lots of sign-ups right now"),
])
def test_form_warns_of_a_held_confirmation_only_when_held(client, lang, text):
    from unittest.mock import patch
    body = client.get(f"/leipzig?lang={lang}").data.decode()
    assert text not in body
    with patch("app.confirmations.confirmation_held", return_value=True):
        body = client.get(f"/leipzig?lang={lang}").data.decode()
    notice = body[body.index(text):body.index('<button type="submit"')]
    assert "https://apps.apple.com/app/id6819977614" in notice
    assert "https://groups.google.com/g/buergerwecker-beta" in notice
    # the notice sits above the submit button
    assert body.index(text) < body.index('<button type="submit"')


def test_form_renders_without_the_notice_when_the_check_fails(client):
    from unittest.mock import patch
    with patch("app.confirmations.confirmation_held", side_effect=RuntimeError):
        r = client.get("/leipzig")
    assert r.status_code == 200
    assert "Gerade sind viele Anmeldungen da" not in r.data.decode()


@pytest.mark.parametrize("lang, tip", [
    ("de", "Tipp: In der Bürgerwecker-App kommen Benachrichtigungen per Push"),
    ("en", "Tip: in the Bürgerwecker app, notifications come by push"),
])
def test_queued_banner_points_to_the_app(client, lang, tip):
    body = client.get(f"/leipzig?confirmed=queued&lang={lang}").data.decode()
    banner = body[body.index(tip):]
    banner = banner[:banner.index("</div>")]   # the footer has an App Store link too
    assert "https://apps.apple.com/app/id6819977614" in banner
    assert "https://groups.google.com/g/buergerwecker-beta" in banner


def test_form_warns_when_the_providers_are_full_though_the_reserve_is_unused(client):
    """The reserve allows 150 more, but the providers can take nothing
    right now: the form must still warn."""
    from unittest.mock import patch
    with patch("app.confirmations.daily_room", return_value=0):
        body = client.get("/leipzig").data.decode()
    assert "Gerade sind viele Anmeldungen da" in body


@pytest.mark.parametrize("lang, text", [
    ("de", "E-Mails sind gerade knapp"),
    ("en", "Email is tight right now"),
])
@pytest.mark.parametrize("path", ["/", "/leipzig"])
def test_pages_nudge_to_the_app_only_under_mail_pressure(client, lang, text, path):
    from unittest.mock import patch
    body = client.get(f"{path}?lang={lang}").data.decode()
    assert text not in body
    with patch("app.cycle.subscriber_cap", return_value=(1, True)):
        body = client.get(f"{path}?lang={lang}").data.decode()
    notice = body[body.index(text):]
    notice = notice[:notice.index("</div>")]
    assert "1 email alert a day" in notice or "1 Benachrichtigung am Tag" in notice
    assert "https://apps.apple.com/app/id6819977614" in notice
    assert "https://groups.google.com/g/buergerwecker-beta" in notice


def test_main_page_notice_sits_above_the_city_grid(client):
    from unittest.mock import patch
    with patch("app.cycle.subscriber_cap", return_value=(1, True)):
        body = client.get("/").data.decode()
    assert body.index("E-Mails sind gerade knapp") < body.index('id="staedte"')


def test_one_notice_carries_both_pressure_and_held(client):
    from unittest.mock import patch
    with patch("app.cycle.subscriber_cap", return_value=(1, True)), \
         patch("app.confirmations.confirmation_held", return_value=True):
        body = client.get("/leipzig").data.decode()
    assert "E-Mails sind gerade knapp" in body
    assert "Gerade sind viele Anmeldungen da" in body
    assert body.count("Mit der App kommen Benachrichtigungen per Push") == 1


def test_a_failing_pressure_check_leaves_the_held_notice(client):
    from unittest.mock import patch
    with patch("app.cycle.subscriber_cap", side_effect=RuntimeError), \
         patch("app.confirmations.confirmation_held", return_value=True):
        r = client.get("/leipzig")
    body = r.data.decode()
    assert r.status_code == 200
    assert "E-Mails sind gerade knapp" not in body
    assert "Gerade sind viele Anmeldungen da" in body


def test_untightened_cap_shows_no_notice(client):
    from unittest.mock import patch
    with patch("app.cycle.subscriber_cap", return_value=(2, False)):
        body = client.get("/").data.decode()
    assert "E-Mails sind gerade knapp" not in body


# --- how crowded a city is -------------------------------------------------

def _queue_db(client):
    from app.config import load_config
    return connect(load_config().db_path)


def _queue_sub(conn, n, city="leipzig", *, confirmed="datetime('now')",
               expires="datetime('now','+30 days')", deleted="NULL"):
    for i in range(n):
        conn.execute(
            "INSERT INTO subscriptions (email, city, filters_json, confirmed_at, "
            f"expires_at, deleted_at) VALUES (?, ?, '{{}}', {confirmed}, "
            f"{expires}, {deleted})", (f"q{i}@example.com", city))


def _queue_slots(conn, sub_id, hashes, when="datetime('now','-1 days')"):
    for h in hashes:
        conn.execute("INSERT INTO seen_slots (subscription_id, slot_hash, sent_at) "
                     f"VALUES (?, ?, {when})", (sub_id, h))


def test_queue_line_shows_waiting_and_daily_slots_from_50(client):
    conn = _queue_db(client)
    _queue_sub(conn, 50)
    # 14 distinct slots, each told to three subscribers: counts once, 14/7 = 2 a day.
    for sid in (1, 2, 3):
        _queue_slots(conn, sid, [f"h{i}" for i in range(14)])
    _queue_slots(conn, 4, ["old"], when="datetime('now','-8 days')")
    de = client.get("/leipzig").data.decode()
    assert "Für Leipzig warten gerade 50 Personen auf einen Termin." in de
    assert "im Schnitt 2 freie Termine am Tag gemeldet. Rechne also mit Wartezeit." in de
    en = client.get("/leipzig?lang=en").data.decode()
    assert "50 people are waiting for an appointment in Leipzig right now." in en
    assert "about 2 free slots a day on average. So expect to wait." in en


def test_queue_line_hidden_below_50(client):
    _queue_sub(_queue_db(client), 49)
    assert "warten gerade" not in client.get("/leipzig").data.decode()


def test_queue_line_drops_slot_sentences_when_nothing_was_reported(client):
    conn = _queue_db(client)
    _queue_sub(conn, 50)
    _queue_slots(conn, 1, ["a"])       # 1 slot in 7 days rounds to 0 a day
    body = client.get("/leipzig").data.decode()
    assert "50 Personen" in body
    assert "Rechne also mit Wartezeit" not in body


def test_queue_counts_only_live_subscriptions_of_this_city(client):
    conn = _queue_db(client)
    _queue_sub(conn, 50)
    _queue_sub(conn, 5, deleted="datetime('now')")
    _queue_sub(conn, 5, expires="datetime('now','-1 days')")
    _queue_sub(conn, 5, confirmed="NULL")
    _queue_sub(conn, 5, city="nuernberg")
    assert "gerade 50 Personen" in client.get("/leipzig").data.decode()


def test_queue_db_error_renders_form_without_the_line(client, monkeypatch):
    import app.web as web
    _queue_sub(_queue_db(client), 50)
    real = web.connect
    calls = []

    def flaky(path):
        conn = real(path)
        calls.append(1)
        if len(calls) > 1:          # the second connection is the queue's
            conn.close()
        return conn
    monkeypatch.setattr(web, "connect", flaky)
    r = client.get("/leipzig")
    assert r.status_code == 200
    assert b"warten gerade" not in r.data
