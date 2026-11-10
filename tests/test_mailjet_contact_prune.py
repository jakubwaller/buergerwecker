from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import requests
from app.housekeeping import (_prune_mailjet_contacts, _start_mailjet_prune,
                              wait_for_mailjet_prune)
from test_housekeeping import db  # noqa: F401  (fixture)

def _cfg(ceiling=800):
    return SimpleNamespace(mailjet_max_contacts=ceiling,
                           mailjet_api_key="k", mailjet_api_secret="s")

def _c(i, email, age=1):
    ts = (datetime.utcnow() - timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ID": i, "Email": email, "CreatedAt": ts}

def _resp(code, data=None):
    return SimpleNamespace(status_code=code, json=lambda: {"Data": data or []})

def _run(contacts, live, ceiling=800, delete=None):
    with patch("requests.get", return_value=_resp(200, contacts)), \
         patch("requests.delete", side_effect=delete or (lambda *a, **k: _resp(200))) as d, \
         patch("time.sleep"):
        _prune_mailjet_contacts(_cfg(ceiling), live)
    return [c.args[0].rsplit("/", 1)[1] for c in d.call_args_list]

def test_left_subscriber_deleted_live_kept(capsys):
    ids = _run([_c(1, "Left@Example.com"), _c(2, "stay@example.com")], {"stay@example.com"})
    assert ids == ["1"]
    out = capsys.readouterr()
    assert "example.com" not in out.out + out.err

def test_ceiling_deletes_oldest_live():
    contacts = [_c(1, "a@example.com", 1), _c(2, "b@example.com", 9), _c(3, "c@example.com", 5)]
    assert _run(contacts, {"a@example.com", "b@example.com", "c@example.com"}, ceiling=1) \
        == ["2", "3"]

def test_paging_advances_and_stops_on_short_page():
    page1 = [_c(i, f"u{i}@example.com") for i in range(1000)]
    page2 = [_c(5000, "z@example.com")]
    with patch("requests.get", side_effect=[_resp(200, page1), _resp(200, page2)]) as g, \
         patch("requests.delete", return_value=_resp(200)), patch("time.sleep"):
        _prune_mailjet_contacts(_cfg(), set(f"u{i}@example.com" for i in range(1000)))
    assert [c.kwargs["params"]["Offset"] for c in g.call_args_list] == [0, 1000]

def test_404_skipped_rest_continue(capsys):
    def delete(url, **k):
        return _resp(404 if url.endswith("/1") else 200)
    ids = _run([_c(1, "a@example.com"), _c(2, "b@example.com")], set(), delete=delete)
    assert ids == ["1", "2"]
    assert "failed 1 ['404']" in capsys.readouterr().out

@pytest.mark.parametrize("bad", [_resp(500), _resp(429), requests.ConnectionError("x")])
def test_systemic_failure_stops_run(bad):
    def delete(*a, **k):
        if isinstance(bad, Exception):
            raise bad
        return bad
    ids = _run([_c(i, f"u{i}@example.com") for i in range(30)], set(), delete=delete)
    assert len(ids) == 1

def test_sql_keeps_only_live_subscriber(db):  # noqa: F811
    from datetime import time
    from app.models import Filter
    from app.repo import insert_pending, confirm
    f = Filter(appointment_types=["A"], locations="all", weekdays=[1],
               time_window_start=time(0, 0), time_window_end=time(23, 59))
    ids = {}
    for name in ("live", "gone", "expired"):
        ids[name] = insert_pending(db, email=f"{name}@example.com", city="x",
                                   language="de", filter_=f, ttl_days=30)
        confirm(db, ids[name])
    db.execute("UPDATE subscriptions SET deleted_at=CURRENT_TIMESTAMP WHERE id=?", (ids["gone"],))
    db.execute("UPDATE subscriptions SET expires_at=datetime('now','-1 day') WHERE id=?",
               (ids["expired"],))
    contacts = [_c(1, "Live@example.com"), _c(2, "gone@example.com"), _c(3, "expired@example.com")]
    with patch("requests.get", return_value=_resp(200, contacts)), \
         patch("requests.delete", return_value=_resp(200)) as d, patch("time.sleep"):
        _start_mailjet_prune(db, _cfg())
        wait_for_mailjet_prune(5)
    assert sorted(c.args[0].rsplit("/", 1)[1] for c in d.call_args_list) == ["2", "3"]

def test_api_failure_does_not_raise(capsys):
    with patch("requests.get", side_effect=requests.ConnectionError("a@example.com")):
        _prune_mailjet_contacts(_cfg(), set())
    with patch("requests.get", return_value=_resp(500)):
        _prune_mailjet_contacts(_cfg(), set())
    out = capsys.readouterr()
    assert "example.com" not in out.out + out.err

def test_zero_disables():
    with patch("requests.get") as g, patch("requests.delete") as d:
        _prune_mailjet_contacts(_cfg(0), set())
        _start_mailjet_prune(None, _cfg(0))
    assert not g.called and not d.called

def test_run_once_starts_the_step(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("MAILJET_MAX_CONTACTS", "800")
    from app.housekeeping import run_once
    with patch("app.housekeeping._prune_mailjet_contacts") as prune, patch("app.mail.send"):
        run_once(db)
        wait_for_mailjet_prune(5)
    assert prune.call_count == 1
