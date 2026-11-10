import pytest


@pytest.fixture(autouse=True)
def _no_mailjet_contact_prune(monkeypatch):
    # run_once reads the real default (800); ordinary tests must not call Mailjet.
    monkeypatch.setenv("MAILJET_MAX_CONTACTS", "0")
