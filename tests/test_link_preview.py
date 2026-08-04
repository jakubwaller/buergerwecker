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
    monkeypatch.setenv("RESEND_API_KEY", "re")
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


def test_title_and_description_name_the_city(client):
    html = client.get("/?city=nuernberg").get_data(as_text=True)
    assert _meta(html, "og:title") == "Bürgerwecker – freie Termine in Nürnberg"
    assert "Nürnberg" in _meta(html, "og:description")
    assert "<title>Bürgerwecker – freie Termine in Nürnberg</title>" in html
    assert _meta(html, "og:url") == "https://buergerwecker.de/?city=nuernberg"


def test_english_pages_preview_in_english(client):
    html = client.get("/?lang=en").get_data(as_text=True)
    assert _meta(html, "og:locale") == "en_GB"
    assert _meta(html, "og:title").startswith("Bürgerwecker – free appointment slots")


def test_amt_of_a_special_category_tenant_stays_off_the_card(client):
    # The office name is the sensitive fact for these tenants, and a preview
    # card ends up in whatever chat the link was pasted into.
    html = client.get("/?city=muenster-standesamt").get_data(as_text=True)
    assert _meta(html, "og:title") == "Bürgerwecker – freie Termine in Münster"
    assert "Standesamt" not in _meta(html, "og:title")
    assert "Standesamt" not in _meta(html, "og:description")


def test_token_paths_never_put_their_token_in_og_url(client):
    html = client.get("/manage/not-a-real-token").get_data(as_text=True)
    assert _meta(html, "og:url") == "https://buergerwecker.de/"


def test_og_image_is_served(client):
    r = client.get("/og-image.png")
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/png"
    assert len(r.data) > 10_000
