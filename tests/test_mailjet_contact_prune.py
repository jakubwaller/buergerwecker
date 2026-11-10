from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import requests
from app.housekeeping import (_prune_mailjet_contacts, _start_mailjet_prune,
                              wait_for_mailjet_prune)
from tests.test_housekeeping import db  # noqa: F401  (fixture)

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

def test_failed_delete_skipped_rest_continue(capsys):
    def delete(url, **k):
        return _resp(500 if url.endswith("/1") else 200)
    ids = _run([_c(1, "a@example.com"), _c(2, "b@example.com")], set(), delete=delete)
    assert ids == ["1", "2"]
    assert "failed 1 ['500']" in capsys.readouterr().out

def test_stops_after_ten_failures():
    ids = _run([_c(i, f"u{i}@example.com") for i in range(30)], set(),
               delete=lambda *a, **k: _resp(500))
    assert len(ids) == 10

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
