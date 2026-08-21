# Deployment

## Prerequisites

- A Linux host running Docker + Docker Compose. The live deployment is a
  netcup VPS; anything always-on works.
- Web domain `buergerwecker.de` (or replacement) resolving to that host. The
  live records are proxied through Cloudflare. On a home server you would also
  have to forward ports 80 and 443 on the router; on a VPS they are simply open.
- A backup directory at `/mnt/backup`. **On the VPS this is a plain directory on
  the root filesystem, so the snapshots sit on the same disk as the live
  database** — it protects against a bad write or a bad deploy, not against
  losing the disk. The off-host copy is what covers that; see "Off-host backup"
  below. (On the Pi this used to be a USB HDD auto-mounted via `/etc/fstab`.)
- Email provider accounts with verified sender domain: Mailjet, Brevo and
  Sweego (plus Resend while it is still in the chain — it is transitional and
  on its way out).
- SPF / DKIM / DMARC records configured on `buergerwecker.de` and the domain
  validated in **every** configured provider (Mailjet, Brevo, Sweego, Resend)
  before any send — an unverified From domain makes a fallback provider reject
  mail. Mind the DMARC record when adding a provider: the domain must keep
  exactly one, so extend the existing record rather than letting a provider's
  automatic flow replace it. `REPLY_TO_EMAIL` points at a real mailbox on
  `jakubwaller.eu`; the From address itself doesn't receive mail.

## First deploy

1. Clone the repo to the host.
2. Copy `.env.example` to `.env` and fill in real secrets:
   - 32-byte `TOKEN_SECRET_PRIMARY` and `ADMIN_TOKEN` (e.g., `openssl rand -hex 32`).
   - Mailjet, Brevo, Sweego and Resend API keys.
   - Review the email-delivery settings (`EMAIL_PROVIDER_ORDER`,
     `BREVO_DAILY_QUOTA`, `SWEEGO_DAILY_QUOTA`, `RESEND_DAILY_QUOTA`,
     `MAILJET_HOURLY_QUOTA`, `MAILJET_DAILY_QUOTA`,
     `QUOTA_ALERT_THRESHOLD_PCT`) — see "Email delivery & quotas" below.
3. Verify `/mnt/backup` exists (and, where it is a separate device, that it is
   mounted) — the compose backup service bind-mounts it.
4. `docker compose up -d`.
5. Watch logs: `docker compose logs -f`.
6. Verify healthz: `curl https://buergerwecker.de/healthz`.

## Email delivery & quotas

Notification digests and confirmation emails are sent in quota-aware batches
across several providers, so a traffic spike degrades gracefully instead of
failing:

- **Provider order** (`EMAIL_PROVIDER_ORDER`, default `mailjet,resend`).
  Digests try the first provider up to its remaining quota, then spill along
  the chain. Mailjet-first routes volume through Mailjet so its account accrues
  the traffic needed to lift a new-sender throttle. A provider named in the
  order without its API key configured is skipped. **Recommended transition
  order once the Brevo and Sweego accounts are set up:
  `mailjet,brevo,sweego,resend`** — the EU providers absorb the overflow and
  Resend (US, being phased out) only sees traffic when everything else is
  spent; drop `resend` from the order once the new providers are proven. It's
  runtime-configurable (a `docker compose restart web poller`, no rebuild).
- **Per-provider caps** (`BREVO_DAILY_QUOTA`, `SWEEGO_DAILY_QUOTA`,
  `RESEND_DAILY_QUOTA`, `MAILJET_HOURLY_QUOTA`, `MAILJET_DAILY_QUOTA`). Sends
  beyond the tighter of a provider's rolling windows are **deferred** to a
  later cycle, not dropped. Defaults match the free tiers (Brevo 300/day —
  shared with any marketing sends on the account, and free-tier mail carries a
  Brevo footer logo; Sweego 100/day; Resend 100/day; Mailjet 10/hour warm-up +
  200/day). **When Mailjet lifts the throttle, raise these caps — not the
  provider order** (e.g. bump `MAILJET_HOURLY_QUOTA`); the daily cap then
  binds. Raise all of them after upgrading to a paid plan.
- **Brevo and Sweego send one message per API call** — neither documents batch
  atomicity, and the retry logic is only safe with per-message verdicts or a
  provably all-or-nothing batch. A few hundred mails a day fit comfortably in
  single calls.
- **When the whole pool is spent, digests are deferred, not dropped.** The
  leftovers have their idempotency claims released and their slots left
  unrecorded, so the next cycle re-sends them with a fresh `cycle_id`. Two
  consequences worth knowing: a slot taken in the meantime is simply gone (the
  digest never goes out, correctly), and the deferred count is written to
  `email_deferral_counts` per UTC day — visible on `/admin`, in the ops summary,
  and in the alert mail. That counter is the **only** record that a subscriber
  was not told about a slot; nothing else persists it.
- **The deferred tail rotates.** Batches are filled in list order, so without
  care the same subscribers land at the back of every saturated cycle.
  `flush_digests` sorts by `last_notified_at` (never-notified first), and a
  deferred digest never stamps that column — so whoever was passed over leads
  the next cycle.
- **Sign-ups are never lost to quota.** If the confirmation email can't go out
  immediately, the registration is kept and the poller re-sends the
  confirmation on a later cycle (i.e. next day once quota resets); the user is
  told it may arrive later.
- **Low-quota alert.** When the **combined** rolling-24h usage across every
  provider that can actually send crosses `QUOTA_ALERT_THRESHOLD_PCT` of the
  summed daily caps, or when notifications are deferred for lack of quota,
  `DEVELOPER_EMAIL` gets one alert per day. Combined, not per-provider: with
  Mailjet-first routing a batch only reaches the next provider in the chain
  once Mailjet's headroom is 0, so "Mailjet at 98%" is what a busy day looks
  like while a third of the pool is still free (2026-08-19, back when the pool
  was Mailjet + Resend: 196/200 mailed as 98%, actually 196/300). Only the
  deferral half of the alert means someone went un-notified — the subject line
  says which fired. That mail is the cue to upgrade to a paid plan and raise the
  matching `*_DAILY_QUOTA`.

Delivery mix over the last 7 days is visible on `/admin`, along with an
**Email quota** section showing month-to-date and today's sends per provider
against `MAILJET_MONTHLY_QUOTA` / `BREVO_MONTHLY_QUOTA` /
`SWEEGO_MONTHLY_QUOTA` / `RESEND_MONTHLY_QUOTA` (display-only caps, free
tiers: 6000, 9000, 3000 and 3000/mo; Brevo and Sweego appear once their API
key is configured) — so you can watch quota burn without logging into the
provider dashboards. Counts come from the app's own durable
`email_send_counts` table (UTC days, an approximation of each provider's reset
cycle) and only include mail this app sent. Each row also shows the **rolling
24h** figure, and a combined row totals it: that rolling number is what actually
gates a send, and it deliberately disagrees with the UTC-day one — just after
UTC midnight "today" is near 0 while the gate still counts last evening.

## Polling cadence

The poller wakes once a minute, but each tenant can set
`poll_interval_seconds` in its `catalog/<tenant>/scraper_config.json` to be
polled less often (e.g. 180 for a city that mandates one request per three
minutes). Skipped cycles leave the tenant's counters, canary, and
last-polled timestamp untouched.

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

Only for a host with a real disk to read — this was set up on the Raspberry Pi
and is **not installed on the VPS**, whose virtual disk exposes nothing useful
to `smartctl`. Kept here for whenever the project runs on physical hardware
again.

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

## Off-host backup (secondary)

`/mnt/backup` shares a disk with the live database, so a copy has to leave the
host. A scheduled pull from a workstation does it — daily, over scp, keeping
every snapshot it has ever fetched:

```
scp 'vps:/mnt/backup/app-*.db.gz' <local-snapshot-dir>/
```

Never let the puller delete: the point is to survive a deletion on the server,
which a mirroring sync would faithfully replicate.

## IP-block runbook

If `terminvereinbarung.leipzig.de` starts returning 403 for the host's IP:

1. Stop the poller: `docker compose stop poller`.
2. Email the city: `verwaltung@leipzig.de`. Subject: "Anfrage zu
   Terminvereinbarung-Notifier". Explain: free notification service, no
   booking, polling once per minute (a handful of requests per minute; well
   under 1 req/sec even at the `MAX_PLANS_PER_CITY` cap), GDPR-compliant,
   open source at
   `github.com/jakubwaller/buergerwecker`. Ask if there is a way to
   continue operation that the city would accept.
3. Do NOT attempt to rotate IPs or use proxies — this is ethically
   worse than the polling itself and undermines the legal posture.
