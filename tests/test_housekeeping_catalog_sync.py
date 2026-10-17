from datetime import time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.housekeeping import run_once, wait_for_catalog_sync


def _env(monkeypatch, **overrides):
    base = {
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
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
        wait_for_catalog_sync()
        assert sync_city.called
        # Should be called once per city directory under catalog/; leipzig is the
        # only one shipped today.
        called_cities = {c.args[0] for c in sync_city.call_args_list}
        assert "leipzig" in called_cities


def test_housekeeping_does_not_mail_developer_on_catalog_drift(db, monkeypatch):
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1", DEVELOPER_EMAIL="dev@example.com")
    drift_result = {"service_drift": {"added": ["NewService"]}, "location_drift": {}}

    def fake_sync(city, http, alert_fn, catalog_root=None, write=True):
        alert_fn(city=city,
                 service_drift=drift_result["service_drift"],
                 location_drift=drift_result["location_drift"])
        return drift_result

    with patch("app.mail.send") as mail_send, \
         patch("app.catalog_sync.sync_city", side_effect=fake_sync):
        run_once(db)
        wait_for_catalog_sync()
        drift_calls = [c for c in mail_send.call_args_list
                       if "drift" in (c.args[2] if len(c.args) > 2 else "").lower()]
        assert not drift_calls, f"drift mail sent; calls={mail_send.call_args_list}"


def test_housekeeping_swallows_catalog_sync_exception(db, monkeypatch):
    """A network error during catalog sync must NOT crash run_once."""
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1")
    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city", side_effect=RuntimeError("boom")):
        run_once(db)  # must not raise
        wait_for_catalog_sync()


def test_housekeeping_does_not_wait_for_the_catalog_sync(db, monkeypatch):
    """The sync walks every tenant's live portal and takes minutes; run_once
    is called inline from the poll loop, so it must hand the sync off and
    return, or polling stops for the duration."""
    import threading
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1")
    release = threading.Event()
    started = threading.Event()

    def slow_sync(city, http, alert_fn, catalog_root=None, write=True):
        started.set()
        release.wait(5)
        return {"service_drift": {}, "location_drift": {}}

    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city", side_effect=slow_sync):
        run_once(db)
        assert started.wait(5)
        # run_once has returned while the first city is still syncing.
        release.set()
        wait_for_catalog_sync(5)


def test_sync_records_subscribed_ids_the_site_no_longer_has(db, monkeypatch):
    """Five TEVIS cities renumbered on 2026-10-10; their subscribers were
    polled for dead ids for three days and nothing said so. The daily sync
    records such ids for the ops summary, and forgets them once fixed."""
    import json
    from app.models import Filter
    from app.repo import insert_pending, confirm
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1")
    sid = insert_pending(db, email="a@example.com", city="leipzig", language="de",
                         filter_=Filter(["old-svc"], ["old-loc", "kept-loc"], [1],
                                        time(0, 0), time(23, 59)),
                         ttl_days=30)
    confirm(db, sid)

    def live_site(live):
        def fake(city, http, alert_fn, catalog_root=None, write=True):
            assert write is False      # production never rewrites catalog/
            return {"service_drift": {}, "location_drift": {},
                    "live_ids": live if city == "leipzig" else set()}
        return fake

    def dead():
        row = db.execute("SELECT value FROM meta WHERE key='catalog_dead_ids'").fetchone()
        return json.loads(row["value"])

    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city", side_effect=live_site({"new-svc", "kept-loc"})):
        run_once(db)
        wait_for_catalog_sync()
    assert dead() == {"leipzig": ["old-loc", "old-svc"]}

    with patch("app.mail.send"), \
         patch("app.catalog_sync.sync_city",
               side_effect=live_site({"old-svc", "old-loc", "kept-loc"})):
        run_once(db)
        wait_for_catalog_sync()
    assert dead() == {}


def test_sync_checks_the_catalog_offices_for_an_all_offices_subscription(db, monkeypatch):
    """A subscription to every office polls the catalog's offices, so a
    renumbered office is dead for it too."""
    import json
    from app.catalog import load_catalog
    from app.models import Filter
    from app.repo import insert_pending, confirm
    _env(monkeypatch, CATALOG_SYNC_ENABLED="1")
    confirm(db, insert_pending(db, email="a@example.com", city="leipzig",
                               language="de",
                               filter_=Filter(["svc"], "all", [1],
                                              time(0, 0), time(23, 59)),
                               ttl_days=30))
    offices = sorted(load_catalog("leipzig").locations.values())

    def fake(city, http, alert_fn, catalog_root=None, write=True):
        return {"service_drift": {}, "location_drift": {},
                "live_ids": {"svc", *offices[1:]} if city == "leipzig" else set()}

    with patch("app.mail.send"), patch("app.catalog_sync.sync_city", side_effect=fake):
        run_once(db)
        wait_for_catalog_sync()
    row = db.execute("SELECT value FROM meta WHERE key='catalog_dead_ids'").fetchone()
    assert json.loads(row["value"]) == {"leipzig": offices[:1]}
