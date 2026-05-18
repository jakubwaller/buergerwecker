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

def test_load_config_missing_required(monkeypatch):
    monkeypatch.delenv("MAILJET_API_KEY", raising=False)
    with pytest.raises(KeyError):
        load_config()
