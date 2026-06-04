import pytest
from datetime import datetime
from app.web import create_app
from app.db import connect, init_schema
from app.admin import stats
import os

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path/"t.db"))
    for k,v in {
        "TOKEN_SECRET_PRIMARY":"x"*32,"TOKEN_SECRET_PREVIOUS":"",
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
        "ADMIN_TOKEN":"admin-tok","PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "RENEWAL_REMINDER_DAYS_BEFORE":"10","MAX_PLANS_PER_CITY":"10",
        "PARSER_CANARY_THRESHOLD_HOURS":"2","DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(str(tmp_path/"t.db")); init_schema(conn)
    app = create_app(); app.config["TESTING"]=True
    return app.test_client()

def test_admin_requires_token(client):
    r = client.get("/admin")
    assert r.status_code == 401

def test_admin_with_token(client):
    r = client.get("/admin?token=admin-tok")
    assert r.status_code == 200
    assert b"active_subscriptions" in r.data

def test_admin_wrong_token(client):
    r = client.get("/admin?token=nope")
    assert r.status_code == 401

def test_go_route_redirects_on_cache_hit(client):
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    conn.execute(
        "INSERT INTO slots_cache (slot_token, city, upstream_url) "
        "VALUES ('tok1', 'leipzig', 'https://example.eu/book/123')"
    )
    r = client.get("/go/tok1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] == "https://example.eu/book/123"

def test_go_route_returns_410_on_miss(client):
    r = client.get("/go/nonexistent-token", follow_redirects=False)
    assert r.status_code == 410

def test_go_link_from_email_resolves_for_datetime_token(client):
    """End-to-end: the booking link the digest email contains must resolve via /go,
    not 410. Smart-CJM slot tokens are URL-encoded datetimes (T%3a..%2b..); Flask
    decodes the /go path param on click, so slots_cache must be keyed so the decoded
    token matches what run_cycle stored."""
    from unittest.mock import patch, MagicMock
    from datetime import time
    from app.db import connect
    from app.models import Slot, Filter
    from app.repo import insert_pending, confirm
    from app.cycle import run_cycle
    conn = connect(os.environ["DB_PATH"])
    f = Filter(appointment_types=["29cd0a26-fe7a-4d65-88cd-1e05fd749c71"], locations="all",
               weekdays=[1, 2, 3, 4, 5, 6, 7], time_window_start=time(0, 0),
               time_window_end=time(23, 59))
    sid = insert_pending(conn, email="a@x.com", city="leipzig", language="de",
                         filter_=f, ttl_days=90)
    confirm(conn, sid)
    # booking_token as it appears in the upstream button / email link: URL-encoded datetime
    booking_token = "2026-06-18T17%3a20%3a00%2b02%3a00"
    slot = Slot("2026-06-18", "17:20", "loc-1",
                "29cd0a26-fe7a-4d65-88cd-1e05fd749c71", booking_token, "res-1")
    with patch("app.cycle.get_scraper") as gs, patch("app.cycle.send_digest"):
        sc = MagicMock(); sc.poll.return_value = [slot]; gs.return_value = sc
        run_cycle(conn, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    # The email link is /go/<booking_token>; the browser/Flask decodes the path on click.
    r = client.get(f"/go/{booking_token}", follow_redirects=False)
    assert r.status_code == 302
    assert "appointment_reserve" in r.headers["Location"]

def test_admin_renders_new_metrics(client):
    r = client.get("/admin?token=admin-tok")
    assert r.status_code == 200
    for key in (b"upstream_by_city", b"slots_cached",
                b"emails_sent_total", b"last_failure_alert_at"):
        assert key in r.data, f"missing admin metric: {key!r}"

def test_stats_includes_upstream_and_extra_metrics(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    today = datetime.utcnow().date().isoformat()
    conn.execute(
        "INSERT INTO city_state (city, polls_today, polls_total, requests_today, "
        "requests_total, counts_date, last_polled_at) "
        "VALUES ('leipzig', 5, 50, 12, 120, ?, ?)",
        (today, "2026-06-03T10:00:00"))
    conn.execute("INSERT INTO slots_cache (slot_token, city, upstream_url) "
                 "VALUES ('t', 'leipzig', 'u')")
    conn.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('k', 'mailjet')")
    conn.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES ('p', 'pending')")
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_failure_alert_at', '2026-06-01T00:00:00')")
    s = stats(conn)
    up = s["upstream_by_city"]["leipzig"]
    assert up == {"polls_today": 5, "polls_total": 50,
                  "requests_today": 12, "requests_total": 120}
    assert s["last_polled_at_by_city"]["leipzig"] == "2026-06-03T10:00:00"
    assert s["slots_cached"] == 1
    assert s["emails_sent_total"] == 1   # 'pending' excluded
    assert s["last_failure_alert_at"] == "2026-06-01T00:00:00"

def test_stats_today_counts_gated_by_stale_date(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    conn.execute(
        "INSERT INTO city_state (city, polls_today, polls_total, requests_today, "
        "requests_total, counts_date) VALUES ('leipzig', 99, 50, 99, 120, '2000-01-01')")
    up = stats(conn)["upstream_by_city"]["leipzig"]
    assert up["polls_today"] == 0 and up["requests_today"] == 0   # stale day -> 0
    assert up["polls_total"] == 50 and up["requests_total"] == 120  # totals intact
