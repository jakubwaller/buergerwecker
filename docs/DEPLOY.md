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

Every push to `main` (i.e. every squash-merge) deploys automatically:
`.github/workflows/deploy.yml` runs the command below over SSH, checks the VPS
checkout is at the pushed commit, and probes `/healthz`. Watch the Deploy run
go green instead of deploying by hand; the manual path below remains the
fallback when the action is red or the secrets aren't set.

### Auto-deploy secrets

Three repository secrets (Settings → Secrets and variables → Actions):

- `DEPLOY_SSH_KEY` — private key whose public half is in the VPS user's
  `~/.ssh/authorized_keys`. Make a dedicated one rather than reusing your own:
  `ssh-keygen -t ed25519 -f deploy_key -N "" -C buergerwecker-deploy`, then
  `ssh-copy-id -i deploy_key.pub vps` and paste the contents of `deploy_key`.
- `DEPLOY_SSH_TARGET` — `user@host` of the VPS.
- `DEPLOY_SSH_PORT` (optional) — sshd port when it isn't 22.
- `DEPLOY_KNOWN_HOSTS` (optional but recommended) — output of
  `ssh-keyscan -p <port> <host>`, pinning the VPS host key. Without it the
  workflow trusts the host key on first use, every run.

### Manual redeploy

The fallback path after a merge to `main`. The VPS holds a clone of this repo at
`~/buergerwecker` (containers `buergerwecker-web-1`, `-poller-1`, `-backup-1`):

```bash
ssh vps 'cd ~/buergerwecker && git pull --ff-only && docker compose up -d --build'
```

Then verify:

```bash
curl -sS https://buergerwecker.de/healthz
ssh vps 'cd ~/buergerwecker && docker compose ps'                    # three services Up
ssh vps 'cd ~/buergerwecker && docker compose logs --tail=50 poller'
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
ssh vps 'cd ~/buergerwecker && docker compose up -d web poller'   # after any .env edit
```

Nothing rebuilds if no source changed, so this is quick. It is what makes a changed
`EMAIL_PROVIDER_ORDER`, a rotated token secret or a raised quota actually take effect.

### Rollback

```bash
ssh vps 'cd ~/buergerwecker && git checkout <last-good-sha> && docker compose up -d --build'
```

A `<last-good-sha>` from before the `./secrets` mount (PR #105) brings back a `docker-compose.yml`
without it, so comment the `*_FILE` lines out of `.env` first, or `web` and `poller` stop at start
on the missing key file (see "Push delivery").

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
- **Under pressure the cap tightens for everyone before anyone is
  deferred** (`MAIL_POOL_PRESSURE_PCT`, default 80, and
  `MAIL_CAP_UNDER_PRESSURE`, default 1; `0` turns it off, and so does
  turning the ordinary cap off). The pool is the
  free provider chain and there is no paid capacity behind it, so when the
  combined rolling-24h usage reaches the percentage, every subscriber's
  daily cap, **mail and app alike**, drops to the tightened value for as
  long as the pressure lasts. A thinner day for everyone is the fair
  degradation; a deferral is one person not told at all. App (push)
  subscribers have no pool of their own and are tightened all the same: the
  same cap on both channels is the rule, and a tighter day for mail alone
  would make the app the way to more notifications. The poller logs
  `mail pool under pressure, every subscriber (mail and app) capped` on each
  cycle it applies, and `/admin` and the ops summary show the tightened cap
  while it is on.
- **When mail is out of quota, push waits behind it: one queue, one wall**
  (`digest._behind_the_mail_wall`). Every digest of a cycle, mail and push,
  stands in one queue, longest-waiting first. Before either batch starts,
  the pool's remaining room (each provider's headroom, the same numbers
  `send_batch` fills) says how far down the queue mail gets; the first mail
  digest past it is the wall. It and the mail behind it are deferred for
  quota, and the push digests behind it wait with them, unrecorded, so they
  lead the next cycle in the same order. Push digests ahead of the wall go
  out: they waited longer than anyone the quota turned away. Neither channel
  gets ahead of the other. A mail digest to a dead address takes no room;
  an outage defers more mail than predicted without holding push back (the
  wall is the quota's). The poller logs `push: N digest(s) wait behind the
  mail quota` when it holds any. **Hourly walls only:** when the daily
  windows are what bind (`mail.daily_room` no more than the pool's room),
  the deferred mail waits for the rolling 24h window, and push goes out —
  holding it then frees no quota, and anyone who exhausts the mail pool (a
  flood of confirmation mails, say) would silence the app for the rest of
  the day as well. The push budget (*Push delivery* below)
  does **not** hold mail back: devices are free to mint, and a push queue
  that held the mail queue would hand them a lever over the website's
  subscribers.
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

Two things that line cannot know on its own:

- **"never reported" counts sends over the last 48h regardless of when the
  dashboard was wired.** A fallback leg whose last send predates the webhook
  reads as unwired until that send ages out of the window; the *last* timestamp
  next to the count is what to compare against. Sweego, last in the chain, can
  go days without sending once the per-subscriber cap holds the volume — its
  webhook is then unverified, not broken. Verify it from the Sweego dashboard's
  own delivery counters, or wait for its next real send.
- **A post that carries the right URL secret but fails the signature** is
  answered 403, logged (`bad signature`), and shown on `/admin` as *rejected for
  a bad signature* (`webhook_rejected_sweego` / `last_webhook_rejected_at_sweego`
  in `meta`; an ops-summary anomaly while the last one is under 48h old). That
  is the diagnosis for a misread `SWEEGO_WEBHOOK_SECRET`, which would otherwise
  be indistinguishable from a webhook nobody wired up. A wrong URL secret is
  not counted — anyone can post one.

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
sqlite3 ~/buergerwecker/data/app.db \
  "DELETE FROM email_suppressions WHERE email='<address>' AND reason='complaint';"
```

Nothing else needs touching: their subscriptions were already ended when the
complaint arrived, so they sign up again as a new subscriber.

Both send paths honour the list — `send_batch` via `_dead_addresses` and the
transactional `send()` via its own check. Do not add a third.

## Push delivery (the app)

App subscribers are notified by push instead of mail: the poller sends through
Apple's APNs (iPhone) and Google's FCM (Android) from the same digest flush,
under the same idempotency key and the same seen_slots bookkeeping. Without the
env vars below nothing changes — a push-only subscription would simply never
be delivered (and its slots stay unseen), so set them before the app is
released, not after.

### Env vars

```
APNS_TEAM_ID=<10-char Apple team id>        # Developer account → Membership
APNS_KEY_ID=<10-char key id>                # Keys → the APNs auth key (.p8)
APNS_KEY_P8_FILE=/run/secrets/apns.p8       # or APNS_KEY_P8=<PEM inline>
APNS_TOPIC=<bundle id of the app>
APNS_SANDBOX=0                              # 1 only for Xcode development builds
FCM_SERVICE_ACCOUNT_JSON_FILE=/run/secrets/fcm.json   # or ..._JSON=<inline>
PLAY_INTEGRITY_REQUIRED=1                   # 0 only on a local/dev server; production must be 1
PLAY_INTEGRITY_SERVICE_ACCOUNT_JSON_FILE=/run/secrets/play-integrity.json   # or ..._JSON=<inline>; blank = the FCM account
PUSH_TTL_SECONDS=1800                       # how long a relay holds a push
PUSH_BUDGET_PER_CYCLE=200                   # digest pushes tried per cycle (0 = no bound)
PUSH_BUDGET_SECONDS=20                      # no new push after this many seconds (0 = no bound)
```

**Play Integrity (Android).** `POST /devices` with a new FCM token and `PUT
/device` with a new FCM token need an `integrity_token`; `web` sends it to
Google's `decodeIntegrityToken` and refuses (400 `integrity_missing`, 403
`integrity_failed`, 503 `integrity_unavailable`) unless it shows the Play-
recognised app on a device that meets device integrity, minted for that very
token and within ten minutes. It fails closed: with no credentials, or with
Google unreachable, Android registration stops (`integrity: ...` in the web
log); iOS and tokens already registered are unaffected. One-time setup, in
the owner's hands: link the Firebase project's Google Cloud project in the
Play Console (App integrity), enable the Play Integrity API in it, and give
the service account in use (the FCM one, or a separate one in
`PLAY_INTEGRITY_SERVICE_ACCOUNT_JSON[_FILE]`) permission to call it. Only
builds installed from Google Play pass; set `PLAY_INTEGRITY_REQUIRED=0` only on
a dev server.

**The push budget.** Push goes out one relay request per device, serially, in
the poller's single loop: without a bound a few thousand app subscriptions
held every city's polling for minutes. Per cycle the poller tries at most
`PUSH_BUDGET_PER_CYCLE` digest pushes, longest-waiting first, and starts no
new one once `PUSH_BUDGET_SECONDS` have passed (the one in flight can still
take its 10 s relay timeout); the rest are released like a quota deferral
(unrecorded, they lead the next cycle). Verification and check-in pushes are
not counted. `push: cycle budget spent (…); N digest(s) wait for the next
cycle` in the poller log means it bound: once in a while is a burst, every
cycle means the app outgrew the defaults (raise them as far as a cycle still
finishes inside its minute).

Both `web` and `poller` need them: the poller sends the digests, and the web
container sends the verification push inside the register request (the
poller's once-a-minute sweep is only the fallback). Put the key files in
`~/buergerwecker/secrets/` (gitignored, `chmod 600`); `docker-compose.yml`
mounts that directory read-only at `/run/secrets` in both containers, so the
`_FILE` paths above work as written. Both must see the files, because
`load_config` reads every `_FILE` at start and a path it cannot open stops the
container — the website included. Hence the order on a host that has no keys
yet: `mkdir -m 700 ~/buergerwecker/secrets` (left to Compose, it is created
root-owned), deploy the mount, and only then copy the files in, add the `_FILE`
lines and `docker compose up -d web poller`. A `_FILE` line in `.env` ahead of
the mount stops whichever container is recreated next. Inline values work too,
but a multi-line PEM in `.env` is fragile. The APNs key is downloadable once from
Apple; the FCM file is Firebase → Project settings → Service accounts →
Generate new private key, for a project whose Cloud Messaging API (v1) is
enabled. Both are credentials for *sending*: they name the app, not any
person.

`APNS_SANDBOX` must match the build. Only a development-signed build straight
from Xcode uses the sandbox; **TestFlight and App Store builds use
production**, so the VPS stays at `0` from the first beta on. A mismatch, like
a wrong `APNS_TOPIC`, answers `BadDeviceToken` (or `DeviceTokenNotForTopic`)
for every device. The rules below keep that from ending anyone's
subscriptions or spending any token's daily verification budget; what it
does do is retire, after three refused codes, a phone that registers
meanwhile and holds nothing yet, which registers again by itself.

### What the poller does with a relay's answer

- `200`: delivered; `sent_idempotency.provider` is `apns` or `fcm`.
- APNs `410` / `BadDeviceToken`, FCM `UNREGISTERED`: the token is dead,
  *or* our configuration is wrong (`APNS_TOPIC`, `APNS_SANDBOX`, the FCM JSON
  of another project), which answers exactly the same for every device at
  once. The two are told apart by what a platform-wide misconfiguration
  cannot produce, a delivery: the device is retired, and its subscriptions
  end, only once the platform has delivered to someone since that device
  first answered dead (`push_devices.dead_since`). The rule cannot see a
  *mixed* fleet: `APNS_SANDBOX=1` with a development build registered next to
  TestFlight devices lets the development phone's delivery count as evidence
  against every TestFlight device, which is the other reason the VPS never
  points at the sandbox. Until then the poller logs
  `push: … answered dead-token … has delivered nothing since; not retiring`
  every cycle a slot matches or a verification code is due.

  **The misconfiguration signature** is that line, cycle after cycle, with
  no delivery on the platform at all:
  - no new `sent_idempotency` row for it
    (`sqlite3 ~/buergerwecker/data/app.db "SELECT MAX(sent_at) FROM
    sent_idempotency WHERE provider='apns'"`, or `fcm`, stays put);
  - no `push: apns retired device N: <reason>` line, which needs a delivery
    as evidence.

  Lines `push: retired unverified device N after 3 refused verification
  pushes` (`retire_reason` `unconfirmed_refusals`) do **not** rule it out:
  they need no evidence and appear under a misconfiguration too, for phones
  that registered and hold nothing yet. Fix the knob the dead-token line
  names. A retired device's `retire_reason` says which answer did it, and
  the app re-registers on its next call. A device that never verified gets
  the same evidence rule for being retired as dead. Without evidence, it is
  retired only after three refused codes and only if it holds nothing.
  A device is retired (and `dead_since` stamped) only while it still holds
  the token that answered.
- `5xx`, FCM `429` (project quota), relay unreachable (a timeout, a refused
  or reset connection): released, the next cycle retries, and the platform is
  not tried again this cycle (an outage must not hold the poller for a timeout
  per device). APNs `429` is per device token and releases only that push.
  Nothing is recorded as seen.
- `403` / `401`: our credentials, and so is a key that cannot even be loaded.
  The provider token is renewed and the push retried once; refused again,
  everything on that platform waits for the next cycle. A persistent
  `push: apns auth` line in the poller log means the key or team id is wrong.
- Any other `400`: our payload. Dropped and logged (`push: … refused payload`);
  retrying cannot help. A push the HTTP client cannot even build a request for
  (`refused payload … local: InvalidURL…`) is dropped the same way, on its
  own: one bad row must not stall the platform.

A push goes only to the token it was queued for: a digest carries the token
the cycle found verified, a check-in and a verification push the token they
were made for, and a device that has changed its token since, or (for
anything but the verification push) is no longer verified, gets nothing
(`push: device … changed its token or lost its verification`). The APNs path
carries the token percent-encoded as one segment, dots included.

### The app's API

The API answers `404 {"error": "not_available"}` to everything, the public
catalog routes and routing errors (an unknown path, a wrong method) included,
while `APP_API_ENABLED` is unset or `0`. It is
switched on (`APP_API_ENABLED=1`) together with the `APNS_*`/`FCM_*`
credentials for the TestFlight build. Registration has no confirmation step of
its own, so a device is verified by push before it may subscribe (below).

The app talks to the web container under `/api/v1` (`app/api.py`); nothing
else uses it, and the website is unchanged. The API answers CORS (on every path under `/api/v1`, routing errors included) only for the
app's WebView origins (`CORS_ORIGINS`), and preflights succeed even while
`APP_API_ENABLED` is off, so the gated 404 stays readable by the app. Every
`POST` and `PUT` must be `Content-Type: application/json` (else `415
unsupported_media_type`): a cross-site form or a `text/plain` fetch skips the
preflight, and would let any web page register devices from its visitors'
addresses. Every answer but the public catalog's is `Cache-Control: no-store`.
A device registers its push
token once (`POST /api/v1/devices`, `{platform, token, language}`) and gets a
`device_id` and a `secret` shown once; every later call carries
`Authorization: Bearer <device_id>.<secret>`. The secret is stored hashed
(`push_devices.secret_hash`). There is no account and no address. The token
must have its platform's shape (`api.normalize_push_token`): APNs 64 to 200
hex characters (stored lowercase), FCM 64 to 4096 of `A-Z a-z 0-9 _ : -`;
anything else is `400 invalid_push_token`, on `PUT /device` too. Before this,
`T#1` or `x/../T` reached Apple as `/3/device/T`, one phone behind any number
of rows.

**Verification.** A device starts unverified. Registering (the same token
again included) makes the server push a one-time code to the token
(`data.type == "verify"`, `data.code`); the app reads it and posts it to
`POST /api/v1/device/verify` (`{code}`). Until then only `GET /device`,
`POST /device/verify` and `POST /device/verify/resend` work; everything else
answers `403 {"error": "device_unverified"}`. The app shows "waiting for the
test notification" meanwhile, and may ask for a new push with
`POST /device/verify/resend` (once a minute; the old code stops working). A code
is valid 24 hours from when it was made and stored only as a hash (a pending
secret likewise 24 hours from when it was stored; resends and re-registrations
do not extend either). The web container sends the push
itself inside the register request when it has the `APNS_*`/`FCM_*`
credentials; otherwise (or when the relay is down) the poller sweeps once a
minute and sends it, for up to 24 hours. Registering a known token again (verified or not)
never touches the existing secret, so it never breaks the install that works and
cannot take a row over: the old secret keeps its access, the new one is pending (`GET /device`, which answers it only `{"verified": false}`, and the two verify routes, usable 24 hours)
and replaces the old secret the moment its holder posts the code (exactly the
secret that authenticated that call). If the main secret posts the code
instead, the pending secret is dropped: the phone answered its owner. A
device's main secret may always call
`GET/PUT/DELETE /device` and the verify routes, verified or not, so a device
waiting for its code can still report a rotated token or delete its data; its
`PUT` and `DELETE` act only while the row still has that secret. Changing the push token
(`PUT /device`) un-verifies the device the same way (`"verified": false`, the
code goes to the new token) and pauses its subscriptions, which neither poll
nor count toward a city's plan cap until it verifies again.

Verification pushes are budgeted per token, in a record that outlives the
device row (`verify_attempts`, keyed by a SHA-256 of platform and token), so
deleting a device and registering again starts nothing over: one attempt a
minute per token (and one delivered push a minute per device row), and per
rolling day five on requests that need no credential (a registration, the same
token again, a pending secret's resend) and, separately, five on the main
secret's own requests (a token change, a resend), so a stranger re-registering
somebody's token cannot spend the owner's. A delivered attempt counts toward
the budgets, and so does a refused one, but only while the platform shows it
works: it delivered to someone in the same pass, or, for a dead token, since
the device first answered dead. An outage counts toward nothing, and a
refusal from a platform that delivers to nobody counts toward no budget: a
wrong `APNS_TOPIC`, `APNS_SANDBOX` or FCM project must not leave every phone
that registered in that window locked out of verification for a day after
the fix. Above the budgets sits a hard ceiling of 20 answered attempts per
token per rolling day (`repo.MAX_VERIFY_ATTEMPTS_PER_TOKEN_PER_DAY`), on the
request path (register, resend, `PUT /device`) and the sweep alike. It counts
every attempt the relay answered, refusals without evidence included, so a
junk token costs at most that much even on a platform that delivers to
nobody (FCM before any Android phone gets pushes). A refusal without
evidence counts toward it only while the platform's settings are the ones it
was made under (`push.config_fingerprint`: APNs team, key, topic and sandbox;
the FCM project and service account). A delivery does not forgive anything:
anyone can make one (an attacker's own phone, other users' digests).

**After fixing a push misconfiguration**, whatever the size of the fleet:

- Fixed in our own settings (`APNS_TOPIC`, `APNS_SANDBOX`, `APNS_KEY_ID`,
  `APNS_TEAM_ID`, or the FCM JSON's project or service account) and deployed
  with `up -d`: nothing to do. The changed settings forgive every refusal
  made under the old ones, in web and poller alike, and a phone held at the
  ceiling is looked at again within the hour (`push.MAX_HOLD_SECONDS`) and
  gets its code; at once if it registers or resends.
- Fixed anywhere else (the Apple developer account, the Firebase console),
  so the env is unchanged: the refusals still count, and every phone that
  reached the ceiling during the window stays held for up to a day. You
  recognise the case in the poller log: the platform delivers again (new
  `sent_idempotency` rows for it), while the phones that tried during the
  window log `push: apns verification for device N held …s: its token's
  daily budget or ceiling is spent`, hourly, instead of getting their code.
  Then clear the refusals (deliveries and refusals with evidence stay
  counted) and the holds:

  ```bash
  sqlite3 ~/buergerwecker/data/app.db \
    "DELETE FROM verify_attempts WHERE credited=0; UPDATE push_devices SET verify_next_at=NULL;"
  ```

  The next sweeps send the waiting phones their codes, 25 or 50 a minute; a
  phone retired meanwhile gets its code when its app next calls and
  registers again.

Register and token change
never refuse, they stamp the request and the sender decides when it goes out
(resend answers 429 with `retry_after`); a device over its budget or the
ceiling is left alone until it frees (`push_devices.verify_next_at`). A
request refused three times with evidence (`verify_failures`) is given up
until the next one. An unverified device with no subscription whose token
has been refused three times without evidence since it last delivered or
changed (`verify_tries`, which registering again or resending does not
reset) is retired (`retire_reason` `unconfirmed_refusals`); the app answers
the 410 by registering afresh, and a revived row that is refused again is
retired again at once. The poller's sweep sends to at most 50 devices a cycle
(`push.MAX_SWEEP_DEVICES`), split evenly between the platforms it has
credentials for, in due order: every device a pass claimed and did not
deliver to goes to the back of the queue, a minute after an outage, and
after a refusal twice as long as after the one before, up to an hour (a
sender that lost the claim to another leaves the device alone). A junk backlog on one platform cannot hold up the
other, and a real device waits behind at most the rows that fell due before
it, 25 or 50 a minute. The minute is also an atomic claim on the
idempotency key `verify|<device_id>|<UTC minute>`. The operator dashboard counts only subscriptions that run. `GET /device` shows an unverified device no subscriptions. A device that never verified and
holds no subscription is purged after a day (housekeeping). An app that shows
"waiting for the test notification" forever therefore means `APNS_*`/`FCM_*`
are missing or wrong on the VPS: check `docker compose logs poller | grep
"push:"`.

- `GET/PUT/DELETE /api/v1/device`: status with the subscription list; a
  rotated token or a new language; delete my data (the device row and, by
  cascade, every subscription it holds).
- `GET /api/v1/cities`, `GET /api/v1/cities/<slug>`: the catalog the sign-up
  form shows, public.
- `GET /api/v1/cities/<slug>/slots`: what the last polls found free, per
  watched service, soonest first, with the earliest slot for the widget. For
  a verified device only (the main secret; `401 unauthorized`, `403
  device_unverified`, `410 device_retired` as everywhere), and only for a
  city it watches: without a live subscription there (not deleted, not
  expired) the answer is `403 not_subscribed`. A special-category (Art. 9)
  service appears only to a device that watches that service itself, since
  that somebody watches it is the sensitive fact. At most 60 reads per device
  an hour across workers (`rate_events`), apart from the write limit;
  `Cache-Control: private, no-store`.
  Read from `slot_snapshots`, one row per (city, service) that the poller
  rewrites after every cycle in which all of the service's plans succeeded
  (`app/snapshots.py`), so the overview adds no upstream request: the one
  GET per watched Anliegen per cycle stays what the cities see. A service
  nobody watches has no entry; a failed poll keeps the previous row with its
  older `polled_at`. Rows are pruned after a day.
- `GET/POST /api/v1/subscriptions`, `GET/PUT/DELETE
  /api/v1/subscriptions/<id>`, `POST /api/v1/subscriptions/<id>/renew`: the
  website's rules over JSON. Same validation against the catalog, same
  Art. 9 consent for a special-category service, same term, same renewal,
  and the per-city plan cap with the app limited to its share (below). No
  double opt-in: the OS permission prompt is the opt-in, so a push
  subscription is live at once.
- A retired device (the relay reported its token dead) gets `410
  device_retired` on every authenticated call, and the app registers afresh.
- Rate limits. The rule: nothing keyed on a client network may let a
  stranger on the same network (behind a carrier NAT, thousands of phones
  share one IPv4; a carrier numbers many phones out of one /48) lock the
  real phones there out for long. A client network is an IPv4 address or an
  IPv6 /64; an IPv6 address that carries an IPv4 one (IPv4-mapped, 6to4,
  Teredo) counts as that IPv4.

  Every request that mints a push token or asks for a verification push
  goes through one gate (`api._token_gate`), kept in the database across
  workers and restarts: a registration (a new token or a known one),
  `PUT /device` with a new token, and a resend. No sibling route goes around
  it, and the push itself then meets the per-token budget and ceiling,
  on the request path and in the sweep alike.
  - The hard bound: such requests per client network per ten minutes,
    `MAX_TOKEN_REQUESTS_PER_IP_PER_10_MIN` (default 10), ten times that per
    IPv6 /48, counted only if both have room; `429 rate_limited` with a
    `retry_after` of at most ten minutes. That is the longest a stranger can
    hold the real phones on a shared network off once they stop, and it
    holds however many workers there are.
  - New push tokens that have not verified, per hour:
    `MAX_UNVERIFIED_DEVICES_PER_IP_PER_HOUR` per client network and
    `MAX_UNVERIFIED_DEVICES_PER_IP6_48_PER_HOUR` per /48 (defaults 5 and 50).
    A new device and a device's token change each add one; a device that
    verifies drops out, one deleted or retired before it did stays in. Past
    either, nothing is refused: every verification push the network asks
    for (new token, known token, resend) comes from the poller's sweep a
    minute later instead of at once. Junk from one network cannot make relay
    calls at request speed, and a real phone behind it waits a minute,
    never a day. The web container logs `api: device N: its network is over
    its unverified-token limit; the code comes from the sweep` for each.
  - Every write of a registered device, `DELETE /device` included (counted,
    never refused: erasure does not wait): `SUBSCRIBE_RATELIMIT_PER_IP_PER_HOUR`
    per device credential per hour, per process (`apidev:<id>:main` or
    `:pending`), so no one else can use up a device's count.

  What the database keeps for the network limits (`rate_events`): a bucket
  name (the limit and the first 32 hex digits of an HMAC-SHA256 of the IPv4
  address, the IPv6 /64 or the /48, keyed from `TOKEN_SECRET_PRIMARY`), a
  timestamp, and for a new token the device id. The poller drops each row
  once its window is over, every cycle: the request counts after ten
  minutes, the new-token rows after an hour, so a row lives its window and
  at most a minute more.

  These are speed bumps; the walls against minted devices are what a device
  may hold: at most 10 live subscriptions (`api.MAX_SUBSCRIPTIONS_PER_DEVICE`),
  and the app's share of each city (below).
- The still-looking check-in reaches app subscriptions as a push
  (`push.checkin_*` in the i18n bundles) in the same window as the mail,
  `RENEWAL_REMINDER_DAYS_BEFORE` days before the term ends; the app answers
  with `/renew` or `DELETE`. Only a delivered push stamps
  `reminder_sent_at`, so a relay outage asks again at the next housekeeping
  run.
- Every authenticated call restarts the device's 30-day purge clock
  (`push_devices.last_seen_at`, at most one write per hour).

Mail and push digests of one cycle are sent side by side, in two threads on
two connections (`digest._send_both`): a slow mail provider does not hold
the push batch, and neither channel is scheduled ahead of the other. Push is
not a fast lane: the same daily cap, tightened for both under pressure, and
when mail is out of quota push waits behind it (*Email delivery & quotas*,
"one queue, one wall").

### The app's share of a city

Devices cost nothing to mint (Android needs no more than a headless FCM
receiver and the public `google-services.json`), so no limit here rests on
how many devices a person has; the per-network registration limits above are
speed bumps, not ceilings. What one device may hold is kept small, and what
all of them hold together is capped. Each limit is a count in the database,
checked and written in one `BEGIN IMMEDIATE` transaction, so two gunicorn
workers run one after the other and the second counts the first's row (under
a plain `BEGIN` the loser of a race got a 500). Measured before this existed:
two verified devices with 16 Bonn subscriptions made every website visitor
get `waitlist_full` for any other Bonn service, and a renew per term kept it
that way.

- **The plan cap is split** (`planning.cap_refuses`). A service is
  *mail-held* when a live mail subscription watches it, *app-held* when only
  live app subscriptions do. A website sign-up or edit is refused only for a
  service nobody polls yet, and only when mail-held services, plus any
  app-held services *past the app's half*, would then exceed
  `MAX_PLANS_PER_CITY` (or the tenant's `max_plans`). App-held services within
  the half never count against mail, however many devices hold them. A
  service already polled is never refused to anyone. The app gets a new
  service only if everything polled stays within the cap (never what the
  website would be refused), and app-held services stay within **half the
  cap** (8 of 16).
- **Why app-held services past the half count against mail.** They exist
  only by conversion: app subscriptions joined a mail-held service (always
  allowed) and its last mail subscriber left. Not counted, that recycled the
  app's half without end: mail fills its cap, devices join, mail leaves,
  mail fills a new cap (Bonn: 16 → 96 services polled in six rounds, found in
  review of this change). Counted, a city is never polled for more than
  cap + cap/2 services (Bonn: 24; the app took its half first, mail then
  filled its own cap), and conversions only change who holds a service. The
  excess drains: while app-held services are past the half, no renewal on an
  app-held service is accepted, so they end with their terms (at most
  `SUBSCRIPTION_TTL_DAYS`); meanwhile mail has that many fewer places.
- **At most 3 live subscriptions and 3 distinct services per city per
  device** (`api.MAX_SUBSCRIPTIONS_PER_DEVICE_PER_CITY`,
  `api.MAX_SERVICES_PER_DEVICE_PER_CITY`), over the same rows as the
  10-subscriptions ceiling (an expired one included: it is renewable):
  `409 too_many_in_city` / `409 too_many_services`, each with `limit` and a
  `message` the app shows as is.
- **At most `MAX_APP_SUBSCRIPTIONS_PER_CITY` live app subscriptions per
  city** (default 100, `0` = no ceiling), all devices together: `503
  waitlist_full` for the app only. The website never sees this count. For
  scale: about 200 live mail subscriptions across all 38 tenants today.
- "Live" for the app counts a paused subscription (a device that changed its
  token and has not verified again) because it resumes without passing a
  check (left out, devices could take turns pausing to stack a city), and
  does not count an expired one, which can come back only through `/renew`.
  A paused device cannot renew, so its places end with their terms.
- **`/renew` is judged like a sign-up** at that moment, leaving the
  subscription itself out; refused (`409`/`503`), it keeps the term it has.
- **What squatting still costs, and the signal.** The places are shared, so
  free devices can hold them: 34 devices fill a city's 100 app places, 3 fill
  its app half of new services (8 of 16). Either way only app users are
  turned away: at the ceiling every new app subscription, joining a polled
  service included; past the half, a new or app-held service (joining a
  mail-held one stays open). Edits and renewals keep the services they
  already have, except app-held services past the half, which drain with
  their terms. The whole website is untouched. The web log says
  `api: <city> is at MAX_APP_SUBSCRIPTIONS_PER_CITY` or
  `api: <city> plan cap or app share reached` on each refusal; a city that
  says so all day with few real users is being held. What to do is the
  operator's call (raise the ceiling, or end the holding devices'
  subscriptions); the registration limits are what make holding expensive.

### Verifying after deploy

```
ssh vps 'cd ~/buergerwecker && docker compose logs --since 1h poller | grep "push:"'
curl -s -X POST https://buergerwecker.de/api/v1/devices -H 'content-type: application/json' -d '{}'
```

The second line answers `404 {"error": "not_available"}` while the gate is
closed (`APP_API_ENABLED` unset or `0`) and `400 {"error": "unknown_platform"}`
once it is open.
A real registration answers `201` with `"verified": false`; a device whose
push arrived then posts its code and `GET /device` says `"verified": true`.

Silence is the healthy state. Retention: an unverified device without any
subscription is purged after a day; a retired device is purged 30 days
after retirement; a live one once 30 days have passed since both its last
registration and the end of its last subscription (housekeeping, same clock as
an address). The per-token verification record (`verify_attempts`) is pruned
by the daily housekeeping run once it is a day old, so no row lives past two
days; the rate events (`rate_events`) go every poller cycle once their window
is over (ten minutes or an hour), and housekeeping drops anything a day old.

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

### One-off: apology mail for terms that expired before the check-in existed

Terms that ended before the check-in shipped (2026-08-26, PR #70) expired silently — digests just
stopped, and to the subscriber that looks like the service dying. `scripts/notify_silent_expired.py`
mails the still-renewable part of that cohort once: an apology, the `/renew` link they never got,
and the unsubscribe link. It selects confirmed, never-asked (`reminder_sent_at IS NULL`), expired
subscriptions still inside `EXPIRED_GRACE_DAYS`; anyone already soft-deleted is left alone.

Sends go through the same quota-aware batch path as digests, so the run cannot blow the daily
pool: what does not fit is deferred and the report says so. Delivered subscribers get the
once-per-term latch stamped, so re-running only retries the unsent rest. Dry run first:

```bash
ssh vps 'cd ~/buergerwecker && docker compose run --rm poller \
    python scripts/notify_silent_expired.py --db /data/app.db'
ssh vps 'cd ~/buergerwecker && docker compose run --rm poller \
    python scripts/notify_silent_expired.py --db /data/app.db --send'
```

The cohort shrinks to nothing on its own as the grace windows close, so this script retires
itself — after mid-September 2026 a dry run reporting 0 is the expected result.

## Polling cadence

The poller wakes once a minute, but each tenant can set
`poll_interval_seconds` in its `catalog/<tenant>/scraper_config.json` to be
polled less often (e.g. 180 for a city that mandates one request per three
minutes). Skipped cycles leave the tenant's counters, canary, and
last-polled timestamp untouched.

## Notification granularity

What counts as one piece of news is decided per tenant by `notify_granularity`,
which **defaults by vendor** — `day` for TEVIS, `slot` for everything else — and
can be overridden per tenant in `scraper_config.json`:

- `slot` — a distinct (day, time, office, service). Right for a vendor that
  lists real inventory (smartCJM): each slot is a separate perishable
  opportunity, and `day` there would withhold genuine second chances.
- `day` — (day, office, service), and the row remembers the **earliest time
  already reported** on that day. A slot at the same or a later time on a
  reported day stays quiet; a strictly earlier one goes out. Right for TEVIS
  because earliest-slot-only is how *our* scraper reads it
  (`app/scrapers/tevis.py::parse_slots` yields one earliest slot per office),
  not a property any tenant could differ on: the slot that appears the moment
  somebody books is the same inventory a minute later, while the earliest slot
  only ever moves *back* when somebody cancels — exactly the news worth sending.

History, for anyone tempted to narrow this again: the first version of `day`
was a plain date key that suppressed cancellations along with bookings, so it
ran on `muenster-kfz` alone from 2026-08-25. The key learnt the time on
2026-08-26 (schema 11) and the vendor default followed the same day. If a
vendor ever needs checking, `SELECT city, MAX(n_slots) FROM availability_samples
WHERE location_uuid <> '' GROUP BY city` reads exactly 1 for earliest-slot-only
tenants and hundreds or thousands for real inventory.

Rows written under `day` before `seen_slots.best_time` existed (schema 11)
carry no time and keep suppressing the whole day, as they always did, until
housekeeping prunes them at 7 days — the migration cannot recover a time the
old key never stored. Nothing to do; it works itself out within the week.

**Changing a tenant's granularity re-notifies once unless you backfill first.** The old and new keys
are different values in `seen_slots`, so on the first cycle after the deploy
every affected subscriber with a currently-matching slot gets one digest — which
is one last round of exactly the noise the setting removes.

`scripts/backfill_day_keys.py` prevents that. The stored hashes cannot be read
back, but the tenant's slot space (date x time x office x service) is small
enough to enumerate and match, which recovers every date each subscriber has
already been told about — and the earliest time on it, which the day key
remembers — and writes the day key for it. Run the dry run first and check
`unrecognized` is at or near zero — each unrecognized row is one subscriber who
may still get a single redundant mail:

**The backfill has to run between the build and the restart**, and the ordering
below is the only one that works. `docker exec` into the *running* poller cannot
do it: that container is the old image, which has neither the script nor
`Slot.day_hash`. And once `up -d` has recreated the poller there is no window to
catch — it sleeps to the next minute boundary and runs a cycle, so the burst has
already gone out. `docker compose run` threads the needle: it uses the freshly
built image while the old poller keeps running, unchanged, on the old
granularity.

```bash
ssh vps 'cd ~/buergerwecker && git pull --ff-only && docker compose build poller'

# Dry run first: check `unrecognized` is at or near zero before applying.
ssh vps 'cd ~/buergerwecker && docker compose run --rm poller \
    python scripts/backfill_day_keys.py <tenant> --db /data/app.db'
ssh vps 'cd ~/buergerwecker && docker compose run --rm poller \
    python scripts/backfill_day_keys.py <tenant> --db /data/app.db --apply'

ssh vps 'cd ~/buergerwecker && docker compose up -d --build'
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

### The horizon: the soonest 50 matching slots

Per subscription and cycle the poller looks only at the soonest
`cycle.MAX_SLOTS_PER_CYCLE` (50) slots the filter matches, seen or not. Those
are checked against `seen_slots`, make the digest and are recorded on
delivery; nothing past them is checked, mentioned or recorded. A slot past
the horizon is treated like one past the filter's `max_days_ahead`: news the
cycle it moves within reach (a booking ahead of it), which is the first time
the subscriber hears of it. Before this a cycle cost one `seen_slots` lookup
per subscriber per matching slot and one row per slot delivered (2000
subscriptions on a 1000-slot calendar: 12.6 s a cycle, 2M rows); now at most
50 of each per subscriber. Capping only what a digest *records* would have
been the check/record mismatch CLAUDE.md warns about (a digest per interval
about the same inventory). Deploying it re-notifies nobody: the rows the old
code wrote cover everything inside the new horizon.

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
Description=Bürgerwecker SMART check

[Service]
Type=oneshot
ExecStart=/path/to/buergerwecker/scripts/smartcheck.sh /dev/sda
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
host. The host's own nightly backup (outside this repo) takes a `sqlite3
.backup` dump of `data/app.db` along with everything else under the home
directory, keeps it 14 days, and a workstation pulls the last seven nights.

**No copy of the database may outlive 30 days, wherever it sits.** The privacy
page promises that a permanently removed record is gone from the last backup
30 days later at the latest; `scripts/backup-loop.sh` holds `/mnt/backup` to it.
Its `BACKUP_RETENTION_DAYS` is not set in `docker-compose.yml`, so it is 30;
raising it breaks the page unless the page changes first. Any off-host copy prunes by its own age, not by
mirroring the server: it still has to survive a wiped server, and it still has
to expire. A puller that keeps every snapshot it ever fetched breaks the
promise — the earlier scp pull did exactly that and is retired.

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
