from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Callable
from bs4 import BeautifulSoup
import requests


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- public API ----------

def fetch_services(http: requests.Session,
                   base_url: str, uid: str) -> dict[str, str]:
    r = http.get(f"{base_url}/get_service_list?uid={uid}", timeout=30)
    data = r.json()
    if not data.get("success"):
        raise RuntimeError("get_service_list returned success=false")
    out: dict[str, str] = {}
    for s in data["results"]:
        name = (s.get("display_name") or "").strip()
        if name:
            out[name] = s["uid"]
    return dict(sorted(out.items()))


def fetch_locations(http: requests.Session,
                    base_url: str, uid: str,
                    service_uids: list[str], steps: str) -> dict[str, str]:
    union_by_uid: dict[str, str] = {}
    for svc in service_uids:
        try:
            locs = _probe_one_service(http, base_url, uid, svc, service_uids, steps)
        except Exception:
            continue
        for loc_uid, loc_name in locs.items():
            union_by_uid.setdefault(loc_uid, loc_name)
    return {n: u for u, n in sorted(union_by_uid.items(), key=lambda kv: kv[1])}


def sync_city(city: str,
              http: requests.Session,
              alert_fn: Callable,
              catalog_root: Path | None = None) -> dict:
    root = Path(catalog_root) if catalog_root else REPO_ROOT / "catalog"
    city_dir = root / city
    scfg = json.loads((city_dir / "scraper_config.json").read_text())
    svc_path = city_dir / "appointment_type.json"
    loc_path = city_dir / "locations.json"
    current_services = json.loads(svc_path.read_text())
    current_locations = json.loads(loc_path.read_text())

    try:
        live_services = fetch_services(http, scfg["base_url"], scfg["uid"])
        live_locations = fetch_locations(http, scfg["base_url"], scfg["uid"],
                                          list(live_services.values()),
                                          scfg["steps"])
    except (requests.RequestException, RuntimeError) as exc:
        return {"error": str(exc),
                "service_drift": {}, "location_drift": {}}

    service_drift = _diff(current_services, live_services)
    location_drift = _diff(current_locations, live_locations)

    if service_drift:
        _atomic_write_json(svc_path, live_services)
    if location_drift:
        _atomic_write_json(loc_path, live_locations)
    if service_drift or location_drift:
        alert_fn(city=city,
                 service_drift=service_drift,
                 location_drift=location_drift)

    return {"service_drift": service_drift,
            "location_drift": location_drift}


# ---------- internals ----------

def _probe_one_service(http: requests.Session,
                       base_url: str, uid: str,
                       target_uid: str,
                       all_service_uids: list[str],
                       steps: str) -> dict[str, str]:
    # 1. wsid acquire
    r0 = http.get(f"{base_url}/search_result?search_mode=earliest&uid={uid}",
                  timeout=30, allow_redirects=True)
    wsid = r0.url.split("wsid=", 1)[1].split("&", 1)[0]

    # 2. fetch services page for CSRF + dynamic rev
    r1 = http.get(f"{base_url}/?uid={uid}&wsid={wsid}&lang=de",
                  timeout=30, allow_redirects=False)
    if getattr(r1, "status_code", 200) == 302:
        r1 = http.get(_rewrite_8443(r1.headers["Location"]),
                      timeout=30, allow_redirects=False)
    soup = BeautifulSoup(r1.text, "html.parser")
    csrf_inp = soup.find("input", attrs={"name": "__RequestVerificationToken"})
    csrf = csrf_inp.get("value") if csrf_inp else ""
    form = (soup.find("form", attrs={"name": re.compile("_services$")})
            or soup.find("form"))
    rev_m = re.search(r"rev=([^&#\"']+)", (form.get("action") if form else "") or "")
    rev = rev_m.group(1) if rev_m else "HL0Ur"

    # 3. POST the services step with target_uid amount=1, all others amount=""
    parts = []
    for u in all_service_uids:
        parts.append(f"services={u}")
        parts.append(f"service_{u}_amount={'1' if u == target_uid else ''}")
    body = ("__RequestVerificationToken=" + csrf
            + f"&action_type=&steps={steps}"
            + "&step_current=services&step_current_index=0&step_goto=%2B1&services=&"
            + "&".join(parts))
    r2 = http.post(f"{base_url}/?uid={uid}&wsid={wsid}&lang=de&rev={rev}",
                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                   data=body, timeout=30, allow_redirects=False)
    if getattr(r2, "status_code", 200) == 302:
        page = http.get(_rewrite_8443(r2.headers["Location"]),
                        timeout=30, allow_redirects=False).text
    else:
        page = r2.text

    # 4. parse location checkboxes from the locations-step HTML
    return _parse_location_checkboxes(page)


def _parse_location_checkboxes(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    for cb in soup.find_all("input", attrs={"type": "checkbox", "name": "locations"}):
        loc_uid = (cb.get("value") or "").strip()
        if not loc_uid:
            continue
        lbl = soup.find("label", attrs={"for": cb.get("id")})
        if not lbl:
            continue
        for line in lbl.text.split("\n"):
            line = line.strip()
            if line:
                out[loc_uid] = line
                break
    return out


def _rewrite_8443(url: str) -> str:
    """Strip the :8443 backend port the Leipzig load balancer sometimes injects."""
    return url.replace(":8443/", "/")


def _diff(old: dict[str, str], new: dict[str, str]) -> dict:
    """Symmetric diff. Returns {} if dicts are equal."""
    if old == new:
        return {}
    old_keys = set(old)
    new_keys = set(new)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    renamed_or_remapped = []
    for k in old_keys & new_keys:
        if old[k] != new[k]:
            renamed_or_remapped.append({"name": k, "old_uid": old[k], "new_uid": new[k]})
    result: dict = {}
    if added: result["added"] = added
    if removed: result["removed"] = removed
    if renamed_or_remapped: result["changed_uid"] = renamed_or_remapped
    return result


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
