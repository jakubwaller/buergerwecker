from __future__ import annotations
import base64
import hashlib
import hmac

TOKEN_VERSION = 1  # bump on any change to the signed payload format

class InvalidToken(Exception):
    pass

def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def _hmac(secret: str, payload: bytes) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()

def _payload(subscription_id: int, purpose: str) -> bytes:
    return f"{TOKEN_VERSION}:{subscription_id}:{purpose}".encode("utf-8")

def sign(subscription_id: int, purpose: str, *, primary: str, previous: str) -> str:
    sig = _hmac(primary, _payload(subscription_id, purpose))
    return f"{TOKEN_VERSION}.{subscription_id}.{purpose}.{_b64u(sig)}"

def verify(token: str, purpose: str, *, primary: str, previous: str) -> int:
    try:
        ver_str, sub_id_str, tok_purpose, sig_b64 = token.split(".", 3)
    except ValueError:
        raise InvalidToken("malformed token")
    try:
        version = int(ver_str)
    except ValueError:
        raise InvalidToken("non-integer version")
    if version != TOKEN_VERSION:
        raise InvalidToken(f"unsupported token version {version}")
    if tok_purpose != purpose:
        raise InvalidToken("purpose mismatch")
    try:
        sub_id = int(sub_id_str)
    except ValueError:
        raise InvalidToken("non-integer subscription id")
    payload = _payload(sub_id, purpose)
    try:
        sig = _b64u_decode(sig_b64)
    except Exception:
        raise InvalidToken("bad signature encoding")
    for secret in (primary, previous):
        if not secret:
            continue
        expected = _hmac(secret, payload)
        if hmac.compare_digest(sig, expected):
            return sub_id
    raise InvalidToken("signature does not match")
