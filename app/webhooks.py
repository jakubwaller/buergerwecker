"""Provider delivery-feedback webhooks: the asynchronous half of sending.

A provider accepts a message with HTTP 200 and only reports minutes later that
the mailbox does not exist, or that the recipient pressed "spam". `send_batch`
cannot see either — it only ever learns about failures the provider can report
synchronously (400/422, see `email_failures`). So without this module a dead or
hostile address is mailed forever, its bounce rate counts against the sending
domain, and the eventual outcome is a blocked domain that no amount of extra
provider quota can fix.

Each provider posts a different shape; the parsers below normalise them to
`Event`, and `apply_events` is the single place that decides what an event
does. Adding a provider means writing one parser and registering it in
`PARSERS` — the effects stay in one place on purpose.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sqlite3
from dataclasses import dataclass

from app.repo import (clear_soft_bounces, record_soft_bounce,
                      soft_delete_by_email, suppress_address)

# What an event means for us, independent of who reported it.
HARD_BOUNCE = "hard_bounce"   # mailbox does not exist: suppress, drop subs
COMPLAINT = "complaint"       # recipient pressed "spam": suppress, drop subs
UNSUBSCRIBE = "unsubscribe"   # recipient used the provider's unsubscribe path
SOFT_BOUNCE = "soft_bounce"   # temporary: count, suppress only on a run
DELIVERED = "delivered"       # reached the mailbox: clears a soft-bounce run
IGNORE = "ignore"             # opens, clicks, sends — nothing to do


@dataclass(frozen=True)
class Event:
    email: str
    kind: str
    provider: str
    detail: str | None = None


def _clean(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _email(value) -> str:
    """Normalise a provider-reported address to the form we store it in.

    `web.subscribe` lowercases on sign-up, and every effect here matches on
    equality: `soft_delete_by_email`, `mail._dead_addresses` and `is_suppressed`
    all compare the address exactly. A provider that echoes back the mixed case
    somebody typed — or, for a blocklist event, an address that never went
    through this service at all — would otherwise write a suppression row that
    blocks nothing and ends no subscription, and look identical on /admin to one
    that works. Returns "" for anything that is not a usable address, which the
    parsers treat as "skip this event"."""
    return value.strip().lower() if isinstance(value, str) else ""


def _detail(*parts) -> str | None:
    """Join whatever the provider said about the failure, for the admin page.

    Deliberately short and provider-supplied only: this sits next to an e-mail
    address in the database, so it carries the reason a message failed and
    nothing else about the person.
    """
    text = " ".join(str(p) for p in parts if p)
    return text[:200] or None


# --------------------------------------------------------------------------
# Mailjet — dev.mailjet.com/email/guides/webhooks/
#
# Events are grouped: "all the events of the last second for the same webhook
# URL" arrive as a JSON array, so the payload is an object OR a list. Event
# names: sent, open, click, bounce, spam, blocked, unsub.
# --------------------------------------------------------------------------

# Mailjet reports what a failure was "related to". Only these blame the
# address; a `blocked` for content or a system fault says nothing about the
# recipient, and suppressing on those would retire good subscribers over our
# own mistake.
_MAILJET_ADDRESS_FAULTS = {"recipient", "domain", "mailbox", "mailbox_inactive"}


def parse_mailjet(payload) -> list[Event]:
    events = payload if isinstance(payload, list) else [payload]
    out: list[Event] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        email = _email(raw.get("email"))
        if not email:
            continue
        name = _clean(raw.get("event"))
        related = _clean(raw.get("error_related_to"))
        detail = _detail(raw.get("error"), raw.get("comment"),
                         raw.get("source"))
        if name == "bounce":
            # `hard_bounce` is Mailjet's own permanence verdict; a bounce that
            # also sets `blocked` has landed the address on their blocklist,
            # which makes every future send to it a guaranteed failure.
            hard = bool(raw.get("hard_bounce")) or bool(raw.get("blocked"))
            kind = HARD_BOUNCE if hard else SOFT_BOUNCE
        elif name == "blocked":
            kind = (HARD_BOUNCE if related in _MAILJET_ADDRESS_FAULTS
                    else SOFT_BOUNCE)
        elif name == "spam":
            kind = COMPLAINT
        elif name == "unsub":
            kind = UNSUBSCRIBE
        elif name == "sent":
            # Mailjet's "sent" is acceptance by the *recipient's* mail server,
            # not by Mailjet — that is a delivery.
            kind = DELIVERED
        else:
            kind = IGNORE
        out.append(Event(email=email, kind=kind, provider="mailjet",
                         detail=detail))
    return out


# --------------------------------------------------------------------------
# Brevo — developers.brevo.com/docs/transactional-webhooks
# One event per request. Event names are snake_case strings in `event`.
# --------------------------------------------------------------------------

_BREVO_KINDS = {
    "hard_bounce": HARD_BOUNCE,
    "invalid_email": HARD_BOUNCE,
    # Brevo blocks an address once it has already bounced, complained or
    # unsubscribed on this account. The verdict is permanent on their side, so
    # every further attempt is spent quota for a message that cannot go out.
    "blocked": HARD_BOUNCE,
    "spam": COMPLAINT,
    "unsubscribed": UNSUBSCRIBE,
    "soft_bounce": SOFT_BOUNCE,
    # `error` is Brevo's catch-all for a send that failed for an unstated
    # reason. Counting it as soft means a persistent one still retires the
    # address, without one transient fault doing it immediately.
    "error": SOFT_BOUNCE,
    "delivered": DELIVERED,
}


def parse_brevo(payload) -> list[Event]:
    events = payload if isinstance(payload, list) else [payload]
    out: list[Event] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        email = _email(raw.get("email"))
        if not email:
            continue
        kind = _BREVO_KINDS.get(_clean(raw.get("event")), IGNORE)
        out.append(Event(email=email, kind=kind, provider="brevo",
                         detail=_detail(raw.get("reason"))))
    return out


# --------------------------------------------------------------------------
# Sweego — learn.sweego.io/docs/webhooks
# The recipient is `recipient`, the event is `event_type`. Their own payload
# docs spell the bounce events inconsistently (`soft-bounce` with a hyphen,
# `hard_bounce` with an underscore), so both separators are accepted for both.
# --------------------------------------------------------------------------

_SWEEGO_KINDS = {
    "hard_bounce": HARD_BOUNCE,
    "soft_bounce": SOFT_BOUNCE,
    "complaint": COMPLAINT,
    "list_unsub": UNSUBSCRIBE,
    "delivered": DELIVERED,
}


def parse_sweego(payload) -> list[Event]:
    events = payload if isinstance(payload, list) else [payload]
    out: list[Event] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        email = _email(raw.get("recipient"))
        if not email:
            continue
        name = _clean(raw.get("event_type")).replace("-", "_")
        out.append(Event(email=email, kind=_SWEEGO_KINDS.get(name, IGNORE),
                         provider="sweego", detail=_detail(raw.get("details"))))
    return out


PARSERS = {
    "mailjet": parse_mailjet,
    "brevo": parse_brevo,
    "sweego": parse_sweego,
}


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

def _eq(supplied: str, expected: str) -> bool:
    """Constant-time compare of two strings that came off the wire.

    `hmac.compare_digest` refuses a `str` containing non-ASCII and raises
    TypeError. Both callers below hand it attacker-chosen text — a URL path
    segment and an HTTP header — so comparing the strings directly turns a
    request with one accented character into a 500 and a stack trace in the
    log, where it should be a flat 403. Comparing the encoded bytes keeps the
    timing property and answers every input.
    """
    return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"),
                               expected.encode("utf-8", "surrogatepass"))


def check_secret(supplied: str, expected: str) -> bool:
    """Constant-time compare of the secret carried in the webhook URL.

    None of the three providers signs its payload except Sweego, and Mailjet's
    own documented answer is to put credentials in the endpoint URL. A secret
    path segment is that, in a form all three can be configured with. It is
    only as private as the URL: keep it out of access logs (Caddy does not log
    this vhost and gunicorn has no access log) and rotate it by changing one
    env var and the URL in each provider's dashboard.
    """
    return bool(supplied) and bool(expected) and _eq(supplied, expected)


def _signing_keys(secret: str) -> list[bytes]:
    """The HMAC keys a dashboard-copied Sweego secret can plausibly mean.

    Documented as base64, but `base64.b64decode` does not validate: handed a
    secret that is not base64 it silently discards the characters outside the
    alphabet and returns a wrong key, so a misread of the format is not an
    error at startup — it is every Sweego webhook rejected with 403, forever,
    with only /admin's silent-provider line to show for it. Standard Webhooks,
    whose header names Sweego uses, additionally prefixes the secret with
    `whsec_`, which is exactly such a non-alphabet character.

    So derive both readings and accept a signature matching either. This does
    not weaken anything: each candidate key still has to produce the signature
    that arrived, and both are derived from the same shared secret.
    """
    raw = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    keys = [raw.encode()]
    try:
        decoded = base64.b64decode(raw)
    except (ValueError, TypeError):
        decoded = b""
    if decoded and decoded not in keys:
        keys.append(decoded)
    return keys


def _offered_signatures(header: str) -> list[str]:
    """Every signature the `webhook-signature` header offers.

    A plain HMAC implementation sends one bare base64 digest. Standard
    Webhooks — again, the source of these header names — sends a
    space-separated list of versioned ones, `v1,<base64> v1a,<base64>`, so that
    a provider can rotate keys without a flag day. Reading only the bare form
    would reject every delivery in the second shape.
    """
    out: list[str] = []
    for part in header.split():
        out.append(part)
        _, sep, rest = part.partition(",")
        if sep and rest:
            out.append(rest)
    return out


def verify_sweego_signature(*, webhook_id: str, timestamp: str,
                            signature: str, body: bytes, secret: str) -> bool:
    """HMAC-SHA256 over `{webhook-id}.{webhook-timestamp}.{body}`, base64, with
    a base64-encoded secret (learn.sweego.io/docs/webhooks/webhook_signature).

    Both the secret and the header are read in the two encodings that spelling
    can have — see `_signing_keys` and `_offered_signatures`. Getting either
    wrong fails closed and silently: a 403 on every delivery, no data, and only
    the silent-provider line on /admin to notice it by.

    `body` must be the raw request bytes: re-serialising the parsed JSON
    changes whitespace and key order and the signature stops matching.

    No timestamp-freshness check on purpose. It would reject Sweego's own
    retries of a delivery we failed to answer, and it buys little here: every
    effect in `apply_events` is idempotent except the soft-bounce counter,
    where a replayed event costs one count out of the threshold.
    """
    if not (webhook_id and timestamp and signature and secret):
        return False
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    candidates = {base64.b64encode(hmac.new(key, signed, hashlib.sha256)
                                   .digest()).decode()
                  for key in _signing_keys(secret)}
    return any(_eq(expected, offered)
               for expected in candidates
               for offered in _offered_signatures(signature))


# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------

@dataclass
class ApplyResult:
    suppressed: int = 0          # addresses newly retired
    unsubscribed: int = 0        # subscriptions soft-deleted
    soft_bounces: int = 0        # counted, not (yet) suppressed
    delivered: int = 0           # soft-bounce runs cleared
    ignored: int = 0


def apply_events(conn: sqlite3.Connection, events: list[Event], *,
                 soft_bounce_threshold: int = 5) -> ApplyResult:
    """Apply normalised events to the suppression list and the subscriptions.

    A hard bounce or a complaint ends the subscriptions held by that address as
    well as suppressing it. Leaving them live would keep the subscriber in the
    active count, keep matching slots for them and keep staging digests that
    the send path then throws away every cycle — busywork for a person who
    cannot or does not want to be reached.

    An unsubscribe is the person's own decision, so it ends their
    subscriptions but does NOT suppress the address: signing up again is
    theirs to do, and the double opt-in confirmation gates it.
    """
    from app.db import transaction

    result = ApplyResult()
    if not events:
        return result
    # Opens and clicks are the bulk of what a provider sends and change
    # nothing. Returning before BEGIN keeps them off the write path entirely.
    if all(ev.kind == IGNORE for ev in events):
        result.ignored = len(events)
        return result
    with transaction(conn):
        for ev in events:
            if ev.kind in (HARD_BOUNCE, COMPLAINT):
                suppress_address(conn, ev.email, reason=ev.kind,
                                 provider=ev.provider, detail=ev.detail)
                result.suppressed += 1
                result.unsubscribed += soft_delete_by_email(conn, ev.email)
            elif ev.kind == UNSUBSCRIBE:
                result.unsubscribed += soft_delete_by_email(conn, ev.email)
            elif ev.kind == SOFT_BOUNCE:
                escalated = record_soft_bounce(
                    conn, ev.email, threshold=soft_bounce_threshold,
                    provider=ev.provider, detail=ev.detail)
                if escalated:
                    result.suppressed += 1
                    result.unsubscribed += soft_delete_by_email(conn, ev.email)
                else:
                    result.soft_bounces += 1
            elif ev.kind == DELIVERED:
                clear_soft_bounces(conn, ev.email)
                result.delivered += 1
            else:
                result.ignored += 1
    return result
