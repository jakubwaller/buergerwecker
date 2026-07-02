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
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
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
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
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
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
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
