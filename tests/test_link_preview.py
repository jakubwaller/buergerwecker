"""Open Graph tags — what LinkedIn, WhatsApp and Slack show for a shared link."""
import re
import pytest
from app.web import create_app
from app.db import connect, init_schema


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "5")
    monkeypatch.setenv("MAILJET_API_KEY", "mj"); monkeypatch.setenv("MAILJET_API_SECRET", "mj")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x"); monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("ADMIN_TOKEN", "a"*32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://buergerwecker.de")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS","24");monkeypatch.setenv("RATE_LIMIT_MINUTES","15")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE","10");monkeypatch.setenv("MAX_PLANS_PER_CITY","10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS","2")
    monkeypatch.setenv("DEVELOPER_EMAIL","dev@x");monkeypatch.setenv("KOFI_URL","https://k")
    init_schema(connect(str(tmp_path / "t.db")))
    app = create_app(); app.config["TESTING"] = True
    return app.test_client()


def _meta(html: str, prop: str) -> str | None:
    m = re.search(rf'<meta property="{re.escape(prop)}" content="([^"]*)"', html)
    return m.group(1) if m else None


def test_root_previews_city_neutral(client):
    # The bare domain is the one link that gets posted to 29 different city
    # subreddits, so it must not name a city.
    html = client.get("/").get_data(as_text=True)
    assert _meta(html, "og:title") == ("Bürgerwecker – nie wieder freie "
                                       "Bürgerbüro-Termine verpassen")
    assert "Leipzig" not in _meta(html, "og:title")
    assert "Leipzig" not in _meta(html, "og:description")
    assert _meta(html, "og:url") == "https://buergerwecker.de/"


def test_home_has_the_full_preview_set(client):
    html = client.get("/").get_data(as_text=True)
    assert _meta(html, "og:type") == "website"
    assert _meta(html, "og:site_name") == "Bürgerwecker"
    assert _meta(html, "og:image") == "https://buergerwecker.de/og-image.png"
    assert _meta(html, "og:image:width") == "1200"
    assert _meta(html, "og:image:height") == "630"
    assert _meta(html, "og:locale") == "de_DE"
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert '<meta name="description" content="Wir gucken' in html


def test_city_page_previews_that_city(client):
    html = client.get("/bonn").get_data(as_text=True)
    assert "Bonn" in _meta(html, "og:title")
    assert _meta(html, "og:url") == "https://buergerwecker.de/bonn"


def test_title_and_description_name_the_city(client):
    html = client.get("/nuernberg").get_data(as_text=True)
    assert _meta(html, "og:title") == "Nürnberg: freie Termine beim Amt – Bürgerwecker"
    assert "Nürnberg" in _meta(html, "og:description")
    assert "<title>Nürnberg: freie Termine beim Amt – Bürgerwecker</title>" in html
    assert _meta(html, "og:url") == "https://buergerwecker.de/nuernberg"


def test_english_pages_preview_in_english(client):
    html = client.get("/leipzig?lang=en").get_data(as_text=True)
    assert _meta(html, "og:locale") == "en_GB"
    assert _meta(html, "og:title").startswith("Leipzig: free appointment slots")


def test_amt_of_a_special_category_tenant_stays_off_the_card(client):
    # The office name is the sensitive fact for these tenants, and a preview
    # card ends up in whatever chat the link was pasted into.
    html = client.get("/muenster-standesamt").get_data(as_text=True)
    assert _meta(html, "og:title") == "Münster: freie Termine beim Amt – Bürgerwecker"
    assert "Standesamt" not in _meta(html, "og:title")
    assert "Standesamt" not in _meta(html, "og:description")


def test_token_paths_never_put_their_token_in_og_url(client):
    html = client.get("/manage/not-a-real-token").get_data(as_text=True)
    assert _meta(html, "og:url") == "https://buergerwecker.de/"


def _link(html: str, rel: str, hreflang: str | None = None) -> str | None:
    attr = f' hreflang="{hreflang}"' if hreflang else ""
    m = re.search(rf'<link rel="{rel}"{attr} href="([^"]*)"', html)
    return m.group(1) if m else None


def test_pages_canonicalise_to_themselves_per_language(client):
    de = client.get("/nuernberg").get_data(as_text=True)
    assert _link(de, "canonical") == "https://buergerwecker.de/nuernberg"
    assert _link(de, "alternate", "en") == "https://buergerwecker.de/nuernberg?lang=en"
    # The English page must NOT canonicalise to the German one, or Google
    # folds the two and the English version stops being indexed.
    en = client.get("/nuernberg?lang=en").get_data(as_text=True)
    assert _link(en, "canonical") == "https://buergerwecker.de/nuernberg?lang=en"
    assert _link(en, "alternate", "x-default") == "https://buergerwecker.de/nuernberg"


def test_token_and_admin_pages_are_noindex(client):
    for path in ("/manage/not-a-real-token", "/admin?token=" + "a" * 32):
        html = client.get(path).get_data(as_text=True)
        assert '<meta name="robots" content="noindex, nofollow">' in html, path
        assert '<link rel="canonical"' not in html, path


def test_public_pages_are_not_noindex(client):
    for path in ("/", "/leipzig", "/impressum", "/datenschutz"):
        html = client.get(path).get_data(as_text=True)
        assert "noindex" not in html, path


def test_sitemap_lists_every_tenant_in_both_languages(client):
    from app.catalog import available_cities
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.mimetype == "application/xml"
    body = r.get_data(as_text=True)
    assert "<loc>https://buergerwecker.de/</loc>" in body
    assert "/manage/" not in body and "/admin" not in body
    for slug in available_cities():
        assert f"<loc>https://buergerwecker.de/{slug}</loc>" in body, slug
        assert f"<loc>https://buergerwecker.de/{slug}?lang=en</loc>" in body, slug


def test_every_sitemap_url_actually_resolves(client):
    """A sitemap that lists 404s is worse than no sitemap."""
    body = client.get("/sitemap.xml").get_data(as_text=True)
    locs = re.findall(r"<loc>https://buergerwecker\.de(.*?)</loc>", body)
    assert len(locs) > 50
    for path in locs:
        assert client.get(path.replace("&amp;", "&")).status_code == 200, path


def test_no_tenant_slug_shadows_a_real_route(client):
    """A city called 'kontakt' would be unreachable: Werkzeug matches the
    static rule first, so the tenant would silently never render."""
    from app.catalog import available_cities
    reserved = {"admin", "impressum", "datenschutz", "kontakt", "subscribe",
                "healthz", "sitemap.xml", "og-image.png", "confirm",
                "unsubscribe", "manage", "renew", "go", "static"}
    assert reserved.isdisjoint(set(available_cities()))


def test_og_image_is_served(client):
    r = client.get("/og-image.png")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/png"
    assert len(r.data) > 10_000
