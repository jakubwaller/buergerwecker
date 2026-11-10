from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import requests
from app.housekeeping import _prune_mailjet_contacts

def _ts(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

def _cfg(days=3):
    return SimpleNamespace(mailjet_contact_max_age_days=days,
                           mailjet_api_key="k", mailjet_api_secret="s")

def _resp(code, data=None):
    return SimpleNamespace(status_code=code, json=lambda: {"Data": data or []})

def test_old_deleted_young_kept(capsys):
    rows = [{"ID": 1, "Email": "old@example.com", "CreatedAt": _ts(5)},
            {"ID": 2, "Email": "new@example.com", "CreatedAt": _ts(1)}]
    with patch("requests.get", return_value=_resp(200, rows)), \
         patch("requests.delete", return_value=_resp(200)) as d, \
         patch("time.sleep"):
        _prune_mailjet_contacts(_cfg())
    assert [c.args[0] for c in d.call_args_list] == ["https://api.mailjet.com/v4/contacts/1"]
    out = capsys.readouterr()
    assert "example.com" not in out.out + out.err

def test_api_failure_does_not_raise(capsys):
    with patch("requests.get", side_effect=requests.ConnectionError("old@example.com")):
        _prune_mailjet_contacts(_cfg())
    with patch("requests.get", return_value=_resp(500)):
        _prune_mailjet_contacts(_cfg())
    out = capsys.readouterr()
    assert "example.com" not in out.out + out.err

def test_zero_disables():
    with patch("requests.get") as g, patch("requests.delete") as d:
        _prune_mailjet_contacts(_cfg(0))
    assert not g.called and not d.called
