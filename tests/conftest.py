import pytest


@pytest.fixture(autouse=True)
def _no_mailjet_contact_prune(monkeypatch):
    # run_once reads the real default (3 days); ordinary tests must not call Mailjet.
    monkeypatch.setenv("MAILJET_CONTACT_MAX_AGE_DAYS", "0")
