# CLAUDE.md

Guidance for coding agents working in this repository.

## What this is

Bürgerwecker: you enter an email, pick an appointment type and the offices that work for you, and
get a mail the moment a matching slot appears on a city's official booking site. You book it
yourself, there. A Flask web app takes subscriptions, a Python poller checks each city every one to
three minutes, and a backup container snapshots the SQLite database. One Docker Compose stack behind
the shared Caddy on the VPS. Cities live as directories under `catalog/`.

## Working in this repo

**Work in a git worktree, not this checkout.** More than one session runs here at once and they
share the working tree. Run `git status` before you edit anything: modified or untracked files you
did not create mean someone else is mid-task, and a `git add -A` or `git checkout -- .` will eat
their work with no warning. Isolate instead:

```bash
git worktree add -b <branch> ~/gitlab/.worktrees/buergerwecker-<task> main
```

Stage by naming paths, never `git add -A` / `git commit -a`, in any checkout you share.

## Commands

```bash
pip install -e ".[dev]"
pytest -m "not live"     # what CI runs
pytest                   # adds the live tests — they hit real city endpoints
ruff check .
```

Live tests are opt-in behind `LIVE_TESTS=1` and talk to real Leipzig endpoints. Never make them run
by default, and never add a network call to an ordinary test.

## Things that are easy to get wrong

**This service does not book, and will not.** No automated booking, ever — it is a stated product
boundary in `README.md`, not a missing feature. Do not add it, do not scaffold toward it, and do not
"helpfully" implement it because an issue seems to ask for it.

**Client IP resolution is deliberate and subtle** (`app/web.py::_client_ip`). Caddy runs without
`trusted_proxies`, so it overwrites any client-supplied `X-Forwarded-For` with the real peer — which
makes XFF trustworthy here. Behind Cloudflare that peer is a shared edge IP, so `CF-Connecting-IP` is
preferred, **but only when the peer really is Cloudflare**, because otherwise that header is
spoofable. All three conditions matter; simplifying any of them reintroduces a spoofable rate limit.

**`IPRateLimiter` is per-process.** With N gunicorn workers the effective limit is N×limit. It is a
soft bot deterrent and explicitly not a security control — never make an authorization decision from
it.

**Bump `TOKEN_VERSION` in `app/tokens.py`** on any change to the signed payload format, or existing
links in already-sent mail silently misparse.

**Mail sends are idempotent by key** — `subscription_id | sorted slot hashes | cycle_id`. Changing
how that key is built means re-notifying people about slots they were already told about.

**Several providers, and the From domain must be verified in every one of them.**
`EMAIL_PROVIDER_ORDER` falls back along the configured chain (Mailjet, Brevo, Sweego); an
unverified sender domain makes a fallback provider reject the mail at exactly the moment it is
needed. Quotas are enforced in config — see the DEPLOY runbook.

**One malformed address used to kill a whole batch.** Batch sends must isolate per-recipient
failures; `tests/test_send_batch.py` guards it.

**`tests/test_no_real_pii.py` fails the build on a real email domain.** Fixtures use `@example.com`.
When a real address triggers a bug, reproduce its *shape* — the 2026-07-24 case is in
`tests/test_subscribe.py` as `subscriber@example-com`, a dotless dead domain — never the address.

## Shipping

Branch, PR, squash-merge — never push to `main` directly, even for a one-line docs change.

1. `pytest -m "not live"` and `ruff check .` green before anything else.
2. Branch off `main`, commit, `gh pr create`, let CI run.
3. `gh pr merge --squash --delete-branch`.
4. Deploy and verify per **[`docs/DEPLOY.md`](docs/DEPLOY.md)** — that file is the runbook and the
   only place deploy commands live. Do not copy them here; a second copy is what rots.

Verify against **https://buergerwecker.de/healthz**. `www.buergerwecker.de` and
`termine.jakubwaller.eu` only redirect to the apex, so checking them reports a healthy deploy as a
failure.

If tests or the post-deploy check fail, stop and report rather than merging or leaving the stack
half-deployed.
