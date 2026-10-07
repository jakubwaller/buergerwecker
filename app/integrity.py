"""Play Integrity attestation for Android registrations.

A phone registering with the app API proves it receives a verification push,
and on Android that proof can be faked: `google-services.json` ships in every
APK, so a headless FCM receiver mints real tokens and receives the push with
no phone at all (a scripted fleet measured 60 verified devices an hour from
one IPv4 address). So an FCM device row only comes into existence, or takes a
new token, with a Play Integrity verdict showing the real app on a real device.

The app asks Google Play for a standard-request token whose `requestHash` is
the SHA-256 of its own FCM token; we have Google decode it (`decodeIntegrityToken`,
with a service account) and check the verdict. Binding the hash to the push
token makes a replayed verdict useless for any other token.

Fails closed: when we cannot get an answer (no credentials, Google down or
rate-limiting us) the registration is refused, never let through.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from urllib.parse import quote

from app import push

PACKAGE_NAME = "de.buergerwecker.app"
DECODE_URL = "https://playintegrity.googleapis.com/v1/{package}:decodeIntegrityToken"
SCOPE = "https://www.googleapis.com/auth/playintegrity"
# A verdict is minted right before the registration request. Ten minutes
# leaves room for a slow network and a retry, little for a stockpile; a minute
# into the future allows for clock skew between Google and us.
MAX_AGE_MS = 10 * 60 * 1000
MAX_FUTURE_MS = 60 * 1000
_JWE_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def looks_like_integrity_token(token: str) -> bool:
    """A Play Integrity token is a JWE in compact serialization: five
    base64url segments separated by dots. Anything else cannot decode, so it
    is refused without asking Google (a decode costs daily quota)."""
    return bool(_JWE_SHAPE.fullmatch(token))


def verify_play_integrity(cfg, integrity_token, push_token: str) -> str | None:
    """None when `integrity_token` shows the real app on a real device and
    was minted for `push_token`; else an error key: `integrity_missing` (no
    token), `integrity_failed` (the verdict says no, or Google calls the
    token invalid) or `integrity_unavailable` (we could not get an answer)."""
    if not isinstance(integrity_token, str) or not integrity_token.strip():
        return "integrity_missing"
    if not cfg.play_integrity_service_account_json:
        print("integrity: no service account configured; refusing", flush=True)
        return "integrity_unavailable"
    try:
        acct = json.loads(cfg.play_integrity_service_account_json)
        bearer = push._google_access_token(acct, SCOPE, "integrity")
        resp = push._post(
            "integrity", DECODE_URL.format(package=quote(PACKAGE_NAME, safe="")),
            headers={"authorization": f"Bearer {bearer}"},
            json={"integrity_token": integrity_token.strip()})
    except Exception as exc:
        # Ours or Google's, never this device's: a network error, a refused
        # token exchange, and any malformed service account (bad JSON, a JSON
        # that is not the expected object, a private_key jwt cannot load).
        # Same stance as push._credentials; fail closed with a 503, not a 500.
        print(f"integrity: Google unreachable or credentials unusable: {exc!r}",
              flush=True)
        return "integrity_unavailable"
    if resp.status_code == 400:
        return "integrity_failed"
    if resp.status_code != 200:
        # 401/403 are our credentials or API setup, 429 our quota, 5xx theirs:
        # none of them says anything about this device.
        print(f"integrity: decode answered {resp.status_code}", flush=True)
        return "integrity_unavailable"
    try:
        payload = resp.json()["tokenPayloadExternal"]
    except (ValueError, KeyError, TypeError):
        return "integrity_unavailable"
    return None if _verdict_ok(payload, push_token) else "integrity_failed"


def _verdict_ok(payload, push_token: str) -> bool:
    try:
        details = payload["requestDetails"]
        if details["requestPackageName"] != PACKAGE_NAME:
            return False
        if details["requestHash"] != hashlib.sha256(push_token.encode("utf-8")).hexdigest():
            return False
        age_ms = time.time() * 1000 - int(details["timestampMillis"])
        if age_ms > MAX_AGE_MS or age_ms < -MAX_FUTURE_MS:
            return False
        app = payload["appIntegrity"]
        if app["appRecognitionVerdict"] != "PLAY_RECOGNIZED":
            return False
        if app["packageName"] != PACKAGE_NAME:
            return False
        # appLicensingVerdict is deliberately not required: the app is free,
        # and the licensing check refuses real Play installs (an account that
        # has not "acquired" the app, family sharing, some work profiles).
        return "MEETS_DEVICE_INTEGRITY" in payload["deviceIntegrity"].get(
            "deviceRecognitionVerdict", [])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
