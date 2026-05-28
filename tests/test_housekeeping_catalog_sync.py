from datetime import time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.housekeeping import run_once


def _env(monkeypatch, **overrides):
    base = {
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
        "TOKEN_SECRET_PRIMARY":"x"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"a"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "DEVELOPER_EMAIL":"dev@x","KOFI_URL":"https://k",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    conn = connect(db_path); init_schema(conn)
    return conn


def test_housekeeping_skips_catalog_sync_when_flag_off(db, monkeypatch):
    _env(monkeypatch)  # CATALOG_SYNC_ENABLED not set
    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city") as sync_city:
        run_once(db)
        sync_city.assert_not_called()


def test_housekeeping_runs_catalog_sync_when_flag_on(db, monkeypatch):
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1")
    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city") as sync_city:
        sync_city.return_value = {"service_drift": {}, "location_drift": {}}
        run_once(db)
        assert sync_city.called
        # Should be called once per city directory under catalog/; leipzig is the
        # only one shipped today.
        called_cities = {c.args[0] for c in sync_city.call_args_list}
        assert "leipzig" in called_cities


def test_housekeeping_alerts_developer_when_catalog_drift_detected(db, monkeypatch):
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1", DEVELOPER_EMAIL="dev@example.com")
    drift_result = {"service_drift": {"added": ["NewService"]}, "location_drift": {}}

    def fake_sync(city, http, alert_fn, catalog_root=None):
        alert_fn(city=city,
                 service_drift=drift_result["service_drift"],
                 location_drift=drift_result["location_drift"])
        return drift_result

    with patch("app.mail.send") as mail_send, \
         patch("app.catalog_sync.sync_city", side_effect=fake_sync):
        run_once(db)
        # mail.send must have been called at least once with the dev email
        # and a subject mentioning catalog drift.
        drift_calls = [c for c in mail_send.call_args_list
                       if "catalog" in (c.args[2] if len(c.args) > 2 else "").lower()
                       or "drift" in (c.args[2] if len(c.args) > 2 else "").lower()]
        assert drift_calls, f"no drift alert email sent; calls={mail_send.call_args_list}"


def test_housekeeping_swallows_catalog_sync_exception(db, monkeypatch):
    """A network error during catalog sync must NOT crash run_once."""
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1")
    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city", side_effect=RuntimeError("boom")):
        run_once(db)  # must not raise
