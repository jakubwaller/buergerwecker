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
