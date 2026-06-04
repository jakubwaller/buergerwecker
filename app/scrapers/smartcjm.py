from __future__ import annotations
import re
import time as _time
import urllib.parse
from bs4 import BeautifulSoup
import requests
from app.models import Slot, PollPlan
from app.catalog import load_catalog

# 5-min margin under Leipzig's observed 20-min idle session countdown.
_WSID_TTL_SECONDS = 15 * 60
# Cache value: (wsid, csrf_token, rev, acquired_at_monotonic)
_WSID_CACHE: dict[tuple[str, str], tuple[str, str, str, float]] = {}


def _now() -> float:
    return _time.monotonic()


APPOINTMENT_RESERVE_RE = re.compile(
    r"appointment_reserve\(\s*"
    r"'([^']+)'\s*,\s*"   # encoded datetime
    r"'(\d+)'\s*,\s*"     # duration minutes
    r"'([^']+)'\s*,\s*"   # location uuid
    r"'([^']+)'\s*\)"     # resource uuid (counter/staff — NOT the service; see parse_slots)
)
SLOT_LI_TESTID_RE = re.compile(r"^slot_button_li-\d+$")


def parse_slots(html: str, *, service_uuid: str) -> list[Slot]:
    """Parse Smart-CJM search-result HTML into Slot records.

    The upstream button is `appointment_reserve(datetime, duration, location,
    resource)` — the 4th argument is a *resource* (counter/staff), NOT the
    service. The search is server-side filtered to a single service, so the
    service every returned slot belongs to is `service_uuid` (the one we
    searched for, supplied by the caller from the plan).
    """
    if "Session abgelaufen" in html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []
    for li in soup.find_all("li", attrs={"data-testid": SLOT_LI_TESTID_RE}):
        btn = li.find("button")
        if not btn:
            continue
        onclick = btn.get("onclick", "")
        m = APPOINTMENT_RESERVE_RE.search(onclick)
        if not m:
            continue
        encoded_dt, _duration, location_uuid, resource_uuid = m.groups()
        dt = urllib.parse.unquote(encoded_dt)
        if "T" not in dt:
            continue
        date_part, time_part = dt.split("T", 1)
        time_str = time_part[:5]
        slots.append(Slot(
            date=date_part,
            time_str=time_str,
            location_uuid=location_uuid,
            service_uuid=service_uuid,
            booking_token=encoded_dt,
            resource_uuid=resource_uuid,
        ))
    return slots


def _rewrite_8443(url: str) -> str:
    """Strip the :8443 backend port the Leipzig load balancer injects in 302s."""
    return url.replace(":8443/", "/")


def _follow_if_redirect(http: requests.Session, response):
    """If the response is a 302, GET the rewritten Location and return the followed response."""
    if getattr(response, "status_code", 200) == 302:
        loc = _rewrite_8443(response.headers.get("Location", ""))
        return http.get(loc, timeout=30, allow_redirects=False)
    return response


def _acquire_wsid(http: requests.Session, base_url: str, uid: str) -> str:
    r = http.get(
        f"{base_url}/search_result?search_mode=earliest&uid={uid}",
        timeout=30, allow_redirects=True,
    )
    if "wsid=" not in r.url:
        raise RuntimeError("wsid not found in redirect URL")
    return r.url.split("wsid=", 1)[1].split("&", 1)[0]


def _fetch_csrf_and_rev(http: requests.Session,
                        base_url: str, uid: str, wsid: str) -> tuple[str, str]:
    """Fetch the services-step page to extract __RequestVerificationToken and rev."""
    r = http.get(f"{base_url}/?uid={uid}&wsid={wsid}&lang=de",
                 timeout=30, allow_redirects=False)
    r = _follow_if_redirect(http, r)
    soup = BeautifulSoup(r.text, "html.parser")
    csrf_inp = soup.find("input", attrs={"name": "__RequestVerificationToken"})
    csrf = csrf_inp.get("value") if csrf_inp else ""
    form = (soup.find("form", attrs={"name": re.compile("_services$")})
            or soup.find("form"))
    action = form.get("action") if form else ""
    rev_m = re.search(r"rev=([^&#\"']+)", action or "")
    rev = rev_m.group(1) if rev_m else ""
    return csrf, rev


def _get_session_state(http: requests.Session, scfg: dict) -> tuple[str, str, str]:
    """Return (wsid, csrf, rev). Cached together for TTL; refreshed on miss."""
    key = (scfg["base_url"], scfg["uid"])
    cached = _WSID_CACHE.get(key)
    if cached is not None:
        wsid, csrf, rev, acquired_at = cached
        if _now() - acquired_at < _WSID_TTL_SECONDS:
            return wsid, csrf, rev
    wsid = _acquire_wsid(http, scfg["base_url"], scfg["uid"])
    csrf, rev = _fetch_csrf_and_rev(http, scfg["base_url"], scfg["uid"], wsid)
    _WSID_CACHE[key] = (wsid, csrf, rev, _now())
    return wsid, csrf, rev


def _invalidate_wsid(scfg: dict) -> None:
    _WSID_CACHE.pop((scfg["base_url"], scfg["uid"]), None)


def _post_services(http: requests.Session, wsid: str, csrf: str, rev: str,
                   plan: PollPlan, scfg: dict) -> None:
    # Submit ONLY the selected service, amount 1 — mirroring the browser's
    # `autoSelectServiceAndSubmit` (one `services=<uid>` + `service_<uid>_amount`).
    # Blasting every catalog service at once makes the server fall back to the
    # global "earliest" slot regardless of the chosen service (amount_min is 1
    # for every Leipzig service).
    sel = plan.appointment_type
    body = (
        f"__RequestVerificationToken={csrf}&"
        f"action_type=&steps={scfg['steps']}&"
        "step_current=services&step_current_index=0&step_goto=%2B1&services=&"
        f"services={sel}&service_{sel}_amount=1"
    )
    r = http.post(
        f"{scfg['base_url']}/?uid={scfg['uid']}&wsid={wsid}&lang=de&rev={rev}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body, timeout=30, allow_redirects=False,
    )
    # Follow :8443 redirect if the load balancer injects one.
    _follow_if_redirect(http, r)


def _post_locations(http: requests.Session, wsid: str, csrf: str, rev: str,
                    plan: PollPlan, catalog, scfg: dict) -> str:
    if plan.locations == "all":
        locations_all = "1"
        loc_uuids = list(catalog.locations.values())
    else:
        locations_all = ""
        loc_uuids = list(plan.locations)
    loc_parts = "&".join(f"locations={u}" for u in loc_uuids)
    body = (
        f"__RequestVerificationToken={csrf}&"
        f"action_type=search&steps={scfg['steps']}&"
        "step_current=locations&step_current_index=1&step_goto=%2B1&"
        f"locations_selected_all={locations_all}&{loc_parts}"
    )
    r = http.post(
        f"{scfg['base_url']}/?uid={scfg['uid']}&wsid={wsid}&lang=de&rev={rev}",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body, timeout=30, allow_redirects=False,
    )
    r = _follow_if_redirect(http, r)
    return r.text


def poll(plan: PollPlan, http: requests.Session) -> list[Slot]:
    """Run the Smart-CJM flow against the city's tenant. Returns parsed slots.

    Session state (wsid + csrf + rev) is cached together for `_WSID_TTL_SECONDS`,
    shared across plans on the same (base_url, uid). On `Session abgelaufen`,
    the cache is invalidated and the flow retries once.
    """
    catalog = load_catalog(plan.city)
    scfg = catalog.scraper_config
    if scfg.get("vendor") != "smartcjm":
        raise RuntimeError(
            f"city {plan.city} not configured for smartcjm scraper "
            f"(vendor={scfg.get('vendor')})"
        )
    wsid, csrf, rev = _get_session_state(http, scfg)
    _post_services(http, wsid, csrf, rev, plan, scfg)
    html = _post_locations(http, wsid, csrf, rev, plan, catalog, scfg)
    if "Session abgelaufen" in html:
        _invalidate_wsid(scfg)
        wsid, csrf, rev = _get_session_state(http, scfg)
        _post_services(http, wsid, csrf, rev, plan, scfg)
        html = _post_locations(http, wsid, csrf, rev, plan, catalog, scfg)
    return parse_slots(html, service_uuid=plan.appointment_type)
