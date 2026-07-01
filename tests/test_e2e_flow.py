import os
from datetime import time
from unittest.mock import patch
import pytest
from app.web import create_app
from app.db import connect, init_schema
from app.models import Slot

@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    for k, v in {
        "DB_PATH": db_path,
        "TOKEN_SECRET_PRIMARY":"x"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"a"*32,
        "MAILJET_API_KEY":"m","MAILJET_API_SECRET":"m","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"r",
        "PUBLIC_BASE_URL":"https://x","DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"99","SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"99",
        "DEVELOPER_EMAIL":"dev@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    conn = connect(db_path); init_schema(conn)
    return db_path

def test_full_flow(env):
    app = create_app(); app.config["TESTING"] = True
    c = app.test_client()

    sent_mails: list[tuple[str, str]] = []

    def fake_send(conn, to, subject, body, *, idem_key):
        sent_mails.append((to, subject))

    # Digests are delivered via the batched path (app.digest.send_batch), so
    # capture that too and report everything delivered.
    from app.mail import BatchResult

    def fake_send_batch(conn, items, cfg):
        for it in items:
            sent_mails.append((it.to, it.subject))
        return BatchResult(delivered={it.idem_key for it in items})

    # 1. subscribe (mock mail)
    # Patch every binding site of app.mail.send since the symbol is imported by name
    # into app.web (as mail_send) and app.digest (as send), bypassing patching of
    # app.mail.send itself.
    mail_patch_targets = ("app.mail.send", "app.web.mail_send", "app.digest.send")

    def _patch_mail():
        from contextlib import ExitStack
        stack = ExitStack()
        for tgt in mail_patch_targets:
            stack.enter_context(patch(tgt, side_effect=fake_send))
        stack.enter_context(patch("app.digest.send_batch", side_effect=fake_send_batch))
        return stack

    with _patch_mail():
        r = c.post("/subscribe", data={
            "lang":"de","city":"leipzig",
            "email":"alice@example.com",
            "appointment_type":"29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
            "all_locations":"1",
            "time_start":"00:00","time_end":"23:59",
            "weekdays":["1","2","3","4","5"],
            "website":"",
        })
        assert r.status_code in (200, 302)
    assert any("Bestätigung" in s for _, s in sent_mails)

    # 2. confirm
    conn = connect(env)
    sid = conn.execute("SELECT id FROM subscriptions WHERE email='alice@example.com'").fetchone()["id"]
    from app.tokens import sign
    tok = sign(sid, "confirm", primary="x"*32, previous="")
    with _patch_mail():
        r = c.get(f"/confirm/{tok}")
    assert r.status_code in (200, 302)
    assert any("Verwaltungs-Link" in s or "Management link" in s for _, s in sent_mails)

    # 3. run a polling cycle with a synthetic slot
    from app.cycle import run_cycle
    fake_slots = [Slot("2026-06-10", "10:30",
                       "loc-1",
                       "29cd0a26-fe7a-4d65-88cd-1e05fd749c71",
                       "tok")]
    from unittest.mock import MagicMock
    scraper = MagicMock(); scraper.poll.return_value = fake_slots
    with patch("app.cycle.get_scraper", return_value=scraper), _patch_mail():
        run_cycle(conn, max_plans_per_city=10, rate_limit_minutes=15,
                  cycle_id="e2e-1")
    digest_seen = any("Neue Termine" in s for _, s in sent_mails)
    assert digest_seen

    # 4. run a SECOND cycle with the same slot — must dedup, no new digest
    sent_count_before = sum(1 for _, s in sent_mails if "Neue Termine" in s)
    with patch("app.cycle.get_scraper", return_value=scraper), _patch_mail():
        run_cycle(conn, max_plans_per_city=10, rate_limit_minutes=0,
                  cycle_id="e2e-2")
    sent_count_after = sum(1 for _, s in sent_mails if "Neue Termine" in s)
    assert sent_count_after == sent_count_before, \
        "dedup failed: same slot triggered a second digest"

    # 5. unsubscribe
    unsub_tok = sign(sid, "unsubscribe", primary="x"*32, previous="")
    r = c.get(f"/unsubscribe/{unsub_tok}")
    assert r.status_code in (200, 302)
    row = conn.execute("SELECT deleted_at FROM subscriptions WHERE id=?", (sid,)).fetchone()
    assert row["deleted_at"] is not None
