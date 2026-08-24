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
    mailjet_monthly_quota: int
    brevo_api_key: str
    brevo_daily_quota: int
    brevo_monthly_quota: int
    sweego_api_key: str
    sweego_daily_quota: int
    sweego_monthly_quota: int
    mailjet_hourly_quota: int
    quota_alert_threshold_pct: int
    max_send_failures_per_address: int
    email_provider_order: tuple
    token_secret_primary: str
    token_secret_previous: str
    admin_token: str
    public_base_url: str
    dedup_window_hours: int
    rate_limit_minutes: int
    adaptive_rate_limit_max_multiplier: int
    subscription_ttl_days: int
    sensitive_subscription_ttl_days: int
    renewal_reminder_days_before: int
    max_plans_per_city: int
    parser_canary_threshold_hours: int
    subscribe_ratelimit_per_ip_per_hour: int
    subscribe_ratelimit_per_email_per_day: int
    contact_ratelimit_per_ip_per_hour: int
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
        brevo_api_key=os.environ.get("BREVO_API_KEY", ""),
        sweego_api_key=os.environ.get("SWEEGO_API_KEY", ""),
        # Free-tier send caps used for quota-aware delivery + alerting. Defaults
        # match the free tiers — Brevo 300/day, Sweego 100/day — and the
        # current Mailjet allowance (10/hour). Raise these after upgrading to a
        # paid plan.
        brevo_daily_quota=int(os.environ.get("BREVO_DAILY_QUOTA", "300")),
        sweego_daily_quota=int(os.environ.get("SWEEGO_DAILY_QUOTA", "100")),
        # Monthly caps are display-only (admin quota view): free tiers allow
        # Mailjet 6000/mo, Brevo 9000/mo, Sweego 3000/mo.
        mailjet_monthly_quota=int(os.environ.get("MAILJET_MONTHLY_QUOTA", "6000")),
        brevo_monthly_quota=int(os.environ.get("BREVO_MONTHLY_QUOTA", "9000")),
        sweego_monthly_quota=int(os.environ.get("SWEEGO_MONTHLY_QUOTA", "3000")),
        mailjet_hourly_quota=int(os.environ.get("MAILJET_HOURLY_QUOTA", "10")),
        quota_alert_threshold_pct=int(os.environ.get("QUOTA_ALERT_THRESHOLD_PCT", "80")),
        # How many provider content-rejections an address may collect before we
        # stop attempting it at all. 0 disables the cap (retry forever).
        max_send_failures_per_address=int(
            os.environ.get("MAX_SEND_FAILURES_PER_ADDRESS", "3")),
        # Order in which providers are tried for notification digests. Default
        # Mailjet-first so its account sees the traffic (needed to get the
        # new-sender throttle lifted); Brevo and Sweego absorb the overflow,
        # in order.
        email_provider_order=tuple(
            p.strip() for p in
            os.environ.get("EMAIL_PROVIDER_ORDER",
                           "mailjet,brevo,sweego").split(",")
            if p.strip()),
        token_secret_primary=_req("TOKEN_SECRET_PRIMARY"),
        token_secret_previous=os.environ.get("TOKEN_SECRET_PREVIOUS", ""),
        admin_token=_req("ADMIN_TOKEN"),
        public_base_url=_req("PUBLIC_BASE_URL"),
        dedup_window_hours=_req_int("DEDUP_WINDOW_HOURS"),
        rate_limit_minutes=_req_int("RATE_LIMIT_MINUTES"),
        # Ceiling on how far the adaptive cadence may stretch RATE_LIMIT_MINUTES
        # for subscribers whose filter is matching a lot of slots (see
        # cycle.adaptive_rate_limit_minutes). Set to 1 to disable adaptivity and
        # put everyone back on the flat floor — the kill switch, no redeploy.
        adaptive_rate_limit_max_multiplier=int(
            os.environ.get("ADAPTIVE_RATE_LIMIT_MAX_MULTIPLIER", "8")),
        subscription_ttl_days=_req_int("SUBSCRIPTION_TTL_DAYS"),
        # Special-category subscriptions (Art. 9 GDPR) expire sooner than
        # ordinary ones: the data is more sensitive, so it should exist for
        # less time. Optional with a default so existing deploys need no new
        # env var; the renewal link still works, it just renews for 30 days.
        sensitive_subscription_ttl_days=int(
            os.environ.get("SENSITIVE_SUBSCRIPTION_TTL_DAYS", "30")),
        renewal_reminder_days_before=_req_int("RENEWAL_REMINDER_DAYS_BEFORE"),
        max_plans_per_city=_req_int("MAX_PLANS_PER_CITY"),
        parser_canary_threshold_hours=_req_int("PARSER_CANARY_THRESHOLD_HOURS"),
        subscribe_ratelimit_per_ip_per_hour=_req_int("SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR"),
        subscribe_ratelimit_per_email_per_day=_req_int("SUBSCRIBE_RATELIMIT_PER_EMAIL_PER_DAY"),
        # Contact form (§ 5 DDG second contact channel). Optional with a
        # default so existing deploys don't need a new env var. Lower than the
        # subscribe limit: every submission costs a provider send, and the
        # form has no confirmation step to absorb abuse.
        contact_ratelimit_per_ip_per_hour=int(
            os.environ.get("CONTACT_RATELIMIT_PER_IP_PER_HOUR", "5")),
        developer_email=_req("DEVELOPER_EMAIL"),
        kofi_url=_req("KOFI_URL"),
        db_path=os.environ.get("DB_PATH", "/data/app.db"),
        catalog_sync_enabled=os.environ.get("CATALOG_SYNC_ENABLED", "0") == "1",
    )
