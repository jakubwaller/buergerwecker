import pytest
from datetime import datetime
from app.web import create_app
from app.db import connect, init_schema
from app.admin import stats, summary_anomalies, render_summary_email
import os

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path/"t.db"))
    for k,v in {
        "TOKEN_SECRET_PRIMARY":"x"*32,"TOKEN_SECRET_PREVIOUS":"",
        "SUBSCRIPTION_TTL_DAYS":"90","SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
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
    assert b"Active subscriptions" in r.data

def test_admin_wrong_token(client):
    r = client.get("/admin?token=nope")
    assert r.status_code == 401

def test_go_route_redirects_on_cache_hit(client):
    """Per-slot tokens from OLD emails (colon-prefixed) keep resolving from
    slots_cache until housekeeping prunes the rows."""
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    conn.execute(
        "INSERT INTO slots_cache (slot_token, city, upstream_url) "
        "VALUES ('leipzig:tok1', 'leipzig', 'https://example.eu/book/123')"
    )
    r = client.get("/go/leipzig:tok1", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] == "https://example.eu/book/123"

def test_go_route_returns_410_on_miss(client):
    r = client.get("/go/leipzig:nonexistent-token", follow_redirects=False)
    assert r.status_code == 410

def test_go_city_link_redirects_to_booking_start(client):
    """The digest's one booking link, /go/<city>, resolves from the catalog at
    click time — no cache row, never expires."""
    r = client.get("/go/leipzig", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert loc.startswith("https://terminvereinbarung.leipzig.de/m/leipzig-ba/")
    assert "uid=" in loc and "lang=de" in loc
    assert "appointment_reserve" not in loc   # upstream ignores it; never emit it
    r_en = client.get("/go/leipzig?lang=en", follow_redirects=False)
    assert "lang=en" in r_en.headers["Location"]

def test_go_city_link_unknown_city_410(client):
    r = client.get("/go/atlantis", follow_redirects=False)
    assert r.status_code == 410

def test_run_cycle_writes_no_slot_cache_and_digest_links_city_page(client):
    """Since per-slot deep links can't work upstream (session-bound booking),
    run_cycle stages digests that link /go/<city> and leaves slots_cache empty."""
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
    booking_token = "2026-06-18T17%3a20%3a00%2b02%3a00"
    slot = Slot("2026-06-18", "17:20", "loc-1",
                "29cd0a26-fe7a-4d65-88cd-1e05fd749c71", booking_token, "res-1")
    with patch("app.cycle.get_scraper") as gs, \
         patch("app.mail._call_mailjet_batch", return_value=200) as mb, \
         patch("app.mail._call_brevo_batch", return_value=201):
        sc = MagicMock(); sc.poll.return_value = [slot]; gs.return_value = sc
        run_cycle(conn, max_plans_per_city=10, rate_limit_minutes=15, cycle_id="c1")
    cached = conn.execute("SELECT COUNT(*) AS n FROM slots_cache").fetchone()["n"]
    assert cached == 0
    body = mb.call_args_list[0].args[0][0].body
    assert "https://x/go/leipzig" in body
    assert booking_token not in body

def test_admin_renders_new_metrics(client):
    r = client.get("/admin?token=admin-tok")
    assert r.status_code == 200
    # Always-present labels (Overview + System sections render regardless of data).
    for label in (b"Slots cached", b"Emails sent", b"Failure alert", b"Last backup",
                  # People, not subscription rows — the two counts differ.
                  b"Distinct subscribers",
                  # The gating window, next to the UTC-day counter it disagrees with.
                  b"rolling 24h", b"combined"):
        assert label in r.data, f"missing admin metric: {label!r}"

def test_admin_renders_notifications_section(client):
    r = client.get("/admin?token=admin-tok")
    assert r.status_code == 200
    # Always-present labels (Notifications section renders regardless of data).
    for label in (b"Notifications", b"Subscribers notified",
                  b"Awaiting first match", b"Delivery"):
        assert label in r.data, f"missing admin metric: {label!r}"


def test_admin_renders_city_panel_with_data(client):
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    today = datetime.utcnow().date().isoformat()
    conn.execute(
        "INSERT INTO city_state (city, polls_today, polls_total, requests_today, "
        "requests_total, counts_date, last_polled_at) "
        "VALUES ('leipzig', 5, 50, 12, 120, ?, ?)",
        (today, "2026-06-04T10:00:00"))
    r = client.get("/admin?token=admin-tok")
    assert r.status_code == 200
    assert b"Leipzig" in r.data          # capitalized city name
    assert b"Polls" in r.data            # per-city panel rendered
    assert b"Matching slots" in r.data   # canary clear (no zero_match_since row)

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
    # Durable counters carry history that sent_idempotency has already pruned, so
    # they deliberately disagree with the row count above.
    # 'resend' is a retired provider: it left the chain in 2026-08 and its
    # historical counters stay in the table, so the all-time total spans it.
    conn.execute("INSERT INTO email_send_counts (provider, day, n) VALUES "
                 "('mailjet', '2026-07-01', 484), ('resend', '2026-07-02', 1)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('last_failure_alert_at', '2026-06-01T00:00:00')")
    s = stats(conn)
    up = s["upstream_by_city"]["leipzig"]
    assert up == {"polls_today": 5, "polls_total": 50,
                  "requests_today": 12, "requests_total": 120}
    assert s["last_polled_at_by_city"]["leipzig"] == "2026-06-03T10:00:00"
    assert s["slots_cached"] == 1
    assert s["emails_sent_last_7d"] == 1   # 'pending' excluded
    # All-time comes from email_send_counts, NOT from the 14-day-pruned table.
    assert s["emails_sent_recorded"] == 485
    assert s["emails_sent_since"] == "Jul 2026"
    assert s["last_failure_alert_at"] == "2026-06-01T00:00:00"

def test_stats_notification_metrics(tmp_path):
    from datetime import time
    from app.models import Filter
    from app.repo import insert_pending, confirm
    conn = connect(str(tmp_path / "n.db")); init_schema(conn)
    f = Filter(appointment_types=["A"], locations="all", weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    s1 = insert_pending(conn, email="a@x", city="leipzig", language="de", filter_=f, ttl_days=90)
    confirm(conn, s1)
    s2 = insert_pending(conn, email="b@x", city="leipzig", language="de", filter_=f, ttl_days=90)
    confirm(conn, s2)
    # s1 was served a slot 2h ago; s2 has never matched.
    conn.execute("UPDATE subscriptions SET last_notified_at=datetime('now','-2 hours') WHERE id=?", (s1,))
    for k, p in (("m1", "mailjet"), ("r1", "brevo"), ("p1", "pending")):
        conn.execute("INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, ?)", (k, p))
    s = stats(conn)
    assert s["notifications_24h"] == 1
    assert s["notifications_7d"] == 1
    assert s["subscribers_ever_notified"] == 1
    assert s["active_awaiting_first_match"] == 1            # s2 still waiting
    assert s["last_notification"]["sub_id"] == s1
    assert s["emails_by_provider_7d"] == {"mailjet": 1, "brevo": 1}  # 'pending' excluded

def test_stats_distinct_active_subscribers(tmp_path):
    from datetime import time
    from app.models import Filter
    from app.repo import insert_pending, confirm
    conn = connect(str(tmp_path / "e.db")); init_schema(conn)
    f = Filter(appointment_types=["A"], locations="all", weekdays=[1, 2, 3, 4, 5, 6, 7],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    # One person holds two subscriptions — here differing only in case, which
    # rows from before the subscribe form lowercased can still do.
    for email in ("one@example.com", "One@Example.com", "two@example.com"):
        confirm(conn, insert_pending(conn, email=email, city="leipzig",
                                     language="de", filter_=f, ttl_days=90))
    insert_pending(conn, email="pending@example.com", city="leipzig",
                   language="de", filter_=f, ttl_days=90)   # never confirmed
    s = stats(conn)
    assert s["active_subscriptions"] == 3
    assert s["active_subscribers"] == 2


# ---------- ops-summary: anomaly detection + compact email ----------

NOW = datetime(2026, 6, 9, 14, 34, 0)


def _summary_stats(**over):
    # A wholly healthy baseline: no anomalies should fire against this. Poll is
    # fresh, quotas well under warn %, signups near baseline, backup recent.
    base = {
        "active_subscriptions": 42,
        "active_subscriptions_by_city": {"leipzig": 42},
        "current_plan_count_by_city": {"leipzig": 6},
        "parser_zero_match_since_by_city": {},
        "pending_confirmation": 3,
        "signups_last_24h": 5,
        "signups_last_7d": 19,
        "emails_sent_last_7d": 88,
        "upstream_by_city": {"leipzig": {"polls_today": 120, "polls_total": 9821,
                                         "requests_today": 240, "requests_total": 20140}},
        "last_polled_at_by_city": {"leipzig": "2026-06-09T14:32:00"},
        "city_labels": {"leipzig": "Leipzig"},
        "slots_cached": 17,
        "emails_sent_recorded": 1203,
        "emails_sent_since": "Jul 2026",
        "notifications_24h": 2,
        "notifications_7d": 7,
        "subscribers_ever_notified": 9,
        "active_awaiting_first_match": 4,
        "last_notification": {"sub_id": 5, "at": "2026-06-09T14:33:00"},
        "emails_by_provider_7d": {"mailjet": 80, "brevo": 8},
        "email_usage": {"mailjet": {"month": 100, "today": 12,
                                    "month_quota": 6000, "day_quota": 200}},
        "last_failure_alert_at": None,
        "last_housekeeping_at": "2026-06-09T11:30:00",
        "last_backup_at": "2026-06-09T09:30:00",
    }
    base.update(over)
    return base


def _line(text, needle):
    return next(l for l in text.splitlines() if needle in l)


# --- anomaly detection ---

def test_healthy_baseline_has_no_anomalies():
    assert summary_anomalies(_summary_stats(), now=NOW) == []


def test_anomaly_quota_near_cap():
    # Mailjet alone in the pool, so its 170/200 IS the combined 85%.
    a = summary_anomalies(_summary_stats(email_usage={
        "mailjet": {"month": 100, "today": 170, "rolling": 170,
                    "month_quota": 6000, "day_quota": 200},
    }), now=NOW)
    assert any("combined rolling 24h quota at 85% (170/200)" in x for x in a)


def test_anomaly_day_quota_grades_the_pool_not_one_provider():
    """Mailjet-first routing exhausts Mailjet before the fallback sees
    anything, so a hot primary is a busy day, not a warning. 196 of a combined
    300 is 65%."""
    a = summary_anomalies(_summary_stats(email_usage={
        "mailjet": {"month": 100, "today": 196, "rolling": 196,
                    "month_quota": 6000, "day_quota": 200},
        "brevo": {"month": 0, "today": 0, "rolling": 0,
                  "month_quota": 9000, "day_quota": 100},
    }), now=NOW)
    assert not any("quota at" in x and "rolling" in x for x in a)


def test_anomaly_quota_grades_the_window_that_actually_gates_sending():
    """The admin page showed `combined day quota at 89% (532/600)` in the alert
    box while the Email quota section right below it showed the gating figure at
    92% (555/600) — the same page disagreeing with itself, because the alert
    read the UTC-day counters and the gate is the rolling 24h window."""
    a = summary_anomalies(_summary_stats(email_usage={
        "mailjet": {"month": 100, "today": 193, "rolling": 200,
                    "month_quota": 6000, "day_quota": 200},
        "brevo": {"month": 100, "today": 258, "rolling": 274,
                  "month_quota": 9000, "day_quota": 300},
        "sweego": {"month": 100, "today": 81, "rolling": 81,
                   "month_quota": 3000, "day_quota": 100},
    }), now=NOW)
    assert any("combined rolling 24h quota at 92% (555/600)" in x for x in a)


def test_anomaly_quota_still_warns_just_after_utc_midnight():
    """The failure this fixes. At 00:05 UTC the day counters have snapped to 0
    while the rolling window still carries last evening's sends, so the old
    grading reported all-clear at the exact moment the real gate was closest to
    deferring — and Bürgerwecker sends heavily in the evening."""
    a = summary_anomalies(_summary_stats(email_usage={
        "mailjet": {"month": 0, "today": 0, "rolling": 200,
                    "month_quota": 6000, "day_quota": 200},
        "brevo": {"month": 0, "today": 0, "rolling": 290,
                  "month_quota": 9000, "day_quota": 300},
        "sweego": {"month": 0, "today": 0, "rolling": 60,
                   "month_quota": 3000, "day_quota": 100},
    }), now=NOW)
    assert any("combined rolling 24h quota at 92% (550/600)" in x for x in a)


def test_anomaly_quota_falls_back_to_today_when_rolling_is_absent():
    """A pre-migration DB returns usage without `rolling`. Reading the missing
    key as zero sends would silence the warning entirely."""
    a = summary_anomalies(_summary_stats(email_usage={
        "mailjet": {"month": 100, "today": 190, "month_quota": 6000,
                    "day_quota": 200},
    }), now=NOW)
    assert any("combined rolling 24h quota at 95% (190/200)" in x for x in a)


def test_anomaly_month_quota_stays_per_provider():
    """Monthly caps are hard per-account walls — no failover borrows against
    them, so they are graded one provider at a time."""
    a = summary_anomalies(_summary_stats(email_usage={
        "mailjet": {"month": 5400, "today": 10, "month_quota": 6000, "day_quota": 200},
        "brevo": {"month": 0, "today": 0, "month_quota": 9000, "day_quota": 300},
    }), now=NOW)
    assert any("mailjet month quota at 90% (5400/6000)" in x for x in a)


def test_anomaly_signup_spike():
    a = summary_anomalies(_summary_stats(signups_last_24h=51, signups_last_7d=68),
                          now=NOW)
    assert any("signup spike: 51 in 24h" in x for x in a)


def test_anomaly_signup_drop():
    # Baseline ~4/day, zero in the last 24h -> flagged.
    a = summary_anomalies(_summary_stats(signups_last_24h=0, signups_last_7d=28),
                          now=NOW)
    assert any("no signups in 24h" in x for x in a)


def test_no_signup_drop_for_quiet_tenant():
    # Baseline below SIGNUP_DROP_BASELINE: a zero day is normal, not an anomaly.
    assert summary_anomalies(_summary_stats(signups_last_24h=0, signups_last_7d=7),
                             now=NOW) == []


def test_anomaly_stale_polling():
    a = summary_anomalies(
        _summary_stats(last_polled_at_by_city={"leipzig": "2026-06-09T06:00:00"}),
        now=NOW)  # ~8.5h stale
    assert any("Leipzig: not polled for 8h" in x for x in a)


def test_anomaly_never_polled_with_subs():
    a = summary_anomalies(_summary_stats(last_polled_at_by_city={}), now=NOW)
    assert any("Leipzig: 42 active subs but no poll recorded" in x for x in a)


def test_zero_matches_alone_is_not_an_anomaly():
    # A scarce city legitimately reports zero matches; the canary owns that case.
    assert summary_anomalies(
        _summary_stats(parser_zero_match_since_by_city={"leipzig": "2026-06-08T06:00:00"}),
        now=NOW) == []


def test_anomaly_reflects_recent_failure_alert():
    a = summary_anomalies(
        _summary_stats(last_failure_alert_at="2026-06-09T12:00:00"), now=NOW)
    assert any("failure alert fired" in x for x in a)


def test_anomaly_reflects_stale_backup():
    a = summary_anomalies(_summary_stats(last_backup_at=None), now=NOW)
    assert any("backup is stale" in x for x in a)


# --- compact email rendering ---

def test_email_leads_with_anomalies():
    anomalies = ["mailjet today quota at 85% (170/200)", "backup is stale (>48h) or missing"]
    text = render_summary_email(_summary_stats(), now=NOW, anomalies=anomalies,
                                base_url="https://buergerwecker.de")
    assert "2 things need a look:" in text
    assert "• mailjet today quota at 85% (170/200)" in text
    assert "https://buergerwecker.de/admin" in _line(text, "Full dashboard")


def test_email_singular_anomaly_grammar():
    text = render_summary_email(_summary_stats(), now=NOW,
                                anomalies=["backup is stale (>48h) or missing"])
    assert "1 thing needs a look:" in text


def test_email_heartbeat_all_clear():
    text = render_summary_email(_summary_stats(), now=NOW, anomalies=[])
    assert "all-clear" in text.lower()


def test_email_snapshot_numbers():
    text = render_summary_email(_summary_stats(), now=NOW, anomalies=[])
    assert "42" in _line(text, "Active subs") and "Leipzig 42" in _line(text, "Active subs")
    signups = _line(text, "Signups")
    assert "24h 5" in signups and "7d 19" in signups
    assert "mailjet 80" in _line(text, "Delivery")
    assert "mailjet 12/200" in _line(text, "Quota today")


def test_stats_today_counts_gated_by_stale_date(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    conn.execute(
        "INSERT INTO city_state (city, polls_today, polls_total, requests_today, "
        "requests_total, counts_date) VALUES ('leipzig', 99, 50, 99, 120, '2000-01-01')")
    up = stats(conn)["upstream_by_city"]["leipzig"]
    assert up["polls_today"] == 0 and up["requests_today"] == 0   # stale day -> 0
    assert up["polls_total"] == 50 and up["requests_total"] == 120  # totals intact

def test_go_tokens_do_not_collide_across_tenants(client):
    """Two tenants can expose a slot at the SAME wall-clock datetime. The cache
    key is tenant-prefixed, so both rows exist and each /go link redirects to
    its own tenant's upstream URL — never the other's."""
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    dt = "2026-07-02T11:50:00+02:00"
    conn.execute("INSERT INTO slots_cache (slot_token, city, upstream_url) "
                 "VALUES (?, 'leipzig', 'https://up/ba')", (f"leipzig:{dt}",))
    conn.execute("INSERT INTO slots_cache (slot_token, city, upstream_url) "
                 "VALUES (?, 'leipzig-abh', 'https://up/abh')", (f"leipzig-abh:{dt}",))
    enc = dt.replace(":", "%3a").replace("+", "%2b")
    r_ba = client.get(f"/go/leipzig:{enc}", follow_redirects=False)
    r_abh = client.get(f"/go/leipzig-abh:{enc}", follow_redirects=False)
    assert r_ba.headers["Location"] == "https://up/ba"
    assert r_abh.headers["Location"] == "https://up/abh"

def test_admin_aggregates_upstream_load_per_host_and_labels_tenants(client):
    """Both Leipzig tenants poll the same physical host: the dashboard must
    show the combined host load (the rate-limit-relevant number) and label
    tenant cards from display.json instead of the raw catalog key."""
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    today = datetime.utcnow().date().isoformat()
    for city, polls, reqs in [("leipzig", 10, 30), ("leipzig-abh", 4, 8)]:
        conn.execute(
            "INSERT INTO city_state (city, polls_today, polls_total, "
            "requests_today, requests_total, counts_date, last_polled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (city, polls, polls, reqs, reqs, today))
    s = stats(conn)
    host = s["upstream_by_host"]["terminvereinbarung.leipzig.de"]
    assert host["requests_today"] == 38          # 30 + 8, combined
    assert host["polls_today"] == 14
    assert host["tenants"] == ["leipzig", "leipzig-abh"]
    # Tenant labels come from display.json (admin is English-only).
    assert "ausländerbehörde" in s["city_labels"]["leipzig-abh"].lower() or \
           "foreigners" in s["city_labels"]["leipzig-abh"].lower()
    # Dashboard renders the labeled row + the shared-host section (both Leipzig
    # tenants poll one server, so this host qualifies as shared).
    html = client.get("/admin?token=admin-tok").data.decode()
    assert "Shared upstream hosts" in html
    assert "terminvereinbarung.leipzig.de" in html
    assert "Leipzig-abh" not in html             # raw key no longer shown


# ---------- email quota view (durable per-day counters) ----------

def test_stats_email_usage_windows_and_caps(tmp_path):
    from types import SimpleNamespace
    conn = connect(str(tmp_path / "u.db")); init_schema(conn)
    conn.execute("INSERT INTO email_send_counts (provider, day, n) "
                 "VALUES ('mailjet', date('now'), 3)")
    # A day from a long-gone month must not count toward month-to-date.
    conn.execute("INSERT INTO email_send_counts (provider, day, n) "
                 "VALUES ('mailjet', '2000-01-15', 99)")
    cfg = SimpleNamespace(mailjet_monthly_quota=6000, mailjet_daily_quota=200,
                          brevo_api_key="k", brevo_monthly_quota=9000,
                          brevo_daily_quota=300,
                          email_provider_order=("mailjet", "brevo"))
    # Two sends inside the rolling window; the UTC-day counter above is separate
    # bookkeeping and the two are allowed to disagree.
    conn.executemany(
        "INSERT INTO sent_idempotency (idem_key, provider) VALUES (?, 'mailjet')",
        [("w1",), ("w2",)])
    u = stats(conn, cfg)["email_usage"]
    assert u["mailjet"] == {"month": 3, "today": 3, "rolling": 2,
                            "month_quota": 6000, "day_quota": 200}
    # Brevo has sent nothing yet but still shows up with its caps.
    assert u["brevo"] == {"month": 0, "today": 0, "rolling": 0,
                          "month_quota": 9000, "day_quota": 300}

def test_stats_email_usage_lists_new_providers_only_when_configured(tmp_path):
    """Brevo/Sweego join the quota table (and thus the ops-summary combined
    pool) only once they can actually send: API key configured AND named in
    EMAIL_PROVIDER_ORDER — the same gate mail._daily_usage applies. A provider
    that cannot send must not add its cap to the pool math."""
    from types import SimpleNamespace
    conn = connect(str(tmp_path / "v.db")); init_schema(conn)
    base = dict(mailjet_monthly_quota=6000, mailjet_daily_quota=200,
                brevo_api_key="xkeysib-x", brevo_monthly_quota=9000,
                brevo_daily_quota=300, sweego_api_key="",
                sweego_monthly_quota=3000, sweego_daily_quota=100)
    cfg = SimpleNamespace(**base, email_provider_order=(
        "mailjet", "brevo", "sweego"))
    u = stats(conn, cfg)["email_usage"]
    assert u["brevo"] == {"month": 0, "today": 0, "rolling": 0,
                          "month_quota": 9000, "day_quota": 300}
    assert "sweego" not in u                    # in the order, but keyless
    # Keyed but NOT in the order — the smoke-test state: the cap must stay out
    # of the pool, exactly as the digest path skips the provider.
    cfg = SimpleNamespace(**base, email_provider_order=("mailjet",))
    assert "brevo" not in stats(conn, cfg)["email_usage"]

def test_stats_email_usage_follows_provider_chain_order(tmp_path):
    """The quota table lists providers in EMAIL_PROVIDER_ORDER — the order a
    send actually falls back along — not alphabetically. A provider that only
    exists in the counters (retired from the chain) trails it."""
    from types import SimpleNamespace
    conn = connect(str(tmp_path / "o.db")); init_schema(conn)
    conn.execute("INSERT INTO email_send_counts (provider, day, n) "
                 "VALUES ('acme-retired', date('now'), 7)")
    cfg = SimpleNamespace(
        mailjet_monthly_quota=6000, mailjet_daily_quota=200,
        brevo_api_key="k", brevo_monthly_quota=9000, brevo_daily_quota=300,
        sweego_api_key="k", sweego_monthly_quota=3000, sweego_daily_quota=100,
        email_provider_order=("mailjet", "brevo", "sweego"))
    u = stats(conn, cfg)["email_usage"]
    assert list(u) == ["mailjet", "brevo", "sweego", "acme-retired"]

def test_init_schema_backfills_counters_once(tmp_path):
    conn = connect(str(tmp_path / "b.db")); init_schema(conn)
    # 'resend' stands in for a retired provider — no caps in config any more,
    # but its backfilled counters must still surface.
    for k, p in (("k1", "mailjet"), ("k2", "mailjet"), ("k3", "resend"),
                 ("k4", "pending")):
        conn.execute("INSERT INTO sent_idempotency (idem_key, provider) "
                     "VALUES (?, ?)", (k, p))
    conn.execute("DELETE FROM email_send_counts")  # simulate pre-feature DB
    init_schema(conn)                              # first init after upgrade
    u = stats(conn)["email_usage"]
    assert u["mailjet"]["today"] == 2
    assert u["resend"]["today"] == 1
    assert "pending" not in u
    init_schema(conn)                              # re-run must not double-count
    assert stats(conn)["email_usage"]["mailjet"]["today"] == 2

def test_admin_renders_email_quota_section(client):
    conn = connect(os.environ["DB_PATH"])
    conn.execute("INSERT INTO email_send_counts (provider, day, n) "
                 "VALUES ('mailjet', date('now'), 483)")
    html = client.get("/admin?token=admin-tok").data.decode()
    assert "Email quota" in html
    assert "483 / 6000" in html          # MAILJET_MONTHLY_QUOTA default
    # combined + deferred lead the section; the per-provider rows follow.
    q = html.index("Email quota")
    assert (q < html.index(">combined</td>") < html.index(">deferred</td>")
            < html.index(">mailjet</td>"))

def test_summary_email_quota_line_lists_each_provider():
    text = render_summary_email(_summary_stats(email_usage={
        "mailjet": {"month": 483, "today": 12, "month_quota": 6000, "day_quota": 200},
        "brevo":   {"month": 1, "today": 0, "month_quota": 9000, "day_quota": 300},
    }), now=NOW, anomalies=[])
    q = _line(text, "Quota today")
    assert "mailjet 12/200" in q and "brevo 0/300" in q


def test_stats_and_anomaly_report_deferrals(tmp_path):
    from types import SimpleNamespace
    conn = connect(str(tmp_path / "d.db")); init_schema(conn)
    conn.executemany(
        "INSERT INTO email_deferral_counts (day, n) VALUES (?, ?)",
        [(datetime.utcnow().date().isoformat(), 12), ("2000-01-15", 99)])
    cfg = SimpleNamespace(mailjet_monthly_quota=6000, mailjet_daily_quota=200)
    s = stats(conn, cfg)
    assert s["deferrals_today"] == 12
    assert s["deferrals_7d"] == 12          # the 2000 row is outside the window
    a = summary_anomalies(_summary_stats(deferrals_today=12), now=NOW)
    assert any("12 notification(s) deferred today" in x for x in a)


def test_anomaly_deferral_reported_even_below_quota_thresholds():
    """A deferral is not a "nearing the cap" warning — it is the cap having
    already cost a subscriber a slot, so percentages don't gate it."""
    a = summary_anomalies(_summary_stats(deferrals_today=1, email_usage={
        "mailjet": {"month": 10, "today": 5, "month_quota": 6000, "day_quota": 200},
    }), now=NOW)
    assert any("deferred today" in x for x in a)
    assert not any("quota at" in x for x in a)


def test_stats_expose_the_wall_behind_the_deferral_count(tmp_path):
    from types import SimpleNamespace
    conn = connect(str(tmp_path / "d.db")); init_schema(conn)
    conn.execute("INSERT INTO email_deferral_counts (day, n) VALUES (date('now'), 3)")
    conn.executemany(
        "INSERT INTO email_deferrals (n, wall, frees_at) VALUES (?, ?, ?)",
        [(2, "hourly", "2026-08-26 13:05:00"), (1, "daily", "2026-08-27 09:31:00")])
    s = stats(conn, SimpleNamespace(mailjet_monthly_quota=6000, mailjet_daily_quota=200))
    assert s["deferral_walls_today"] == {"hourly": 2, "daily": 1}
    assert s["last_deferral"]["wall"] == "daily"
    assert s["last_deferral"]["frees_at"] == "2026-08-27 09:31:00"


def test_anomaly_and_summary_split_deferrals_by_wall():
    a = summary_anomalies(_summary_stats(deferrals_today=3, deferral_walls_today={
        "hourly": 2, "daily": 1}), now=NOW)
    line = next(x for x in a if "deferred today" in x)
    assert "hourly 2, daily 1" in line and "rolling 24h window" in line
    text = render_summary_email(_summary_stats(deferrals_today=3, deferrals_7d=3,
                                               deferral_walls_today={"hourly": 3}),
                                now=NOW, anomalies=[])
    d = _line(text, "Deferred")
    assert "hourly 3" in d and "rolling 24h window" not in d   # nothing lost


def test_admin_page_shows_the_last_deferral_and_its_wall(client):
    conn = connect(os.environ["DB_PATH"])
    conn.execute("INSERT INTO email_deferral_counts (day, n) VALUES (date('now'), 1)")
    conn.execute("INSERT INTO email_deferrals (at, n, wall, frees_at) VALUES "
                 "('2026-08-26 12:05:11', 1, 'daily', '2026-08-27 09:31:00')")
    html = client.get("/admin?token=admin-tok").data.decode()
    assert "last 12:05 UTC, 1 against the" in html
    assert "daily wall" in html and "frees 2026-08-27 09:31 UTC" in html


def test_admin_renders_subscriber_and_cancellation_charts(client):
    from app.db import connect
    conn = connect(os.environ["DB_PATH"])
    conn.execute(
        "INSERT INTO subscriptions (email, city, filters_json, confirmed_at, "
        " expires_at, deleted_at) VALUES ('x@example.com', 'leipzig', '{}', "
        " datetime('now','-5 days'), datetime('now','+30 days'), datetime('now','-2 days'))")
    conn.commit()
    html = client.get("/admin?token=admin-tok").get_data(as_text=True)
    assert "Subscribers" in html and "Cancellations" in html
    assert "Expired, not renewed" in html
    assert "1 people (1 unsubscribed, 0 expired)" in html


def test_stats_plan_counts_use_the_loaded_config_cap(tmp_path, monkeypatch):
    """stats() used to re-read MAX_PLANS_PER_CITY from the environment and
    ignore the Config it was handed, so its plan counts could disagree with
    what the poller actually builds."""
    from datetime import time
    from types import SimpleNamespace
    from app.repo import insert_pending, confirm
    from app.models import Filter
    monkeypatch.delenv("MAX_PLANS_PER_CITY", raising=False)
    conn = connect(str(tmp_path / "c.db")); init_schema(conn)
    for loc in (["1"], ["2"]):
        f = Filter(appointment_types=["A"], locations=loc, weekdays=[1],
                   time_window_start=time(0, 0), time_window_end=time(23, 59))
        sid = insert_pending(conn, email=f"{loc[0]}@example.com", city="dresden",
                             language="de", filter_=f, ttl_days=90)
        confirm(conn, sid)
    cfg = SimpleNamespace(max_plans_per_city=1)
    # Two distinct plans over a cap of one collapse to a single "all" plan.
    assert stats(conn, cfg)["current_plan_count_by_city"]["dresden"] == 1
    assert stats(conn)["current_plan_count_by_city"]["dresden"] == 2
