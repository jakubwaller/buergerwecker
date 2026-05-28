import os
import pytest
from app.config import Config, load_config

def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("MAILJET_API_KEY", "mj_key")
    monkeypatch.setenv("MAILJET_API_SECRET", "mj_secret")
    monkeypatch.setenv("MAILJET_FROM_EMAIL", "termine@example.eu")
    monkeypatch.setenv("MAILJET_FROM_NAME", "Termine")
    monkeypatch.setenv("MAILJET_DAILY_QUOTA", "6000")
    monkeypatch.setenv("RESEND_API_KEY", "re_key")
    monkeypatch.setenv("TOKEN_SECRET_PRIMARY", "a" * 32)
    monkeypatch.setenv("TOKEN_SECRET_PREVIOUS", "")
    monkeypatch.setenv("ADMIN_TOKEN", "b" * 32)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://termine.example.eu")
    monkeypatch.setenv("DEDUP_WINDOW_HOURS", "24")
    monkeypatch.setenv("RATE_LIMIT_MINUTES", "15")
    monkeypatch.setenv("SUBSCRIPTION_TTL_DAYS", "90")
    monkeypatch.setenv("RENEWAL_REMINDER_DAYS_BEFORE", "10")
    monkeypatch.setenv("MAX_PLANS_PER_CITY", "10")
    monkeypatch.setenv("PARSER_CANARY_THRESHOLD_HOURS", "2")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR", "5")
    monkeypatch.setenv("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY", "1")
    monkeypatch.setenv("DEVELOPER_EMAIL", "dev@example.eu")
    monkeypatch.setenv("KOFI_URL", "https://ko-fi.com/jakubwaller")
    monkeypatch.setenv("DB_PATH", "/tmp/test.db")

    cfg = load_config()
    assert cfg.mailjet_api_key == "mj_key"
    assert cfg.token_secret_primary == "a" * 32
    assert cfg.token_secret_previous == ""
    assert cfg.subscription_ttl_days == 90
    assert cfg.max_plans_per_city == 10
    assert cfg.kofi_url == "https://ko-fi.com/jakubwaller"

def test_resend_api_key_is_optional(monkeypatch):
    """RESEND_API_KEY may be unset — fallback provider is opt-in."""
    for k, v in {
        "MAILJET_API_KEY":"x","MAILJET_API_SECRET":"x","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000",
        "TOKEN_SECRET_PRIMARY":"a"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"b"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"5",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"1",
        "DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    cfg = load_config()
    assert cfg.resend_api_key == ""

def test_load_config_missing_required(monkeypatch):
    # Establish a complete valid environment, then remove ONE required var,
    # so the test fails for the right reason regardless of the shell's state.
    for k, v in {
        "MAILJET_API_KEY":"x","MAILJET_API_SECRET":"x","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"x",
        "TOKEN_SECRET_PRIMARY":"a"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"b"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"5",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"1",
        "DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MAILJET_API_KEY")
    with pytest.raises(KeyError, match="MAILJET_API_KEY"):
        load_config()

def test_load_config_rejects_empty_required(monkeypatch):
    for k, v in {
        "MAILJET_API_KEY":"x","MAILJET_API_SECRET":"x","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"x",
        "TOKEN_SECRET_PRIMARY":"a"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"b"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"5",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"1",
        "DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ADMIN_TOKEN", "")  # explicit blank
    with pytest.raises(KeyError, match="ADMIN_TOKEN"):
        load_config()

def test_load_config_int_error_names_the_var(monkeypatch):
    for k, v in {
        "MAILJET_API_KEY":"x","MAILJET_API_SECRET":"x","MAILJET_FROM_EMAIL":"x@x",
        "MAILJET_FROM_NAME":"x","MAILJET_DAILY_QUOTA":"6000","RESEND_API_KEY":"x",
        "TOKEN_SECRET_PRIMARY":"a"*32,"TOKEN_SECRET_PREVIOUS":"",
        "ADMIN_TOKEN":"b"*32,"PUBLIC_BASE_URL":"https://x",
        "DEDUP_WINDOW_HOURS":"24","RATE_LIMIT_MINUTES":"15",
        "SUBSCRIPTION_TTL_DAYS":"90","RENEWAL_REMINDER_DAYS_BEFORE":"10",
        "MAX_PLANS_PER_CITY":"10","PARSER_CANARY_THRESHOLD_HOURS":"2",
        "SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR":"5",
        "SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY":"1",
        "DEVELOPER_EMAIL":"d@x","KOFI_URL":"https://k",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DEDUP_WINDOW_HOURS", "not-a-number")
    with pytest.raises(ValueError, match="DEDUP_WINDOW_HOURS"):
        load_config()
