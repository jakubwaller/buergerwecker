# Deployment

## Prerequisites

- Raspberry Pi running Docker + Docker Compose.
- Domain `termine.jakubwaller.eu` (or replacement) pointing to the Pi's
  public IP. Ports 80 and 443 forwarded to the Pi.
- USB HDD mounted at `/mnt/backup` (auto-mount via `/etc/fstab`).
- Mailjet and Resend accounts with verified sender domain.
- SPF / DKIM / DMARC records configured on `jakubwaller.eu` before any send.

## First deploy

1. Clone the repo to the Pi.
2. Copy `.env.example` to `.env` and fill in real secrets:
   - 32-byte `TOKEN_SECRET_PRIMARY` and `ADMIN_TOKEN` (e.g., `openssl rand -hex 32`).
   - Mailjet and Resend API keys.
   - Review the email-delivery settings (`EMAIL_PROVIDER_ORDER`,
     `RESEND_DAILY_QUOTA`, `MAILJET_HOURLY_QUOTA`, `MAILJET_DAILY_QUOTA`,
     `QUOTA_ALERT_THRESHOLD_PCT`) — see "Email delivery & quotas" below.
3. Verify the USB HDD is mounted at `/mnt/backup`.
4. `docker compose up -d`.
5. Watch logs: `docker compose logs -f`.
6. Verify healthz: `curl https://termine.jakubwaller.eu/healthz`.

## Email delivery & quotas

Notification digests and confirmation emails are sent in quota-aware batches
across two providers, so a traffic spike degrades gracefully instead of
failing:

- **Provider order** (`EMAIL_PROVIDER_ORDER`, default `mailjet,resend`).
  Digests try the first provider up to its remaining quota, then spill to the
  next. Mailjet-first routes volume through Mailjet so its account accrues the
  traffic needed to lift a new-sender throttle. Normally you leave this as-is;
  it's runtime-configurable (a `docker compose restart web poller`, no rebuild)
  only if you ever want a different primary.
- **Per-provider caps** (`RESEND_DAILY_QUOTA`, `MAILJET_HOURLY_QUOTA`,
  `MAILJET_DAILY_QUOTA`). Sends beyond the tighter of a provider's rolling
  windows are **deferred** to a later cycle, not dropped. Defaults match the
  free tiers (Resend 100/day; Mailjet 10/hour warm-up + 200/day). **When
  Mailjet lifts the throttle, raise these caps — not the provider order** (e.g.
  bump `MAILJET_HOURLY_QUOTA`); the daily cap then binds. Raise all of them
  after upgrading to a paid plan.
- **Sign-ups are never lost to quota.** If the confirmation email can't go out
  immediately, the registration is kept and the poller re-sends the
  confirmation on a later cycle (i.e. next day once quota resets); the user is
  told it may arrive later.
- **Low-quota alert.** When daily Resend usage crosses
  `QUOTA_ALERT_THRESHOLD_PCT` of `RESEND_DAILY_QUOTA`, or when notifications are
  deferred for lack of quota, `DEVELOPER_EMAIL` gets one alert per day. That is
  the cue to ask Mailjet to raise the throttle, upgrade to a paid plan, and/or
  raise the quota vars above.

Delivery mix over the last 7 days is visible on `/admin`.

## Load testing

`scripts/loadtest.py` measures sign-up write contention and `run_cycle` time at
N subscribers. It mocks the email providers — **no network, no real emails** —
so it is safe to run locally but must **never** be pointed at production (it
would pollute the live DB and burn email quota).

```
python scripts/loadtest.py                 # defaults (1k/10k/50k subscribers)
python scripts/loadtest.py --subs 50000    # single size
```

## Token-secret rotation

1. Set `TOKEN_SECRET_PREVIOUS=$TOKEN_SECRET_PRIMARY` in `.env`.
2. Generate a new secret: `openssl rand -hex 32` → `TOKEN_SECRET_PRIMARY`.
3. `docker compose restart web poller`.
4. Existing tokens remain valid; next rotation invalidates them.

## SMART monitoring (host-side systemd timer)

Install `smartmontools`. Add `/etc/systemd/system/termine-smart.service`:

```
[Unit]
Description=Termine-Notifier SMART check

[Service]
Type=oneshot
ExecStart=/path/to/termine-notifier/scripts/smartcheck.sh /dev/sda
```

And `/etc/systemd/system/termine-smart.timer`:

```
[Unit]
Description=Weekly SMART check

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl enable --now termine-smart.timer`.

## Off-Pi backup (secondary)

From your laptop:

```
rsync -av jakub@pi:/mnt/backup/ ~/termine-backups/
```

Cron weekly.

## IP-block runbook

If `terminvereinbarung.leipzig.de` starts returning 403 for the Pi's IP:

1. Stop the poller: `docker compose stop poller`.
2. Email the city: `verwaltung@leipzig.de`. Subject: "Anfrage zu
   Terminvereinbarung-Notifier". Explain: free notification service, no
   booking, polling once per minute (a handful of requests per minute; well
   under 1 req/sec even at the `MAX_PLANS_PER_CITY` cap), GDPR-compliant,
   open source at
   `github.com/jakubwaller/termine-notifier`. Ask if there is a way to
   continue operation that the city would accept.
3. Do NOT attempt to rotate IPs or use proxies — this is ethically
   worse than the polling itself and undermines the legal posture.
