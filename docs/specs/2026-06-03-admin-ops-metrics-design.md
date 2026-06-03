# Admin ops metrics — design

**Date:** 2026-06-03
**Status:** approved

## Goal

Surface more operational visibility on `/admin`, primarily **how many requests
we send upstream to Leipzig** (relevant to the IP-block runbook / ≤10 req/min
posture), plus a few cheap already-available metrics.

## Background

- `city_state.requests_today` exists in the schema but is **never written** — it
  always reads 0.
- `last_polled_at` is written per city per cycle (`cycle.py`).
- One `poll()` to Leipzig makes **2–4 HTTP requests** (acquire WSID → CSRF →
  POST services → POST locations; a WSID cache amortises the session GETs).
- `init_schema` runs in the **poller** at startup, not in web. Poller uses a
  plain `requests.Session`.

## Decisions (from brainstorming)

- Count **both** poll cycles and underlying HTTP requests, shown separately.
- Keep **both** a `today` value and an **all-time** total, per city.
- Also surface: last poll time per city, slots currently cached, emails sent
  all-time, last poller failure alert.

## Design

### Schema (`city_state`)
Reuse existing `requests_today`; add `polls_today`, `polls_total`,
`requests_total` (all `INTEGER NOT NULL DEFAULT 0`) and `counts_date TEXT`
(the UTC date the `*_today` values belong to). Bump `SCHEMA_VERSION` → 2.

Migration: idempotent `ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`
plus a duplicate-column `try/except` (so concurrent poller/web startup can't
crash). Fresh installs get the columns from `SCHEMA_SQL`.

### Counting
- `app/http_session.py`: `CountingSession(requests.Session)` increments
  `request_count` on every `request()` — counts **only** upstream Leipzig calls
  (Mailjet uses a separate path).
- Poller uses `CountingSession` (reused across cycles, as today).
- `run_cycle` attributes, per plan, `+1 poll` and `+Δrequest_count` to that
  plan's city, then persists with one set-based UPDATE that **resets the
  `*_today` values when `counts_date` rolls over** (lazy daily reset on write;
  no dependency on housekeeping timing). "Today" = UTC date.

### Admin stats (`app/admin.py`)
Add `upstream_by_city` (`{city: {polls_today, polls_total, requests_today,
requests_total}}`, today values shown as 0 when `counts_date` ≠ today),
`last_polled_at_by_city`, `slots_cached`, `emails_sent_total`,
`last_failure_alert_at`. Counter reads are wrapped defensively (zeros if a DB
hasn't migrated yet). No template change — `admin.html` already loops
`stats.items()`.

## Testing
- `CountingSession` increments on request.
- `run_cycle` attributes polls + HTTP requests to `city_state`.
- Migration is idempotent and adds columns to a pre-existing `city_state`.
- `stats()` returns the new keys with correct values incl. today-gating.
- `/admin` renders the new keys.

## Deploy
Touches the **poller** (counting + migration) and **web** (reading), so the
rebuild must include both: `docker compose up -d --build web poller`.

## Out of scope (YAGNI)
Per-cycle history table; rolling-24h window; admin-page localisation.
