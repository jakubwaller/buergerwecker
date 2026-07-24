from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Callable
from bs4 import BeautifulSoup
import requests


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------- public API ----------

def fetch_services(http: requests.Session,
                   base_url: str, uid: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (german, english) name→uuid maps.

    The lang query param is ignored by get_service_list — German is always in
    `display_name`, and the English label (when present) is in
    `data.display_name_en`. A service without an English label falls back to
    its German name so the English map always covers the full set.
    """
    r = http.get(f"{base_url}/get_service_list?uid={uid}", timeout=30)
    data = r.json()
    if not data.get("success"):
        raise RuntimeError("get_service_list returned success=false")
    de: dict[str, str] = {}
    en: dict[str, str] = {}
    for s in data["results"]:
        name = (s.get("display_name") or "").strip()
        if not name:
            continue
        de[name] = s["uid"]
        name_en = ((s.get("data") or {}).get("display_name_en") or "").strip() or name
        en[name_en] = s["uid"]
    return dict(sorted(de.items())), dict(sorted(en.items()))


def fetch_locations(http: requests.Session,
                    base_url: str, uid: str,
                    service_uids: list[str], steps: str,
                    lang: str = "de") -> dict[str, str]:
    union, _ = fetch_locations_with_map(http, base_url, uid, service_uids,
                                        steps, lang=lang)
    return union


def fetch_locations_with_map(http: requests.Session,
                             base_url: str, uid: str,
                             service_uids: list[str], steps: str,
                             lang: str = "de",
                             ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Union name→uuid map plus the per-service coverage map.

    The second element maps service uuid → sorted location uuids that offer
    it (the sign-up form uses it to hide non-offering offices). A service
    whose probe fails is left out of the map entirely — absent means
    "unknown, show everything", never "offered nowhere".
    """
    union_by_uid: dict[str, str] = {}
    per_service: dict[str, list[str]] = {}
    for svc in service_uids:
        try:
            locs = _probe_one_service(http, base_url, uid, svc, service_uids,
                                      steps, lang=lang)
        except Exception:
            continue
        per_service[svc] = sorted(locs)
        for loc_uid, loc_name in locs.items():
            union_by_uid.setdefault(loc_uid, loc_name)
    return ({n: u for u, n in sorted(union_by_uid.items(), key=lambda kv: kv[1])},
            per_service)


def sync_city(city: str,
              http: requests.Session,
              alert_fn: Callable,
              catalog_root: Path | None = None) -> dict:
    root = Path(catalog_root) if catalog_root else REPO_ROOT / "catalog"
    city_dir = root / city
    scfg = json.loads((city_dir / "scraper_config.json").read_text())
    if scfg.get("vendor") == "tevis":
        return _sync_tevis(city, city_dir, scfg, http, alert_fn)
    # The Smart-CJM path below discovers services/locations by driving the
    # booking wizard; a vendor without a sync implementation keeps its static
    # catalog rather than running Smart-CJM HTTP against a config that has no
    # `uid`/`steps`.
    if scfg.get("vendor") != "smartcjm":
        return {"skipped": f"vendor {scfg.get('vendor')}",
                "service_drift": {}, "location_drift": {}}
    svc_path = city_dir / "appointment_type.json"
    loc_path = city_dir / "locations.json"
    svc_en_path = city_dir / "appointment_type.en.json"
    loc_en_path = city_dir / "locations.en.json"
    current_services = json.loads(svc_path.read_text())
    current_locations = json.loads(loc_path.read_text())

    # Single-location tenants (e.g. leipzig-abh-h) have no locations step in
    # their flow, so there is nothing to probe: the location list is static in
    # the catalog and never drift-checked. The topology decision is shared
    # with the poller (smartcjm._run_flow) so the two can never disagree.
    from app.scrapers.smartcjm import has_locations_step

    try:
        live_services, live_services_en = fetch_services(http, scfg["base_url"], scfg["uid"])
        if has_locations_step(scfg):
            live_locations, service_map = fetch_locations_with_map(
                http, scfg["base_url"], scfg["uid"],
                list(live_services.values()), scfg["steps"])
        else:
            # Single-location tenants have no locations step: nothing to
            # probe, and a coverage map would be meaningless.
            live_locations, service_map = current_locations, None
    except (requests.RequestException, RuntimeError) as exc:
        return {"error": str(exc),
                "service_drift": {}, "location_drift": {}}

    service_drift = _diff(current_services, live_services)
    location_drift = _diff(current_locations, live_locations)

    # Drift detection and alerting stay anchored on the German (canonical)
    # catalog. The English label files are rewritten alongside their German
    # counterparts so the two can never diverge in uuid set. English fetches
    # are best-effort: a failure must never leave the German files unwritten.
    if service_drift:
        _atomic_write_json(svc_path, live_services)
        _atomic_write_json(svc_en_path, live_services_en)
    if location_drift:
        _atomic_write_json(loc_path, live_locations)
        try:
            live_locations_en = fetch_locations(http, scfg["base_url"], scfg["uid"],
                                                 list(live_services.values()),
                                                 scfg["steps"], lang="en")
            if live_locations_en:
                _atomic_write_json(loc_en_path, live_locations_en)
        except requests.RequestException:
            pass  # German file already written; English stays at its last good value
    if service_map is not None:
        _write_service_map_if_changed(city_dir, service_map)
    if service_drift or location_drift:
        alert_fn(city=city,
                 service_drift=service_drift,
                 location_drift=location_drift)

    return {"service_drift": service_drift,
            "location_drift": location_drift}


# ---------- TEVIS ----------

def fetch_tevis_services(http: requests.Session,
                         base_url: str, md: str) -> dict[str, str]:
    """Parse the Anliegen page (`/select2?md=`) into a name→id map.

    Services render as `cnc-<id>` amount inputs with a label keyed to the
    input's element id. This GET also mints the session cookie the
    `/location` probes below need. An empty result means the page layout
    changed (or a help page was served) — callers must treat it as an error,
    not as "the city deleted every service".
    """
    r = http.get(f"{base_url}/select2", params={"md": md}, timeout=30)
    soup = BeautifulSoup(r.text, "html.parser")
    out: dict[str, str] = {}
    for inp in soup.find_all("input"):
        name = inp.get("name") or ""
        if not name.startswith("cnc-"):
            continue
        sid = name[len("cnc-"):].strip()
        lbl = soup.find("label", attrs={"for": inp.get("id")})
        label = " ".join(lbl.get_text(" ", strip=True).split()) if lbl else ""
        if sid and label:
            out[label] = sid
    return dict(sorted(out.items()))


def fetch_tevis_locations(http: requests.Session, base_url: str, mdt: str,
                          service_ids: list[str],
                          ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(union id→name, per-service coverage map) across all services.

    The `/location` page lists only offices offering the selected service, so
    the union across services is the complete office set. Office cards are
    <form>s with a hidden `loc` input; the first non-empty text line is the
    office name (map-marker duplicates have empty text and are skipped by the
    setdefault). Mirrors fetch_locations_with_map's failure semantics: a
    service whose probe fails is absent from the map, not empty.
    """
    union: dict[str, str] = {}
    per_service: dict[str, list[str]] = {}
    for i, sid in enumerate(service_ids):
        if i:
            time.sleep(0.5)  # politeness: this loop runs once per city per day
        try:
            r = http.get(f"{base_url}/location",
                         params={"mdt": mdt, "select_cnc": "1",
                                 f"cnc-{sid}": "1"},
                         timeout=30)
        except requests.RequestException:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        locs: set[str] = set()
        for form in soup.find_all("form"):
            inp = form.find("input", attrs={"name": "loc"})
            if inp is None:
                continue
            loc = (inp.get("value") or "").strip()
            if not loc:
                continue
            label = _tevis_office_label(form)
            if not label:
                continue
            locs.add(loc)
            union.setdefault(loc, label)
        per_service[sid] = sorted(locs)
    return union, per_service


def _tevis_office_label(form) -> str:
    """Office display name from a TEVIS office card.

    The card is a <dl> of labelled rows ("Name", "Anschrift", "Nächster
    Termin", …) — the name is the <dd> right after the "Name" <dt>. Falls
    back to the first non-empty text line for layouts without a <dl>. Map-
    marker duplicate forms have no text at all and yield ""."""
    for dt in form.find_all("dt"):
        if " ".join(dt.get_text(" ", strip=True).split()) == "Name":
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                return " ".join(dd.get_text(" ", strip=True).split())
    lines = (" ".join(l.split())
             for l in form.get_text("\n", strip=True).split("\n"))
    return next((l for l in lines if l), "")


def _sync_tevis(city: str, city_dir: Path, scfg: dict,
                http: requests.Session, alert_fn: Callable) -> dict:
    svc_path = city_dir / "appointment_type.json"
    loc_path = city_dir / "locations.json"
    current_services = json.loads(svc_path.read_text())
    current_locations = json.loads(loc_path.read_text())

    try:
        live_services = fetch_tevis_services(http, scfg["base_url"],
                                             str(scfg["md"]))
    except requests.RequestException as exc:
        return {"error": str(exc), "service_drift": {}, "location_drift": {}}
    if not live_services:
        return {"error": "no services parsed from select2 page",
                "service_drift": {}, "location_drift": {}}

    # Probe locations for every live service so a brand-new service's offices
    # are seen too. Union across catalog + live ids is unnecessary: an id
    # gone from the page can't be probed anyway.
    union_by_id, service_map = fetch_tevis_locations(
        http, scfg["base_url"], str(scfg["mdt"]),
        sorted(set(live_services.values())))
    if not union_by_id:
        return {"error": "no offices parsed from location pages",
                "service_drift": {}, "location_drift": {}}

    # Catalog display names are hand-curated at onboarding (e.g. stripped
    # prefixes), so keep the existing name for known ids — otherwise every
    # sync would "drift" on cosmetic label differences. Live names are used
    # only for genuinely new ids.
    live_services = _keep_catalog_names(current_services, live_services)
    live_locations = _keep_catalog_names(
        current_locations,
        {name: lid for lid, name in union_by_id.items()})

    service_drift = _diff(current_services, live_services)
    location_drift = _diff(current_locations, live_locations)
    if service_drift:
        _atomic_write_json(svc_path, live_services)
    if location_drift:
        _atomic_write_json(loc_path, live_locations)
    _write_service_map_if_changed(city_dir, service_map)
    if service_drift or location_drift:
        alert_fn(city=city,
                 service_drift=service_drift,
                 location_drift=location_drift)
    return {"service_drift": service_drift,
            "location_drift": location_drift}


def _keep_catalog_names(current: dict[str, str],
                        live: dict[str, str]) -> dict[str, str]:
    """Rebuild `live` (name→id) preferring the catalog's display name for ids
    the catalog already knows."""
    cur_by_id = {i: n for n, i in current.items()}
    return dict(sorted((cur_by_id.get(i, n), i) for n, i in live.items()))


def _write_service_map_if_changed(city_dir: Path,
                                  service_map: dict[str, list[str]]) -> None:
    """Persist the per-service location coverage map when its content moved.

    Written even when nothing else drifted: coverage can change (an office
    starts offering a service) without the office/service sets changing.
    """
    path = city_dir / "service_locations.json"
    data = {sid: sorted(locs) for sid, locs in sorted(service_map.items())}
    try:
        if json.loads(path.read_text()) == data:
            return
    except (FileNotFoundError, ValueError):
        pass
    _atomic_write_json(path, data)


# ---------- internals ----------

def _probe_one_service(http: requests.Session,
                       base_url: str, uid: str,
                       target_uid: str,
                       all_service_uids: list[str],
                       steps: str,
                       lang: str = "de") -> dict[str, str]:
    # 1. wsid acquire
    r0 = http.get(f"{base_url}/search_result?search_mode=earliest&uid={uid}",
                  timeout=30, allow_redirects=True)
    wsid = r0.url.split("wsid=", 1)[1].split("&", 1)[0]

    # 2. fetch services page for CSRF + dynamic rev
    r1 = http.get(f"{base_url}/?uid={uid}&wsid={wsid}&lang={lang}",
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
    r2 = http.post(f"{base_url}/?uid={uid}&wsid={wsid}&lang={lang}&rev={rev}",
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
            # Collapse runs of whitespace — the English wizard emits labels like
            # "Resident Services Office  Leutzsch" with a stray double space.
            line = " ".join(line.split())
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
