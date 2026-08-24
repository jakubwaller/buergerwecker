"""Special-category services (Art. 9 GDPR): the extra explicit consent, and
everything that hangs off it.

Two Ämter forced this: a Gesundheitsamt whose only Anliegen is STI counselling
(health data) and a Standesamt whose SBGG declarations are data about gender
identity. Selecting one of those is itself the sensitive fact, so the sign-up
needs consent under Art. 9(2)(a) on top of the double opt-in, the subscription
expires sooner, and nothing we mail out may name the Anliegen or the Amt.
"""

import json
import os
from datetime import datetime, time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import catalog as catalog_mod
from app.db import connect, init_schema
from app.models import Filter, Slot, Subscription
from app.repo import confirm, insert_pending
from app.tokens import sign
from app.web import create_app

SENSITIVE_CITY = "beispielstadt-gesundheitsamt"
SENSITIVE_SVC = "9001"
ORDINARY_SVC = "9002"
LEIPZIG_SVC = "29cd0a26-fe7a-4d65-88cd-1e05fd749c71"


@pytest.fixture
def catalog_root(tmp_path, monkeypatch):
    """A catalog root holding an ordinary tenant plus one that offers a
    special-category Anliegen. Synthetic on purpose: the shipped Münster
    Standesamt still *excludes* its two sensitive Anliegen pending the
    coordination with the city's data-protection officer, so no live tenant
    exercises this path yet."""
    root = tmp_path / "catalog"
    src = catalog_mod.CATALOG_ROOT / "leipzig"
    ordinary = root / "leipzig"
    ordinary.mkdir(parents=True)
    for name in ("appointment_type.json", "locations.json", "scraper_config.json"):
        (ordinary / name).write_text((src / name).read_text(encoding="utf-8"),
                                     encoding="utf-8")
    sens = root / SENSITIVE_CITY
    sens.mkdir()
    (sens / "scraper_config.json").write_text(json.dumps({
        "vendor": "tevis", "base_url": "https://termine.example.com",
        "md": 23, "mdt": 212, "sensitive_services": [SENSITIVE_SVC],
    }), encoding="utf-8")
    (sens / "appointment_type.json").write_text(json.dumps({
        "Beratung zu sexuell übertragbaren Infektionen": SENSITIVE_SVC,
        "Allgemeine Beratung": ORDINARY_SVC,
    }), encoding="utf-8")
    (sens / "locations.json").write_text(json.dumps({
        "Gesundheitsamt, Musterweg 3": "240",
    }), encoding="utf-8")
    (sens / "display.json").write_text(json.dumps({
        "city_name": {"de": "Beispielstadt", "en": "Beispielstadt"},
        "office": {"de": "Gesundheitsamt", "en": "Public health office"},
    }), encoding="utf-8")
    monkeypatch.setattr(catalog_mod, "CATALOG_ROOT", root)
    catalog_mod.load_catalog.cache_clear()
    yield root
    catalog_mod.load_catalog.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch, catalog_root):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x" * 32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    # Left unset on purpose: the 30-day special-category retention is a
    # documented default, so existing deploys inherit it without a new env var.
    monkeypatch.delenv("SENSITIVE_SUBSCRIPTION_TTL_DAYS", raising=False)
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "50")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "50")
    monkeypatch.setenv("MAILJET_API_KEY", "mj")
    monkeypatch.setenv("MAILJET_API_SECRET", "mj")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "x@x")
    monkeypatch.setenv("MAILJET_FROM_NAME", "x")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS", "24")
    monkeypatch.setenv("RATE_LIMIT_MINUTES", "15")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE", "10")
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS", "2")
    monkeypatch.setenv("DEVELOPER_EMAIL", "dev@example.com")
    monkeypatch.setenv("KOFI_URL", "https://k")
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _post(client, data, ip):
    """GLOBAL_IP_LIMITER is a module-level singleton whose counts outlive the
    test, so every /subscribe here comes from its own address — sharing
    127.0.0.1 would 429 the sign-up tests in a full-suite run."""
    return client.post("/subscribe", data=data,
                       headers={"X-Forwarded-For": ip})


def _form(service, *, city=SENSITIVE_CITY, email="alice@example.com", **extra):
    data = {"lang": "de", "city": city, "email": email,
            "appointment_type": service, "all_locations": "1",
            "time_start": "00:00", "time_end": "23:59",
            "weekdays": ["1", "2", "3", "4", "5"], "website": ""}
    data.update(extra)
    return data


def _row(email):
    conn = connect(os.environ["DB_PATH"])
    return conn.execute(
        "SELECT * FROM subscriptions WHERE email=?", (email,)).fetchone()


def _days_until(expires_at: str) -> int:
    return round((datetime.fromisoformat(expires_at) - datetime.utcnow())
                 .total_seconds() / 86400)


# ---------- the form ----------

def test_ordinary_tenant_form_has_no_consent_box(client):
    """The extra box appears only where it is needed — asking every subscriber
    to consent to Art. 9 processing that never happens would be noise."""
    body = client.get("/leipzig").data.decode()
    assert "consent_special" not in body


def test_sensitive_tenant_form_asks_for_explicit_consent(client):
    body = client.get(f"/{SENSITIVE_CITY}").data.decode()
    assert 'name="consent_special"' in body
    assert "Art. 9" in body
    # Explicit consent means opt-in: never pre-ticked.
    assert 'name="consent_special" value="1" checked' not in body
    # The script that hides the box again for ordinary Anliegen needs to know
    # which uuids are sensitive.
    assert f'"{SENSITIVE_SVC}"' in body


def test_english_form_offers_the_consent_in_english(client):
    body = client.get(f"/{SENSITIVE_CITY}?lang=en").data.decode()
    assert "I explicitly consent" in body
    assert "Art. 9(2)(a) GDPR" in body


# ---------- /subscribe ----------

def test_subscribe_to_sensitive_service_without_consent_is_rejected(client):
    """The box is hidden by script while an ordinary Anliegen is selected, and
    a POST need never have rendered the page at all — so the gate is here."""
    with patch("app.web._send_confirmation_email", return_value=True) as send:
        r = _post(client, _form(SENSITIVE_SVC, email="nc@example.com"),
                  "198.51.100.1")
    assert r.status_code == 400
    assert "Einwilligung" in r.data.decode()
    assert _row("nc@example.com") is None
    send.assert_not_called()


def test_subscribe_with_consent_records_it_and_shortens_retention(client):
    with patch("app.web._send_confirmation_email", return_value=True):
        r = _post(client, _form(SENSITIVE_SVC, email="yes@example.com",
                                consent_special="1"), "198.51.100.2")
    assert r.status_code == 302
    row = _row("yes@example.com")
    assert row["consent_special_at"] is not None   # Art. 7(1) record of consent
    assert _days_until(row["expires_at"]) == 30    # not the usual 90


def test_ordinary_service_never_records_a_special_consent(client):
    """A stray checkbox on an ordinary Anliegen must not stamp a consent that
    was never needed, nor cut that subscriber's retention short."""
    with patch("app.web._send_confirmation_email", return_value=True):
        r = _post(client, _form(ORDINARY_SVC, email="ord@example.com",
                                consent_special="1"), "198.51.100.3")
    assert r.status_code == 302
    row = _row("ord@example.com")
    assert row["consent_special_at"] is None
    assert _days_until(row["expires_at"]) == 90


# ---------- /manage ----------

def _subscribe(service, *, city=SENSITIVE_CITY, email="m@example.com",
               consent=False):
    conn = connect(os.environ["DB_PATH"])
    f = Filter(appointment_types=[service], locations="all",
               weekdays=[1, 2, 3, 4, 5],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    sid = insert_pending(conn, email=email, city=city, language="de",
                         filter_=f, ttl_days=30 if consent else 90,
                         consent_special=consent)
    confirm(conn, sid)
    return sid, sign(sid, "manage", primary="x" * 32, previous="")


def _manage_post(client, token, service, **extra):
    data = {"appointment_type": service, "all_locations": "1",
            "time_start": "00:00", "time_end": "23:59",
            "weekdays": ["1", "2", "3"]}
    data.update(extra)
    return client.post(f"/manage/{token}", data=data)


def test_manage_switch_to_sensitive_service_requires_consent(client):
    """Editing a filter is a second way to select a sensitive Anliegen — it
    carries the same gate, or it would be a way around the sign-up form."""
    sid, token = _subscribe(ORDINARY_SVC, email="sw@example.com")
    r = _manage_post(client, token, SENSITIVE_SVC)
    assert r.status_code == 400
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT filters_json, consent_special_at FROM "
                       "subscriptions WHERE id=?", (sid,)).fetchone()
    assert SENSITIVE_SVC not in row["filters_json"]
    assert row["consent_special_at"] is None


def test_manage_switch_with_consent_stamps_it_and_pulls_expiry_in(client):
    sid, token = _subscribe(ORDINARY_SVC, email="sw2@example.com")
    r = _manage_post(client, token, SENSITIVE_SVC, consent_special="1")
    assert r.status_code == 200
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT expires_at, consent_special_at FROM "
                       "subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["consent_special_at"] is not None
    assert _days_until(row["expires_at"]) == 30


def test_manage_switch_away_from_sensitive_clears_the_consent(client):
    """The consent covered one selection. Keeping the stamp afterwards would
    overstate what the subscriber agreed to — and would keep them on the
    shortened retention for no reason."""
    sid, token = _subscribe(SENSITIVE_SVC, email="sw3@example.com",
                            consent=True)
    r = _manage_post(client, token, ORDINARY_SVC)
    assert r.status_code == 200
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT consent_special_at FROM subscriptions "
                       "WHERE id=?", (sid,)).fetchone()
    assert row["consent_special_at"] is None


def test_manage_page_prefills_an_existing_consent(client):
    _sid, token = _subscribe(SENSITIVE_SVC, email="pre@example.com",
                             consent=True)
    body = client.get(f"/manage/{token}").data.decode()
    assert 'name="consent_special" value="1" checked' in body


# ---------- renewal ----------

def test_renewal_keeps_the_short_term_for_a_sensitive_subscription(client):
    """Otherwise the renewal link would quietly promote a 30-day
    special-category subscription to the ordinary 90 days."""
    sid, _tok = _subscribe(SENSITIVE_SVC, email="ren@example.com",
                           consent=True)
    r = client.get(f"/renew/{sign(sid, 'renew', primary='x' * 32, previous='')}")
    assert r.status_code == 200
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT expires_at FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    assert _days_until(row["expires_at"]) == 30


def test_renewal_still_gives_ordinary_subscriptions_the_full_term(client):
    sid, _tok = _subscribe(ORDINARY_SVC, email="ren2@example.com")
    client.get(f"/renew/{sign(sid, 'renew', primary='x' * 32, previous='')}")
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT expires_at FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    assert _days_until(row["expires_at"]) == 90


# ---------- what goes out by email ----------

def _sub_for(service, sid=1):
    return Subscription(
        id=sid, email="a@example.com", city=SENSITIVE_CITY, language="de",
        sub_filter=Filter(appointment_types=[service], locations="all",
                          weekdays=[1, 2, 3, 4, 5, 6, 7],
                          time_window_start=time(0, 0),
                          time_window_end=time(23, 59)),
        created_at=datetime(2026, 5, 1), confirmed_at=datetime(2026, 5, 1),
        last_notified_at=None, expires_at=datetime(2026, 8, 1),
        reminder_sent_at=None, heartbeat_30d_at=None, heartbeat_60d_at=None,
        deleted_at=None)


def _queue_digest(sub):
    """Render a digest through send_digest's own staging path (sink=[] stops
    it before delivery) so the booking link is the real one."""
    from app.digest import send_digest
    cfg = SimpleNamespace(token_secret_primary="x" * 32,
                          token_secret_previous="",
                          public_base_url="https://x",
                          kofi_url="https://k")
    sink = []
    send_digest(conn=None, subscription=sub,
                matched_slots=[Slot("2026-06-10", "10:30", "240", sub.sub_filter.appointment_types[0], "t")],
                cycle_id="c1", cfg=cfg, sink=sink)
    return sink[0].item


def test_digest_names_neither_the_sensitive_service_nor_the_office(client):
    """The mail sits in an inbox. Date, time and the booking link are all it
    may carry — the Anliegen and the Amt are the sensitive facts themselves."""
    body = _queue_digest(_sub_for(SENSITIVE_SVC)).body
    assert "sexuell übertragbaren" not in body
    assert "Gesundheitsamt" not in body
    assert "Musterweg" not in body
    assert "aus Datenschutzgründen nicht genannt" in body
    assert "10:30" in body            # the useful part still gets through


def test_digest_booking_link_does_not_spell_out_the_tenant(client):
    """/go/<city> would put `…-gesundheitsamt` in a URL in the mail body,
    undoing the redaction one line above it."""
    body = _queue_digest(_sub_for(SENSITIVE_SVC)).body
    assert SENSITIVE_CITY not in body
    assert "https://x/go/sub/" in body


def test_ordinary_digest_is_unchanged(client):
    body = _queue_digest(_sub_for(ORDINARY_SVC)).body
    assert "Allgemeine Beratung" in body
    assert "Gesundheitsamt, Musterweg 3" in body
    assert f"https://x/go/{SENSITIVE_CITY}" in body


def test_go_sub_link_resolves_the_tenant_without_naming_it(client):
    sid, _tok = _subscribe(SENSITIVE_SVC, email="go@example.com", consent=True)
    tok = sign(sid, "goto", primary="x" * 32, previous="")
    r = client.get(f"/go/sub/{tok}")
    assert r.status_code == 302
    assert r.headers["Location"] == "https://termine.example.com/select2?md=23"


def test_go_sub_rejects_a_token_minted_for_another_purpose(client):
    sid, _tok = _subscribe(SENSITIVE_SVC, email="go2@example.com",
                           consent=True)
    tok = sign(sid, "unsubscribe", primary="x" * 32, previous="")
    assert client.get(f"/go/sub/{tok}").status_code == 400


def test_go_sub_link_dies_with_the_subscription(client):
    sid, _tok = _subscribe(SENSITIVE_SVC, email="go3@example.com",
                           consent=True)
    connect(os.environ["DB_PATH"]).execute(
        "UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
        (sid,))
    tok = sign(sid, "goto", primary="x" * 32, previous="")
    assert client.get(f"/go/sub/{tok}").status_code == 404


# ---------- referrer ----------

@pytest.mark.parametrize("path", ["/", f"/{SENSITIVE_CITY}",
                                  "/datenschutz", "/impressum"])
def test_pages_send_no_referrer(client, path):
    """The URL of a sign-up page names the Amt; without this every outbound
    click hands that URL to the destination."""
    assert client.get(path).headers["Referrer-Policy"] == "no-referrer"
