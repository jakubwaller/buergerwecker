import pytest
from app.web import create_app
from app.db import connect, init_schema
from app.repo import insert_pending
from app.models import Filter
from datetime import time
import os

@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    for k, v in {
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
        "ADMIN_TOKEN":"a"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "RENEWAL_REMINDER_DAYS_BEFORE":"10","MAX_PLANS_PER_CITY":"10",
        "PARSER_CANARY_THRESHOLD_HOURS":"2","DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path); init_schema(conn)
    f = Filter(appointment_types=["A"], locations="all", weekdays=[1,2,3,4,5,6,7],
               time_window_start=time(0,0), time_window_end=time(23,59))
    sid = insert_pending(conn, email="a@x.com", city="leipzig", language="de",
                         filter_=f, ttl_days=90)
    app = create_app(); app.config["TESTING"]=True
    return app.test_client(), sid

def _sign(sid, purpose):
    from app.tokens import sign
    return sign(sid, purpose, primary="x"*32, previous="")

def test_confirm_marks_subscription_confirmed(client):
    from unittest.mock import patch
    c, sid = client
    tok = _sign(sid, "confirm")
    with patch("app.web._send_manage_link_email"):
        r = c.get(f"/confirm/{tok}")
    assert r.status_code in (200, 302)
    with patch("app.web._send_manage_link_email"):
        r2 = c.get(f"/confirm/{tok}")
    assert r2.status_code in (200, 302)

def _confirmed_at(sid):
    conn = connect(os.environ["DB_PATH"])
    return conn.execute("SELECT confirmed_at FROM subscriptions WHERE id=?",
                        (sid,)).fetchone()[0]


def test_confirm_link_on_an_unsubscribed_signup_is_not_found(client):
    from unittest.mock import patch
    c, sid = client
    conn = connect(os.environ["DB_PATH"])
    conn.execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
                 (sid,))
    with patch("app.web._send_manage_link_email") as ms:
        r = c.get(f"/confirm/{_sign(sid, 'confirm')}")
    assert r.status_code == 404
    assert "existiert nicht mehr" in r.get_data(as_text=True)
    assert _confirmed_at(sid) is None
    ms.assert_not_called()


@pytest.mark.parametrize("lang, expect_text, expect_href", [
    ("de", "Anmeldung ist abgelaufen", 'href="/leipzig"'),
    ("en", "sign-up has expired", 'href="/leipzig?lang=en"'),
])
def test_confirm_link_after_the_term_ran_out_says_sign_up_again(
        client, lang, expect_text, expect_href):
    """Confirm tokens never expire, and an unconfirmed row outlives its term
    for EXPIRED_GRACE_DAYS before the soft-delete. Confirming it then would
    show "läuft bis <past date>" and mail a promise the poller never keeps."""
    from unittest.mock import patch
    c, sid = client
    conn = connect(os.environ["DB_PATH"])
    conn.execute("UPDATE subscriptions SET language=?, "
                 "expires_at=datetime('now','-1 day') WHERE id=?", (lang, sid))
    with patch("app.web._send_manage_link_email") as ms:
        r = c.get(f"/confirm/{_sign(sid, 'confirm')}")
    assert r.status_code == 410
    html = r.get_data(as_text=True)
    assert expect_text in html
    assert expect_href in html
    assert _confirmed_at(sid) is None
    ms.assert_not_called()


def test_confirm_survives_manage_link_email_failure(client):
    """A failure sending the (secondary) management-link email must NOT turn a
    successful confirmation into a 500. The subscription is already confirmed;
    the manage-link email is a convenience. Regression test for the production
    'Internal Server Error' on /confirm."""
    from unittest.mock import patch
    c, sid = client
    tok = _sign(sid, "confirm")
    with patch("app.web._send_manage_link_email",
               side_effect=RuntimeError("mail provider exploded")):
        r = c.get(f"/confirm/{tok}")
    assert r.status_code == 200, r.data[:300]
    # The subscription must actually be confirmed despite the email failure.
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT confirmed_at FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    assert row["confirmed_at"] is not None
    # And the page must show a human-readable confirmation (sub is German).
    assert b"best\xc3\xa4tigt" in r.data.lower()  # "bestätigt"


def _expected_end_date(sid, lang="de"):
    from datetime import datetime
    from app.i18n import format_date
    conn = connect(os.environ["DB_PATH"])
    exp = conn.execute("SELECT expires_at FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()["expires_at"]
    return format_date(datetime.fromisoformat(exp[:19]).date(), lang)


def test_confirm_page_states_the_end_date(client):
    """The user ask of 2026-08-31: a term that ends silently reads as the
    service having died, so the end date is named at the start."""
    from unittest.mock import patch
    c, sid = client
    with patch("app.web._send_manage_link_email"):
        r = c.get(f"/confirm/{_sign(sid, 'confirm')}")
    assert r.status_code == 200
    html = r.data.decode()
    assert _expected_end_date(sid) in html
    assert "läuft bis zum" in html


def test_manage_link_email_states_the_end_date(client):
    from unittest.mock import patch
    c, sid = client
    with patch("app.web.mail_send") as ms:
        r = c.get(f"/confirm/{_sign(sid, 'confirm')}")
    assert r.status_code == 200
    body = ms.call_args.args[3]
    assert ms.call_args.args[2].startswith("Anmeldung bestätigt")
    assert "deine Anmeldung ist aktiv" in body
    assert "Verwaltungs-Link" in body          # the FAQ calls it that
    assert f"läuft bis zum {_expected_end_date(sid)}" in body


def test_manage_page_states_the_end_date(client):
    c, sid = client
    r = c.get(f"/manage/{_sign(sid, 'manage')}")
    assert r.status_code == 200
    html = r.data.decode()
    assert f"läuft bis zum {_expected_end_date(sid)}" in html


def test_manage_page_says_expired_when_paused(client):
    """An expired-but-in-grace subscription must not claim to still run."""
    c, sid = client
    conn = connect(os.environ["DB_PATH"])
    conn.execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') "
                 "WHERE id=?", (sid,))
    r = c.get(f"/manage/{_sign(sid, 'manage')}")
    assert r.status_code == 200
    html = r.data.decode()
    assert "abgelaufen" in html
    assert "läuft bis zum" not in html


def test_faq_states_the_configured_term(client):
    """"Every couple of weeks" is exactly what confused a subscriber; the FAQ
    names the configured number of days instead."""
    c, _sid = client
    de = c.get("/").data.decode()
    assert "Wie lange läuft meine Anmeldung?" in de
    assert "90 Tage" in de
    en = c.get("/?lang=en").data.decode()
    assert "How long does my subscription run?" in en
    assert "90 days" in en


def test_unsubscribe_soft_deletes(client):
    from unittest.mock import patch
    c, sid = client
    _confirm_tok = _sign(sid, "confirm")
    with patch("app.web._send_manage_link_email"):
        c.get(f"/confirm/{_confirm_tok}")
    unsub = _sign(sid, "unsubscribe")
    r = c.get(f"/unsubscribe/{unsub}")
    assert r.status_code in (200, 302)
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT deleted_at FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["deleted_at"] is not None

def test_invalid_token_rejected(client):
    c, sid = client
    r = c.get("/confirm/garbage")
    assert r.status_code == 400

def test_manage_get_prefills_current_filter(tmp_path, monkeypatch):
    """The manage form must reflect the subscription's current filter,
    not the bare-template defaults."""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    for k, v in {
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
        "ADMIN_TOKEN":"a"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "RENEWAL_REMINDER_DAYS_BEFORE":"10","MAX_PLANS_PER_CITY":"10",
        "PARSER_CANARY_THRESHOLD_HOURS":"2","DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path); init_schema(conn)
    # Pick a real Leipzig appointment-type and location UUID from the catalog
    # so the template renders <option> rows that can match.
    from app.catalog import load_catalog
    cat = load_catalog("leipzig")
    appt_uuid = next(iter(cat.appointment_types.values()))
    loc_uuid_a, loc_uuid_b = list(cat.locations.values())[:2]
    f = Filter(appointment_types=[appt_uuid],
               locations=[loc_uuid_a, loc_uuid_b],
               weekdays=[2, 4],  # Tue + Thu only
               time_window_start=time(9, 30),
               time_window_end=time(17, 0),
               max_days_ahead=14)
    sid = insert_pending(conn, email="m@x.com", city="leipzig", language="de",
                         filter_=f, ttl_days=90)
    conn.execute("UPDATE subscriptions SET confirmed_at=datetime('now') WHERE id=?", (sid,))
    conn.commit()
    app = create_app(); app.config["TESTING"]=True
    c = app.test_client()
    tok = _sign(sid, "manage")
    r = c.get(f"/manage/{tok}")
    assert r.status_code == 200, r.data[:200]
    html = r.data.decode()
    # Appointment type: the saved option must be marked `selected`.
    assert f'value="{appt_uuid}" selected' in html, "appointment_type not preselected"
    # Locations: NOT "all", so the All checkbox must NOT be checked.
    assert 'name="all_locations" value="1" checked' not in html
    # The two selected location UUIDs must each be checked.
    assert f'value="{loc_uuid_a}" checked' in html
    assert f'value="{loc_uuid_b}" checked' in html
    # Weekdays: only Tue (2) and Thu (4) selected.
    assert 'name="weekdays" value="2" checked' in html
    assert 'name="weekdays" value="4" checked' in html
    assert 'name="weekdays" value="1" checked' not in html
    assert 'name="weekdays" value="3" checked' not in html
    # Time window values.
    assert 'value="09:30"' in html
    assert 'value="17:00"' in html
    # Max-days-ahead window preselected.
    assert 'value="14" selected' in html

def test_manage_get_prefills_all_locations(tmp_path, monkeypatch):
    """When `locations == 'all'`, the All-locations checkbox must be checked
    and individual location checkboxes must NOT be."""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    for k, v in {
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
        "ADMIN_TOKEN":"a"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "RENEWAL_REMINDER_DAYS_BEFORE":"10","MAX_PLANS_PER_CITY":"10",
        "PARSER_CANARY_THRESHOLD_HOURS":"2","DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path); init_schema(conn)
    from app.catalog import load_catalog
    cat = load_catalog("leipzig")
    appt_uuid = next(iter(cat.appointment_types.values()))
    f = Filter(appointment_types=[appt_uuid], locations="all",
               weekdays=[1,2,3,4,5,6,7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    sid = insert_pending(conn, email="m@x.com", city="leipzig", language="de",
                         filter_=f, ttl_days=90)
    conn.execute("UPDATE subscriptions SET confirmed_at=datetime('now') WHERE id=?", (sid,))
    conn.commit()
    app = create_app(); app.config["TESTING"]=True
    c = app.test_client()
    r = c.get(f"/manage/{_sign(sid, 'manage')}")
    assert r.status_code == 200
    html = r.data.decode()
    assert 'name="all_locations" value="1" checked' in html
    # No individual location should be checked.
    for loc_uuid in cat.locations.values():
        assert f'value="{loc_uuid}" checked' not in html

def test_one_click_unsubscribe_post(client):
    """Mail clients POST to the List-Unsubscribe URL (RFC 8058); the route must
    accept it and actually unsubscribe."""
    c, sid = client
    r = c.post(f"/unsubscribe/{_sign(sid, 'unsubscribe')}")
    assert r.status_code == 200
    import os
    from app.db import connect
    row = connect(os.environ["DB_PATH"]).execute(
        "SELECT deleted_at FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["deleted_at"] is not None

def test_filter_edit_clears_the_abundance_measurement(tmp_path, monkeypatch):
    """`last_match_count` was measured against the previous filter. Narrowing a
    firehose filter down to one scarce office must not leave the subscriber
    stuck on the slow cadence the old filter earned."""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "x"*32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    for k, v in {
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
        "ADMIN_TOKEN":"a"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "RENEWAL_REMINDER_DAYS_BEFORE":"10","MAX_PLANS_PER_CITY":"10",
        "PARSER_CANARY_THRESHOLD_HOURS":"2","DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path); init_schema(conn)
    from app.catalog import load_catalog
    cat = load_catalog("leipzig")
    appt_uuid = next(iter(cat.appointment_types.values()))
    loc_uuid = next(iter(cat.locations.values()))
    f = Filter(appointment_types=[appt_uuid], locations="all",
               weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    sid = insert_pending(conn, email="m@x.com", city="leipzig", language="de",
                         filter_=f, ttl_days=90)
    conn.execute("UPDATE subscriptions SET confirmed_at=datetime('now'), "
                 "last_match_count=90 WHERE id=?", (sid,))
    conn.commit()

    app = create_app(); app.config["TESTING"] = True
    r = app.test_client().post(f"/manage/{_sign(sid, 'manage')}", data={
        "appointment_type": appt_uuid, "locations": [loc_uuid],
        "weekdays": ["2"], "time_start": "09:00", "time_end": "10:00",
    })
    assert r.status_code == 200, r.data[:200]
    row = connect(db_path).execute(
        "SELECT last_match_count, filters_json FROM subscriptions WHERE id=?",
        (sid,)).fetchone()
    assert row["last_match_count"] is None
    assert loc_uuid in row["filters_json"]      # the edit really landed


# ---------- /renew: the "weiter" answer of the still-looking check-in ----------

def _confirmed(client):
    from unittest.mock import patch
    c, sid = client
    with patch("app.web._send_manage_link_email"):
        c.get(f"/confirm/{_sign(sid, 'confirm')}")
    from app.db import connect
    return c, sid, connect(os.environ["DB_PATH"])


def _days_left(conn, sid):
    return conn.execute("SELECT julianday(expires_at) - julianday('now') AS d "
                        "FROM subscriptions WHERE id=?", (sid,)).fetchone()["d"]


def test_renew_extends_and_rearms_the_checkin(client):
    """Without clearing reminder_sent_at a renewed subscription would never
    be asked again and would expire silently at the end of the next term."""
    c, sid, conn = _confirmed(client)
    conn.execute("UPDATE subscriptions SET expires_at=datetime('now','+2 days'), "
                 "reminder_sent_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
    r = c.get(f"/renew/{_sign(sid, 'renew')}")
    assert r.status_code == 200
    assert "Wir suchen weiter".encode() in r.data
    row = conn.execute("SELECT reminder_sent_at FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    assert row["reminder_sent_at"] is None
    assert 89 < _days_left(conn, sid) <= 90


def test_renew_revives_a_paused_subscription(client):
    """Expired but not yet deleted: the link from the check-in still works."""
    c, sid, conn = _confirmed(client)
    conn.execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') WHERE id=?",
                 (sid,))
    r = c.get(f"/renew/{_sign(sid, 'renew')}")
    assert r.status_code == 200
    assert _days_left(conn, sid) > 0


def test_renew_after_deletion_is_not_found(client):
    """A valid token over a deleted row must not show a "renewed" page over a
    0-row UPDATE."""
    c, sid, conn = _confirmed(client)
    conn.execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (sid,))
    r = c.get(f"/renew/{_sign(sid, 'renew')}")
    assert r.status_code == 404


def test_datenschutz_states_the_configured_terms(client):
    """The retention periods on the privacy page come from config, so a
    changed .env cannot leave the page promising the old term."""
    c, _sid = client        # fixture: TTL 90, sensitive default 30, grace default 14
    de = c.get("/datenschutz").data.decode()
    assert "laufen 90 Tage nach der Anmeldung" in de
    assert "nach 30 Tagen" in de
    assert "pausiert 14 Tage" in de
    en = c.get("/datenschutz?lang=en").data.decode()
    assert "expire automatically 90 days after sign-up" in en
    assert "paused for 14 days" in en


# ---------- /manage POST validates like /subscribe ----------

LEIPZIG_SERVICE = "29cd0a26-fe7a-4d65-88cd-1e05fd749c71"


def test_manage_post_rejects_a_service_the_catalog_does_not_offer(client):
    c, sid = client
    r = c.post(f"/manage/{_sign(sid, 'manage')}",
               data={"appointment_type": "junk", "all_locations": "1"})
    assert r.status_code == 400
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT filters_json FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    assert Filter.from_json(row["filters_json"]).appointment_types == ["A"]


def test_manage_post_rejects_a_malformed_time_window(client):
    c, sid = client
    r = c.post(f"/manage/{_sign(sid, 'manage')}",
               data={"appointment_type": LEIPZIG_SERVICE, "all_locations": "1",
                     "time_start": "25:00", "time_end": "23:59"})
    assert r.status_code == 400


def test_manage_post_updates_a_valid_filter(client):
    c, sid = client
    r = c.post(f"/manage/{_sign(sid, 'manage')}",
               data={"appointment_type": LEIPZIG_SERVICE, "all_locations": "1",
                     "time_start": "08:00", "time_end": "12:00"})
    assert r.status_code == 200
    conn = connect(os.environ["DB_PATH"])
    row = conn.execute("SELECT filters_json FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    f = Filter.from_json(row["filters_json"])
    assert f.appointment_types == [LEIPZIG_SERVICE]
    assert f.time_window_start == time(8, 0)


def test_manage_post_honours_the_plan_cap(client):
    """The manage form was a way past the wait-list: sign up for a plan
    already polled, then edit into any other."""
    from unittest.mock import patch
    c, sid = client
    conn = connect(os.environ["DB_PATH"])
    conn.execute("UPDATE subscriptions SET confirmed_at=datetime('now') WHERE id=?", (sid,))
    with patch("app.web.would_exceed_cap", return_value=True) as cap:
        r = c.post(f"/manage/{_sign(sid, 'manage')}",
                   data={"appointment_type": LEIPZIG_SERVICE, "all_locations": "1"})
    assert r.status_code == 503
    # Its own current plan is not counted against it.
    existing = cap.call_args.args[0]
    assert existing == []
    row = conn.execute("SELECT filters_json FROM subscriptions WHERE id=?",
                       (sid,)).fetchone()
    assert Filter.from_json(row["filters_json"]).appointment_types == ["A"]


def test_manage_post_for_a_deleted_subscription_is_not_found(client):
    c, sid = client
    conn = connect(os.environ["DB_PATH"])
    conn.execute("UPDATE subscriptions SET deleted_at=datetime('now') WHERE id=?", (sid,))
    r = c.post(f"/manage/{_sign(sid, 'manage')}",
               data={"appointment_type": LEIPZIG_SERVICE, "all_locations": "1"})
    assert r.status_code == 404
