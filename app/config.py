from __future__ import annotations
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Config:
    mailjet_api_key: str
    mailjet_api_secret: str
    mailjet_from_email: str
    mailjet_from_name: str
    mailjet_daily_quota: int
    resend_api_key: str
    resend_daily_quota: int
    mailjet_hourly_quota: int
    quota_alert_threshold_pct: int
    email_provider_order: tuple
    token_secret_primary: str
    token_secret_previous: str
    admin_token: str
    public_base_url: str
    dedup_window_hours: int
    rate_limit_minutes: int
    subscription_ttl_days: int
    renewal_reminder_days_before: int
    max_plans_per_city: int
    parser_canary_threshold_hours: int
    subscribe_ratelimit_per_ip_per_hour: int
    subscribe_ratelimit_per_email_per_day: int
    developer_email: str
    kofi_url: str
    db_path: str
    catalog_sync_enabled: bool

def _req(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise KeyError(f"Missing required env var: {key}")
    return val

def _req_int(key: str) -> int:
    raw = _req(key)
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Env var {key} must be an integer, got: {raw!r}")

def load_config() -> Config:
    return Config(
        mailjet_api_key=_req("MAILJET_API_KEY"),
        mailjet_api_secret=_req("MAILJET_API_SECRET"),
        mailjet_from_email=_req("MAILJET_FROM_EMAIL"),
        mailjet_from_name=_req("MAILJET_FROM_NAME"),
        mailjet_daily_quota=_req_int("MAILJET_DAILY_QUOTA"),
        resend_api_key=os.environ.get("RESEND_API_KEY", ""),
        # Free-tier send caps used for quota-aware delivery + alerting. Defaults
        # match Resend's free tier (100/day) and the current Mailjet allowance
        # (10/hour). Raise these after upgrading to a paid plan.
        resend_daily_quota=int(os.environ.get("RESEND_DAILY_QUOTA", "100")),
        mailjet_hourly_quota=int(os.environ.get("MAILJET_HOURLY_QUOTA", "10")),
        quota_alert_threshold_pct=int(os.environ.get("QUOTA_ALERT_THRESHOLD_PCT", "80")),
        # Order in which providers are tried for notification digests. Default
        # Mailjet-first so its account sees the traffic (needed to get the
        # new-sender throttle lifted); Resend absorbs the overflow.
        email_provider_order=tuple(
            p.strip() for p in
            os.environ.get("EMAIL_PROVIDER_ORDER", "mailjet,resend").split(",")
            if p.strip()),
        token_secret_primary=_req("TOKEN_SECRET_PRIMARY"),
        token_secret_previous=os.environ.get("TOKEN_SECRET_PREVIOUS", ""),
        admin_token=_req("ADMIN_TOKEN"),
        public_base_url=_req("PUBLIC_BASE_URL"),
        dedup_window_hours=_req_int("DEDUP_WINDOW_HOURS"),
        rate_limit_minutes=_req_int("RATE_LIMIT_MINUTES"),
        subscription_ttl_days=_req_int("SUBSCRIPTION_TTL_DAYS"),
        renewal_reminder_days_before=_req_int("RENEWAL_REMINDER_DAYS_BEFORE"),
        max_plans_per_city=_req_int("MAX_PLANS_PER_CITY"),
        parser_canary_threshold_hours=_req_int("PARSER_CANARY_THRESHOLD_HOURS"),
        subscribe_ratelimit_per_ip_per_hour=_req_int("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR"),
        subscribe_ratelimit_per_email_per_day=_req_int("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY"),
        developer_email=_req("DEVELOPER_EMAIL"),
        kofi_url=_req("KOFI_URL"),
        db_path=os.environ.get("DB_PATH", "/data/app.db"),
        catalog_sync_enabled=os.environ.get("CATALOG_SYNC_ENABLED", "0") == "1",
    )
