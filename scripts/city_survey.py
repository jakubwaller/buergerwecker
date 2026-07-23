#!/usr/bin/env python3
"""Survey German 100k+ cities' appointment-booking systems.

For every city, fetch likely Termin pages on the city's own site, follow the
first booking-looking links, and classify the vendor by response signatures.
Run from a NORMAL network (residential IP) — datacenter IPs are blocked by
several vendors (Cloudflare on *.saas.smartcjm.com, the DrivePort WAF), which
would misclassify them.

    python scripts/city_survey.py                 # all cities, CSV to stdout
    python scripts/city_survey.py --only leipzig halle
    python scripts/city_survey.py --delay 2       # seconds between requests

Classes:
    smartcjm-selfhosted  -> works with our existing scraper (build now)
    smartcjm-saas        -> our scraper, but Cloudflare-fronted (gentle cadence)
    tevis                -> Kommunix TEVIS family (check captcha column!)
    netappoint | driveport | zms -> new scraper needed (zms = ask Berlin)
    unknown              -> no signature found; inspect candidates manually
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
import time
from urllib.parse import urljoin, urlsplit

import requests

# (city, population_rounded_k, site domain) — German cities >100k inhabitants.
CITIES = [
    ("Berlin", 3878, "service.berlin.de"), ("Hamburg", 1910, "www.hamburg.de"),
    ("München", 1512, "stadt.muenchen.de"), ("Köln", 1084, "www.stadt-koeln.de"),
    ("Frankfurt am Main", 773, "frankfurt.de"), ("Stuttgart", 633, "www.stuttgart.de"),
    ("Düsseldorf", 629, "www.duesseldorf.de"), ("Leipzig", 625, "www.leipzig.de"),
    ("Dortmund", 593, "www.dortmund.de"), ("Essen", 584, "www.essen.de"),
    ("Dresden", 563, "www.dresden.de"), ("Bremen", 569, "www.bremen.de"),
    ("Hannover", 545, "www.hannover.de"), ("Nürnberg", 523, "www.nuernberg.de"),
    ("Duisburg", 502, "www.duisburg.de"), ("Bochum", 365, "www.bochum.de"),
    ("Wuppertal", 358, "www.wuppertal.de"), ("Bielefeld", 338, "www.bielefeld.de"),
    ("Bonn", 336, "www.bonn.de"), ("Münster", 320, "www.stadt-muenster.de"),
    ("Mannheim", 315, "www.mannheim.de"), ("Karlsruhe", 308, "www.karlsruhe.de"),
    ("Augsburg", 301, "www.augsburg.de"), ("Wiesbaden", 283, "www.wiesbaden.de"),
    ("Mönchengladbach", 268, "www.moenchengladbach.de"),
    ("Gelsenkirchen", 263, "www.gelsenkirchen.de"), ("Aachen", 252, "www.aachen.de"),
    ("Braunschweig", 249, "www.braunschweig.de"), ("Chemnitz", 248, "www.chemnitz.de"),
    ("Kiel", 246, "www.kiel.de"), ("Halle (Saale)", 242, "halle.de"),
    ("Magdeburg", 239, "www.magdeburg.de"), ("Freiburg", 236, "www.freiburg.de"),
    ("Krefeld", 228, "www.krefeld.de"), ("Mainz", 220, "www.mainz.de"),
    ("Lübeck", 218, "www.luebeck.de"), ("Erfurt", 214, "www.erfurt.de"),
    ("Oberhausen", 210, "www.oberhausen.de"), ("Rostock", 209, "www.rostock.de"),
    ("Kassel", 204, "www.kassel.de"), ("Hagen", 189, "www.hagen.de"),
    ("Potsdam", 187, "www.potsdam.de"), ("Saarbrücken", 181, "www.saarbruecken.de"),
    ("Hamm", 180, "www.hamm.de"), ("Ludwigshafen", 174, "www.ludwigshafen.de"),
    ("Mülheim an der Ruhr", 172, "www.muelheim-ruhr.de"),
    ("Oldenburg", 172, "www.oldenburg.de"), ("Osnabrück", 167, "www.osnabrueck.de"),
    ("Leverkusen", 167, "www.leverkusen.de"), ("Darmstadt", 165, "www.darmstadt.de"),
    ("Heidelberg", 162, "www.heidelberg.de"), ("Solingen", 160, "www.solingen.de"),
    ("Herne", 157, "www.herne.de"), ("Neuss", 154, "www.neuss.de"),
    ("Regensburg", 157, "www.regensburg.de"), ("Paderborn", 155, "www.paderborn.de"),
    ("Ingolstadt", 142, "www.ingolstadt.de"), ("Offenbach", 132, "www.offenbach.de"),
    ("Fürth", 131, "www.fuerth.de"), ("Würzburg", 128, "www.wuerzburg.de"),
    ("Ulm", 128, "www.ulm.de"), ("Heilbronn", 126, "www.heilbronn.de"),
    ("Pforzheim", 126, "www.pforzheim.de"), ("Wolfsburg", 125, "www.wolfsburg.de"),
    ("Göttingen", 117, "www.goettingen.de"), ("Bottrop", 117, "www.bottrop.de"),
    ("Reutlingen", 117, "www.reutlingen.de"), ("Koblenz", 115, "www.koblenz.de"),
    ("Erlangen", 113, "www.erlangen.de"), ("Bremerhaven", 113, "www.bremerhaven.de"),
    ("Recklinghausen", 112, "www.recklinghausen.de"),
    ("Bergisch Gladbach", 112, "www.bergischgladbach.de"), ("Jena", 110, "www.jena.de"),
    ("Remscheid", 111, "www.remscheid.de"), ("Trier", 111, "www.trier.de"),
    ("Salzgitter", 106, "www.salzgitter.de"), ("Moers", 104, "www.moers.de"),
    ("Siegen", 102, "www.siegen.de"), ("Hildesheim", 101, "www.hildesheim.de"),
    ("Gütersloh", 101, "www.guetersloh.de"),
    ("Kaiserslautern", 100, "www.kaiserslautern.de"),
]

# Paths worth trying on each city site, in order.
CANDIDATE_PATHS = ["/termin", "/termine", "/terminvereinbarung",
                   "/buergerservice/terminvereinbarung", "/buergeramt",
                   "/serviceportal", "/"]

TERMIN_LINK_RE = re.compile(
    r'https?://[^\s"\'<>]*(?:termin|tevis|reservieren|smartcjm|netappoint|'
    r'driveport|calendar)[^\s"\'<>]*', re.I)

SIGNATURES = [   # (class, regex on final URL or body)
    ("smartcjm-saas", re.compile(r"saas\.smartcjm\.com", re.I)),
    ("smartcjm-selfhosted", re.compile(r"extern/calendar|smart-cjm|smartcjm", re.I)),
    ("tevis", re.compile(r"tevis|kommunix|termine-reservieren\.de", re.I)),
    ("netappoint", re.compile(r"netappoint", re.I)),
    ("driveport", re.compile(r"driveport|termineapi", re.I)),
    ("zms", re.compile(r"service\.berlin\.de/terminvereinbarung", re.I)),
]
CAPTCHA_RE = re.compile(r"captcha|turnstile|friendlycaptcha|hcaptcha|recaptcha", re.I)


def classify(url: str, body: str) -> str | None:
    hay = url + "\n" + body[:200_000]
    for name, rx in SIGNATURES:
        if rx.search(hay):
            return name
    return None


def survey_city(http: requests.Session, name: str, domain: str,
                delay: float) -> dict:
    seen_hosts: set[str] = set()
    result = {"city": name, "class": "unknown", "captcha": "",
              "booking_url": "", "notes": ""}
    for path in CANDIDATE_PATHS:
        url = f"https://{domain}{path}"
        try:
            r = http.get(url, timeout=15, allow_redirects=True)
        except requests.RequestException as exc:
            result["notes"] = f"{path}: {type(exc).__name__}"
            continue
        time.sleep(delay)
        cls = classify(r.url, r.text)
        if cls:
            result.update({"class": cls, "booking_url": r.url})
            result["captcha"] = "yes" if CAPTCHA_RE.search(r.text) else ""
            return result
        # Follow up to 3 termin-looking links from this page.
        for link in list(dict.fromkeys(TERMIN_LINK_RE.findall(r.text)))[:3]:
            host = urlsplit(link).netloc
            if host in seen_hosts:
                continue
            seen_hosts.add(host)
            try:
                r2 = http.get(urljoin(r.url, link), timeout=15,
                              allow_redirects=True)
            except requests.RequestException:
                continue
            time.sleep(delay)
            cls = classify(r2.url, r2.text)
            if cls:
                result.update({"class": cls, "booking_url": r2.url})
                result["captcha"] = "yes" if CAPTCHA_RE.search(r2.text) else ""
                return result
            if not result["booking_url"]:
                result["booking_url"] = r2.url   # best candidate for manual look
        if r.status_code == 200 and path != "/":
            break   # found a real termin page but no signature; stop guessing
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="substring filter on city names")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds to sleep after each request (be polite)")
    args = ap.parse_args()
    http = requests.Session()
    http.headers["User-Agent"] = "Buergerwecker/city-survey (+https://buergerwecker.de)"
    w = csv.writer(sys.stdout)
    w.writerow(["city", "pop_k", "class", "captcha", "booking_url", "notes"])
    for name, pop, domain in CITIES:
        if args.only and not any(o.lower() in name.lower() for o in args.only):
            continue
        res = survey_city(http, name, domain, args.delay)
        w.writerow([res["city"], pop, res["class"], res["captcha"],
                    res["booking_url"], res["notes"]])
        sys.stdout.flush()


if __name__ == "__main__":
    main()
