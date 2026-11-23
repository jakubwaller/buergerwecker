"""JCC Software jcc-flow scraper (Stadt Essen Bürgeramt, termine.essen.de).

Notify-only, earliest slot per office like TEVIS. The portal's "Datum und
Zeit" step shows the earliest free appointment of every office, and it reads
them with one GET, `/proxy/warp/appointment/firstAvailableAppointmentTime`,
for the chosen service and the offices' location numbers. One poll is that
one GET; nothing else is read. It never reads the free-times list of a day
(`availabletimelist`), never selects a time, never creates an appointment or
uploads a file (`/warp/appointment`, `filefield`), and sends no personal
data. Booking stays on the city's page.

Every request to the API carries the header set the page sends on
/proxy/warp/ (`app.js` request interceptor): a Bearer token, the customer id
(`id`), `warp-api-version`, `language` and `connection-source-id`. The token
is not issued by the API: the page reads `flow-api-token` from its public
config (`customer_assets/<customer id>/config-<version>.json`, the version from
`version.json` next to it) and AES-decrypts it with a key and IV that are
constants in the bundle (`decryptUsingAES256`: AES-256-CBC, PKCS7). We do the
same, once, and cache the token on the poller's long-lived session. On a 401
or 403 the token is derived again and the GET retried once; a second refusal
raises.

Requests per poll: 1 once the token is cached; 2 more (version.json and
config) at process start and after a refusal. A tenant whose token is
refused twice in a row (re-derived, refused again) is remembered as refused
on the session for `poll_interval_seconds`; until then its polls raise
without a request, so a persistent 401 costs at most 4 requests per
interval for the whole tenant, not per plan.

Verified live on 2026-11-23 (run by the owner): with the token, the four
other headers, `Origin` (the base URL) and `Referer` (the base URL and a
slash) the read answered HTTP 200 with 7 offices; without Origin and Referer
it answers 400 "Not a valid request". No login and no session call is
needed (the page's anonymous POST /warp/login answered 500 and this module
never calls it). The User-Agent stays the session's own; none is set here.
The answer shape and the tests come from that answer and the browser's
recorded requests.
"""
from __future__ import annotations
import base64
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from app.models import Slot, PollPlan
from app.catalog import load_catalog

# Constants of the jcc-flow bundle, not of a tenant: the AES key and IV of
# decryptUsingAES256, and the JCCAfspraken connection source the page names
# on every /proxy/warp/ request.
_AES_KEY = b"#Swg9f@$Aa+3De3^DDGC6MrhQEPkrU?N"
_AES_IV = b"gdnE9!zsgX4JG62G"
_CONNECTION_SOURCE_ID = "f59ddbf9-9158-42d0-ac39-f6065c003da4"
_TZ = ZoneInfo("Europe/Berlin")


class _Refused(RuntimeError):
    """The API answered 401 or 403: the token is no longer accepted."""


def decrypt_token(value: str) -> str:
    """The page's decryptUsingAES256 on a base64 config value."""
    dec = Cipher(algorithms.AES(_AES_KEY), modes.CBC(_AES_IV)).decryptor()
    raw = dec.update(base64.b64decode(value)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return (unpad.update(raw) + unpad.finalize()).decode("utf-8")


def _token(http: requests.Session, scfg: dict) -> str:
    """The tenant's Bearer token, derived from its public config once per
    session (tracked on the session object, as tevis tracks its cookie)."""
    tokens: dict = getattr(http, "_jcc_tokens", None) or {}
    customer = scfg["customer_id"]
    if customer not in tokens:
        assets = f"{scfg['base_url'].rstrip('/')}/customer_assets/{customer}"
        r = http.get(f"{assets}/version.json", timeout=30)
        r.raise_for_status()
        r = http.get(f"{assets}/config-{r.text.strip()}.json", timeout=30)
        r.raise_for_status()
        tokens[customer] = decrypt_token(r.json()["flow-api-token"])
        http._jcc_tokens = tokens
    return tokens[customer]


def _to_date(scfg: dict) -> str:
    # The page's own format: no zero padding ("2027-1-22").
    d = datetime.now(_TZ).date() + timedelta(days=int(scfg["horizon_days"]))
    return f"{d.year}-{d.month}-{d.day}"


def _earliest(http: requests.Session, scfg: dict, service: str,
              numbers: list[int]) -> list:
    """The `data` list of one firstAvailableAppointmentTime answer; anything
    but HTTP 200 with success true and a list raises."""
    token = _token(http, scfg)
    params = [("toDate", _to_date(scfg)), ("activityId", service),
              ("amount", "1")] + [("locationNumber", n) for n in numbers]
    # Per request, never on the session: the poller's session is shared by
    # every tenant and vendor.
    headers = {"Authorization": f"Bearer {token}",
               "id": scfg["customer_id"],
               "warp-api-version": str(scfg["warp_api_version"]),
               "language": "de",
               "connection-source-id": _CONNECTION_SOURCE_ID,
               # The API answers 400 without them (live, 2026-11-23).
               "Origin": scfg["base_url"].rstrip("/"),
               "Referer": scfg["base_url"].rstrip("/") + "/"}
    r = http.get(f"{scfg['api_url'].rstrip('/')}/proxy/warp/appointment/"
                 "firstAvailableAppointmentTime",
                 params=params, headers=headers, timeout=30)
    if r.status_code in (401, 403):
        raise _Refused(f"jcc HTTP {r.status_code}")
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError as exc:  # an HTML error page from a proxy or firewall
        raise RuntimeError(f"jcc: answer is not JSON: {exc}") from exc
    if body.get("success") is not True or not isinstance(body.get("data"), list):
        raise RuntimeError(f"jcc: unexpected answer {str(body)[:200]}")
    return body["data"]


def parse_slots(data: list, *, service_id: str, locations) -> list[Slot]:
    """One Slot per office with a first available time. A row whose
    firstAvailableTime is null has nothing free. Anything else odd raises
    instead of reading as "nothing free": a row without a locationId or
    firstAvailableTime key, an office not asked for, an unparseable time, a
    date before today (the .NET empty date 0001-01-01). An offset-aware time
    is converted to Europe/Berlin, a naive one is taken as Berlin local."""
    slots: list[Slot] = []
    today = datetime.now(_TZ).date()
    for row in data:
        if "locationId" not in row or "firstAvailableTime" not in row:
            raise RuntimeError(f"jcc: row without locationId/time {str(row)[:200]}")
        loc, first = row["locationId"], row["firstAvailableTime"]
        if loc not in locations:
            raise RuntimeError(f"jcc: office {loc} was not asked for")
        if first is None:
            continue
        at = datetime.fromisoformat(first)  # malformed raises, never []
        if at.tzinfo is not None:
            at = at.astimezone(_TZ)
        if at.date() < today:
            raise RuntimeError(f"jcc: time {first} is in the past")
        date_iso, time_str = at.date().isoformat(), at.strftime("%H:%M")
        slots.append(Slot(
            date=date_iso,
            time_str=time_str,
            location_uuid=loc,
            service_uuid=service_id,
            # No per-slot deep link; digests link to the portal start page
            # (catalog.booking_start_url). Keeps the Slot shape unique.
            booking_token=f"{date_iso}T{time_str}@{loc}",
        ))
    return slots


def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """The earliest free slot per office for the plan's service."""
    catalog = load_catalog(plan.city)
    scfg = catalog.scraper_config
    if scfg.get("vendor") != "jcc":
        raise RuntimeError(
            f"city {plan.city} not configured for jcc scraper "
            f"(vendor={scfg.get('vendor')})"
        )
    service = plan.appointment_type
    offices = (catalog.service_locations.get(service)
               or list(catalog.locations.values()))
    if plan.locations != "all":
        offices = [o for o in offices if o in plan.locations]
    if not offices:
        return []
    numbers = [scfg["location_numbers"][o] for o in offices]
    customer = scfg["customer_id"]
    refused: dict = getattr(http, "_jcc_refused", None) or {}
    if refused.get(customer, 0) > time.monotonic():
        raise RuntimeError("jcc: token refused, not asking again yet")
    try:
        data = _earliest(http, scfg, service, numbers)
    except _Refused:
        # Once more with a freshly derived token; a second refusal raises
        # and is remembered for one poll interval.
        http._jcc_tokens.pop(customer, None)
        try:
            data = _earliest(http, scfg, service, numbers)
        except _Refused:
            refused[customer] = time.monotonic() + int(
                scfg.get("poll_interval_seconds", 180))
            http._jcc_refused = refused
            raise
    refused.pop(customer, None)
    return parse_slots(data, service_id=service, locations=set(offices))
