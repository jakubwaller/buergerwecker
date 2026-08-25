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
  Sweego.
- SPF / DKIM / DMARC records configured on `buergerwecker.de` and the domain
  validated in **every** configured provider (Mailjet, Brevo, Sweego)
  before any send — an unverified From domain makes a fallback provider reject
  mail. Mind the DMARC record when adding a provider: the domain must keep
  exactly one, so extend the existing record rather than letting a provider's
  automatic flow replace it. `REPLY_TO_EMAIL` points at a real mailbox on
  `jakubwaller.eu`; the From address itself doesn't receive mail.
- A signed data-processing agreement (DPA / AVV) with every processor the
  privacy page names — Mailjet, Brevo and Sweego, besides Cloudflare and
  netcup. The Datenschutz page states unconditionally
  that a DPA is in place with all listed providers, so concluding a new
  provider's DPA comes **before** deploying a version that lists it, not
  after.

## Redeploy

The normal path after a merge to `main`. The VPS holds a clone of this repo at
`~/termine-notifier` (containers `termine-notifier-web-1`, `-poller-1`, `-backup-1`):

```bash
ssh vps 'cd ~/termine-notifier && git pull --ff-only && docker compose up -d --build'
```

Then verify:

```bash
curl -sS https://buergerwecker.de/healthz
ssh vps 'cd ~/termine-notifier && docker compose ps'                    # three services Up
ssh vps 'cd ~/termine-notifier && docker compose logs --tail=50 poller'
```

`--build` is not optional. `app/` **and `catalog/` are copied into the web and poller images**
(`Dockerfile.web`, `Dockerfile.poller`), not mounted from the host — so a new city, or an edited
`scraper_config.json`, reaches production only through a rebuild. The one bind-mount is `./data`,
which holds `app.db`: it survives every rebuild, and deleting it to "start clean" destroys every
subscription.

Recreating `web` drops in-flight requests for a moment — there is one container and no rolling
deploy. Recreating `poller` cancels the cycle in progress, which is harmless: it re-reads its state
from the database on the next wake, and the idempotency record for mail already sent lives in the
database too, not in memory.

### A config-only change needs `up -d`, not `restart`

`.env` is read by Compose when a container is **created** and baked into that container's
environment. `docker compose restart` starts the *same* container, so it cannot see an edited file
— it reports success and changes nothing. Verified on the VPS 2026-08-25: after rewriting `.env`,
`restart` still printed the old value and `up -d` printed the new one.

```bash
ssh vps 'cd ~/termine-notifier && docker compose up -d web poller'   # after any .env edit
```

Nothing rebuilds if no source changed, so this is quick. It is what makes a changed
`EMAIL_PROVIDER_ORDER`, a rotated token secret or a raised quota actually take effect.

### Rollback

```bash
ssh vps 'cd ~/termine-notifier && git checkout <last-good-sha> && docker compose up -d --build'
```

The database is not versioned with the code. Schema changes are additive — `_add_missing_columns`
in `app/db.py` only ever runs `ALTER TABLE … ADD COLUMN` — so an older image tolerates a newer
database, and rolling back the code is safe on its own. Restoring a snapshot from `/mnt/backup` is
a separate and much bigger decision: it loses every sign-up since that snapshot was taken.

## Ingress: the live vhost is not in this repo

This stack runs no reverse proxy. `web` joins the external `web_proxy` network under the alias
`termine-web`, and the shared Caddy container — `elternschule-caddy-1`, owned by the
**elternschule-bot** stack in `~/elternschule` on the same host — terminates TLS and proxies to
that alias. The `buergerwecker.de`, `www.buergerwecker.de` and `termine.jakubwaller.eu` vhosts all
live in *that* repo's `Caddyfile`; changing any of them is that stack's deploy, not this one's, and
its runbook has the procedure (a `Caddyfile` edit there needs a container restart — `caddy reload`
reports success and reloads the old config).

The `Caddyfile` at the root of *this* repo is a leftover from when the project fronted itself.
Nothing reads it, and it has drifted: it proxies to `web:8000`, a name that does not resolve on the
shared network. Editing it does not change the live site.

## First deploy

1. Clone the repo to the host.
2. Copy `.env.example` to `.env` and fill in real secrets:
   - 32-byte `TOKEN_SECRET_PRIMARY` and `ADMIN_TOKEN` (e.g., `openssl rand -hex 32`).
   - The Mailjet API key + secret (required — Mailjet is the primary sender).
     `BREVO_API_KEY` and `SWEEGO_API_KEY` are optional: leave a key blank to
     disable that provider.
   - Review the email-delivery settings (`EMAIL_PROVIDER_ORDER`,
     `BREVO_DAILY_QUOTA`, `SWEEGO_DAILY_QUOTA`,
     `MAILJET_HOURLY_QUOTA`, `MAILJET_DAILY_QUOTA`,
     `QUOTA_ALERT_THRESHOLD_PCT`) — see "Email delivery & quotas" below.
3. Verify `/mnt/backup` exists (and, where it is a separate device, that it is
   mounted) — the compose backup service bind-mounts it.
4. `docker network create web_proxy` if no other stack has created it yet. The network is
   declared `external: true`, so Compose will not create it and the stack refuses to start
   without it.
5. Arrange ingress. This stack has no reverse proxy of its own (see "Ingress" above): on a fresh
   host, either bring up the elternschule-bot stack's Caddy, which already carries the
   `buergerwecker.de` vhost, or put any proxy in front that terminates TLS and forwards to the
   `termine-web` alias on `web_proxy`.
6. `docker compose up -d`.
7. Watch logs: `docker compose logs -f`.
8. Verify healthz: `curl https://buergerwecker.de/healthz`.

## Email delivery & quotas

Notification digests and confirmation emails are sent in quota-aware batches
across several providers, so a traffic spike degrades gracefully instead of
failing:

- **Provider order** (`EMAIL_PROVIDER_ORDER`, default `mailjet,brevo,sweego`).
  Digests try the first provider up to its remaining quota, then spill along
  the chain. Mailjet-first routes volume through Mailjet so its account accrues
  the traffic needed to lift a new-sender throttle. A provider named in the
  order without its API key configured is skipped. The order gates **every**
  send path — notification digests and the transactional fallback chain alike —
  so a configured key alone is inert until its provider is named here.
- **Prove a new provider before adding it to the order.** Verify the From
  domain in its dashboard, then send yourself one real mail through its API
  with the same payload the app builds (`app/mail.py`) and check it arrives
  with the `List-Unsubscribe`/`List-Unsubscribe-Post` headers intact (Brevo
  takes them as a plain `headers` passthrough — confirm on a real mailbox).
  For Sweego, whose API reference renders client-side and cannot be
  desk-checked, validate the payload shape with a `dry-run: true` send first,
  then one real send. (That dry-run is not optional ceremony: it is how we
  found, 2026-08-21, that Sweego rejects the RFC 8058 URL-only
  `List-Unsubscribe` header and requires the `<mailto:…>,<url>` form — which
  `app/mail.py` now builds for Sweego alone.) Only then add the provider to
  the order. Retiring a provider runs the same steps in reverse: drop it from
  the order (that removes it from both send paths), delete its API key from
  `.env`, and trim it from the Datenschutz page in the same deploy. The order
  is runtime-configurable (a `docker compose up -d web poller`, no rebuild — `restart`
  would leave the old order in place).
- **Per-provider caps** (`BREVO_DAILY_QUOTA`, `SWEEGO_DAILY_QUOTA`,
  `MAILJET_HOURLY_QUOTA`, `MAILJET_DAILY_QUOTA`). Sends
  beyond the tighter of a provider's rolling windows are **deferred** to a
  later cycle, not dropped. Defaults match the free tiers (Brevo 300/day —
  shared with any marketing sends on the account, and free-tier mail carries a
  Brevo footer logo; Sweego 100/day; Mailjet 10/hour warm-up +
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
  was two providers: 196/200 mailed as 98%, actually 196/300). Only the
  deferral half of the alert means someone went un-notified — the subject line
  says which fired. That mail is the cue to upgrade to a paid plan and raise the
  matching `*_DAILY_QUOTA`.

Delivery mix over the last 7 days is visible on `/admin`, along with an
**Email quota** section showing month-to-date and today's sends per provider
against `MAILJET_MONTHLY_QUOTA` / `BREVO_MONTHLY_QUOTA` /
`SWEEGO_MONTHLY_QUOTA` (display-only caps, free
tiers: 6000, 9000 and 3000/mo; Brevo and Sweego appear once their API
key is configured and they are named in `EMAIL_PROVIDER_ORDER`) — so you can
watch quota burn without logging into the
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

## Notification granularity

A tenant can set `notify_granularity` in its `scraper_config.json` to decide
what counts as one piece of news:

- `slot` (default, and what every tenant gets by omitting the key) — a distinct
  (day, time, office, service).
- `day` — (day, office, service), the time dropped. Only correct for a vendor
  that exposes the *earliest* free slot per office (TEVIS): there, the slot
  that appears the moment somebody books is the same inventory a minute later,
  and mailing about it again tells the subscriber nothing new. On a vendor that
  lists real inventory (smartCJM), `day` would withhold genuine second chances
  — do not set it.

**Only `muenster-kfz` is set to `day` today**, and the other 30 TEVIS tenants
deliberately are not. `day` cannot distinguish the earliest slot moving forward
(booked — nothing new to say) from it moving back (a cancellation — worth
saying), so once a day has been reported, an earlier slot on that day stays
quiet until housekeeping prunes the row after 7 days. Münster-KFZ releases
same-day slots each morning and nothing further ahead, so a day is reported
once and never revisited and the limitation cannot bite. On a tenant whose
horizon is weeks — Braunschweig, Mainz, Kiel — the earliest slot can walk out
to a distant date and a cancellation pull it back, and that is exactly the mail
a subscriber wants. **Teach the key about earlier-than-last-told before
enabling `day` anywhere with a multi-day horizon.**

Whether a tenant shows only its earliest slot is measurable rather than assumed:
`SELECT city, MAX(n_slots) FROM availability_samples WHERE location_uuid <> ''
GROUP BY city` — TEVIS tenants read exactly 1, smartCJM tenants read hundreds
or thousands.

**Changing it re-notifies once.** The old and new keys are different values in
`seen_slots`, so on the first cycle after the deploy every affected subscriber
with a currently-matching slot gets one digest, and only then does the new
suppression apply. That is a one-time burst of up to one mail per affected
subscriber — deploy a granularity change when the mail pool has headroom, not
during a saturated morning. Check the pool first: `/admin` → Email quota.

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
3. `docker compose up -d web poller` — **not `restart`**, which cannot see the edited
   `.env` (see "A config-only change needs `up -d`" above).
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
