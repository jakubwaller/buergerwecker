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
- **A deferral also says which wall it hit.** `email_deferrals` logs each
  deferring cycle with `wall` = `hourly` (Mailjet's warm-up throttle — the next
  cycles clear it), `daily` (the combined pool — nothing moves until the rolling
  24h window frees a slot, and the appointment is usually gone by then) or
  `outage` (a provider with room failed; the next cycle retries), plus
  `frees_at`, the earliest moment a retry can succeed. `/admin` shows the last
  event next to the counter and the ops summary splits the day's total by wall:
  "3 deferred" against the hourly wall is noise, against the daily wall it is
  the case for a tighter per-subscriber daily cap.
- **Each subscriber gets at most `MAX_DIGESTS_PER_SUBSCRIBER_PER_DAY` digests
  per rolling 24h** (default 2, `0` disables — env only, no redeploy). The
  adaptive cadence only stretches the interval and disengages on the
  earliest-slot-only tenants that generate most of the volume, so before the
  cap the mean was 4.6 digests per notified subscriber per day with more than
  half at 5+. A held digest is **dropped, not queued**: nothing is recorded as
  seen, so the first cycle after the subscriber's window frees re-evaluates the
  live slots and sends what is still open. `/admin` shows who is capped right
  now, how many subscribers were held today (`digest_cap_holds`, one row per
  subscriber per UTC day, 90-day prune) and the digests-per-subscriber number
  the cap exists to move; the ops summary carries the same line. Deliveries
  are counted in `digest_deliveries` (7-day prune), seeded once at migration
  from `seen_slots` so the cap binds from the first cycle.
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

## Delivery feedback webhooks (bounces & spam complaints)

The send path only ever learns about failures a provider can report
synchronously (an HTTP 400/422 on the send call). Everything else — a mailbox
that does not exist, a recipient pressing "spam" — is reported minutes later,
over a webhook, and is invisible without one. Left unconfigured, this service
mails dead addresses forever and the bounce rate is charged against the sending
domain until large receivers throttle it. Buying more provider quota does not
fix a throttled domain.

**Endpoint:** `POST https://buergerwecker.de/webhooks/<provider>/<secret>`,
where `<provider>` is `mailjet`, `brevo` or `sweego` and `<secret>` is
`WEBHOOK_SECRET`. Two of the three providers sign nothing, and Mailjet's own
documented answer is to put credentials in the endpoint URL, so a secret path
segment is the one mechanism all three can be configured with.

### Env vars

```
WEBHOOK_SECRET=<32+ random chars>          # empty disables the endpoint (503)
SWEEGO_WEBHOOK_SECRET=<from Sweego>        # optional, adds HMAC verification
SOFT_BOUNCE_SUPPRESS_THRESHOLD=5           # soft bounces before retiring an address
```

Generate the secret with `openssl rand -hex 24`. Rotating it means changing the
env var and the URL in all three dashboards; until both sides match the
endpoint answers 403 and events are lost, so do it in one sitting.

### Per-provider dashboard setup

- **Mailjet** — Account settings → Event notifications (Event API). Set the URL
  for `bounce`, `blocked`, `spam`, `unsub` and `sent`. Leave "group events"
  on; the parser handles both a single object and the grouped array.
- **Brevo** — Transactional → Settings → Webhooks. Enable `hard_bounce`,
  `soft_bounce`, `invalid_email`, `blocked`, `spam`, `unsubscribed`, `error`
  and `delivered`. One event per request.
- **Sweego** — Webhooks → new webhook, attached to the `buergerwecker.de`
  domain. Enable hard bounce, soft bounce, spam-complaints, list-unsubscribe
  and delivered. Copy the webhook secret into `SWEEGO_WEBHOOK_SECRET`
  **verbatim**, including a `whsec_` prefix if the dashboard shows one — it is
  verified as HMAC-SHA256 over `{id}.{timestamp}.{raw body}`, and both the
  base64 and the literal reading of the secret are accepted, because a
  misread here is not an error at startup but a silent 403 on every delivery.
  Leave it empty and only the URL secret gates the endpoint.

`delivered`/`sent` matter as much as the failures: they clear a soft-bounce run
so a temporarily full mailbox does not creep up to the threshold over months.

### What an event does

| event | effect |
| --- | --- |
| hard bounce, invalid address, provider blocklisted the address | address suppressed for good, all its subscriptions ended |
| spam complaint | same |
| unsubscribe (provider-side) | subscriptions ended, address NOT suppressed — signing up again is theirs to do |
| soft bounce / transient error | counted; suppressed only after `SOFT_BOUNCE_SUPPRESS_THRESHOLD` in a row |
| delivered / sent | clears the soft-bounce counter (never lifts a suppression) |

A Mailjet `blocked` event only retires the address when its `error_related_to`
blames the address (`recipient`, `domain`, `mailbox`, `mailbox_inactive`). A
`blocked` for our own content or a provider fault is counted as soft — otherwise
one bad template would retire every subscriber it was sent to.

### Verifying after deploy

```bash
# 403 on a wrong secret, 404 on an unknown provider, 200 on a real payload.
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://buergerwecker.de/webhooks/brevo/WRONG -d '{}' -H 'Content-Type: application/json'
```

Then send yourself a test mail and check `/admin` → **Deliverability**. That
section is the health check that matters: it shows the 30-day complaint rate
(Gmail and Yahoo throttle a bulk sender above 0.30%), the hard-bounce rate, how
many addresses are suppressed, and **when each provider last reported anything**.
A provider that has gone silent for 48h is flagged, because a rate of 0.00% with
a dead webhook and a rate of 0.00% with a healthy one look identical in the
numbers, and the first is the dangerous one.

### Retention, and getting back off the list

Retention splits by reason, and so does the way back.

**Bounce** suppressions age out with the subscription that justified them
(`_prune_suppressions`, the same 30-day clock as `_purge_hard`), and a new
sign-up lifts one immediately (`repo.clear_delivery_block`, which also resets
the `email_failures` counter). A bounce only ever claimed the mailbox was
broken *then*; somebody typing that address into the form now is the evidence
it works. If it is still broken, one bounce re-suppresses it. No manual step.

**Complaint** suppressions run on their own clock,
`COMPLAINT_RETENTION_DAYS` (365), independent of any subscription — a feedback
loop can report late, so a complaint may arrive for an address whose
subscription was already purged, and tying it to the subscription would delete
the one suppression that matters most within 24h. It is not indefinite:
Art. 5(1)(e) wants a stated period, and since this service is double opt-in
only a lapsed entry can at worst cost one confirmation mail to somebody who
went back to the site and asked for it.

A complaint is **not** lifted by signing up again — that is the person's own
verdict and a form submission is not their word for it. Instead the sign-up
form says so, with a link to `/kontakt`, rather than accepting the sign-up and
dropping the confirmation into the suppression list while the page claims it
was sent. To lift one by hand after they ask:

```bash
sqlite3 ~/termine-notifier/data/app.db \
  "DELETE FROM email_suppressions WHERE email='<address>' AND reason='complaint';"
```

Nothing else needs touching: their subscriptions were already ended when the
complaint arrived, so they sign up again as a new subscriber.

Both send paths honour the list — `send_batch` via `_dead_addresses` and the
transactional `send()` via its own check. Do not add a third.

## Subscription term & the "still looking?" check-in

A subscription's term is short on purpose. Most people never click *Abmelden* after they have
booked, and a booked person drawing two digests a day is both wasted quota and the shape of mail
that draws spam complaints. So instead of waiting for an unsubscribe, the service asks:

- `SUBSCRIPTION_TTL_DAYS` (14) — the term from sign-up or renewal.
- `RENEWAL_REMINDER_DAYS_BEFORE` (3) — this many days before the term ends, housekeeping sends one
  *Suchst du noch einen Termin?* mail with two one-click answers: *weiter* (`/renew`, starts a new
  term) and *hab einen* (`/unsubscribe`), plus a *Filter anpassen* link for people holding out for
  particular days or times. Once per term; `/renew` re-arms it.
- No answer → the subscription expires: digests stop, nothing is deleted yet.
- `EXPIRED_GRACE_DAYS` (optional, 14) — how long an expired subscription stays paused with its
  *weiter* link still working before housekeeping deletes it. `/admin` shows the count as
  **Paused**.
- `SENSITIVE_SUBSCRIPTION_TTL_DAYS` (optional, 30) is the *shorter* term for Art. 9 subscriptions
  and never exceeds the ordinary one — with the ordinary term at 14, both are 14.

**Lowering the term reaches existing subscriptions.** The configured term is a ceiling on
everyone's remaining time, not just a default for new sign-ups: the next housekeeping run pulls
every longer expiry in to `now + SUBSCRIPTION_TTL_DAYS` (it never pushes one out). So after a
deploy that drops the term from 90 to 14, expect one check-in mail per active subscription in a
single day, `SUBSCRIPTION_TTL_DAYS − RENEWAL_REMINDER_DAYS_BEFORE` days later (11 with the
defaults), on top of the digests — a burst sized like the active base, counted against the
providers' daily pool like any other send. Digests to anyone who does not answer stop three days
after that.

The Datenschutz page reads all three periods from config, so it cannot promise a term the
deploy no longer keeps.

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
quiet until housekeeping prunes the row after 7 days.

That is a real trade even on Münster-KFZ, whose horizon is **not** same-day:
probed live on 2026-08-25, service 2407 stood a day out and 2408 sixteen days
out. It is accepted there because the alternative is measured at 4-8 mails per
subscriber per day, every one of them a different time on a day they had
already been told about, and because the earliest slot usually moves *within* a
day (a day holds many slots, so a booking rarely exhausts it). The loss is
confined to a day that was reported, vanished, and reopened within the week.
**Before enabling `day` on any further tenant, teach the key
"earlier than last told" rather than re-making this trade by hand.**

Whether a tenant shows only its earliest slot is measurable rather than assumed:
`SELECT city, MAX(n_slots) FROM availability_samples WHERE location_uuid <> ''
GROUP BY city` — TEVIS tenants read exactly 1, smartCJM tenants read hundreds
or thousands.

**Changing it re-notifies once unless you backfill first.** The old and new keys
are different values in `seen_slots`, so on the first cycle after the deploy
every affected subscriber with a currently-matching slot gets one digest — which
is one last round of exactly the noise the setting removes.

`scripts/backfill_day_keys.py` prevents that. The stored hashes cannot be read
back, but the tenant's slot space (date x time x office x service) is small
enough to enumerate and match, which recovers every date each subscriber has
already been told about and writes the day key for it. Run the dry run first and
check `unrecognized` is at or near zero — each unrecognized row is one
subscriber who may still get a single redundant mail:

**The backfill has to run between the build and the restart**, and the ordering
below is the only one that works. `docker exec` into the *running* poller cannot
do it: that container is the old image, which has neither the script nor
`Slot.day_hash`. And once `up -d` has recreated the poller there is no window to
catch — it sleeps to the next minute boundary and runs a cycle, so the burst has
already gone out. `docker compose run` threads the needle: it uses the freshly
built image while the old poller keeps running, unchanged, on the old
granularity.

```bash
ssh vps 'cd ~/termine-notifier && git pull --ff-only && docker compose build poller'

# Dry run first: check `unrecognized` is at or near zero before applying.
ssh vps 'cd ~/termine-notifier && docker compose run --rm poller \
    python scripts/backfill_day_keys.py <tenant> --db /data/app.db'
ssh vps 'cd ~/termine-notifier && docker compose run --rm poller \
    python scripts/backfill_day_keys.py <tenant> --db /data/app.db --apply'

ssh vps 'cd ~/termine-notifier && docker compose up -d --build'
```

The keys are inert until the new poller asks for them, so the gap between the
backfill and `up -d` is safe to take at whatever pace you like. The script is
idempotent — rerun it freely — and it waits out the live poller's write lock
rather than failing on it. On a rerun the keys from the first run show up under
`already day keys`, not `unrecognized`; `written: 0` is the expected result.

Measured on muenster-kfz on 2026-08-25: 557 of 557 rows recovered, 231 day keys
written, first-cycle burst 35 digests → 2 (those 2 being subscribers genuinely
never told about that date). If you deploy *without* the backfill anyway, do it
when the pool has headroom and check `/admin` → Email quota first.

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
