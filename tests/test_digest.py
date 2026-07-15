import re
from datetime import datetime, time
from unittest.mock import patch
import pytest
from app.db import connect, init_schema
from app.catalog import Catalog
from app.models import Filter, Slot, Subscription
from app.digest import render_digest_text, _format_date

# A rendered slot line: two-space indent, weekday + dd.mm., time — optionally
# followed by a per-slot service label (multi-service filters only).
SLOT_LINE = re.compile(r"^  \S+ \d{2}\.\d{2}\.  \d{2}:\d{2}(?:$|  ·  )")


def _slot_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if SLOT_LINE.match(ln)]


def _sub(language="de", appointment_types=("svc-A",), locations="all"):
    locs = "all" if locations == "all" else list(locations)
    return Subscription(
        id=1, email="a@x.com", city="leipzig", language=language,
        sub_filter=Filter(
            appointment_types=list(appointment_types), locations=locs,
            weekdays=[1, 2, 3, 4, 5, 6, 7],
            time_window_start=time(0, 0), time_window_end=time(23, 59),
        ),
        created_at=datetime(2026, 5, 1), confirmed_at=datetime(2026, 5, 1),
        last_notified_at=None,
        expires_at=datetime(2026, 8, 1),
        reminder_sent_at=None, heartbeat_30d_at=None, heartbeat_60d_at=None,
        deleted_at=None,
    )


def _cat():
    return Catalog(
        city="leipzig",
        appointment_types={"Personalausweis": "svc-A", "Reisepass": "svc-B"},
        locations={"Bürgerbüro Mitte": "loc-1", "Bürgerbüro Nord": "loc-2"},
        scraper_config={},
        appointment_types_en={"Identity card": "svc-A", "Passport": "svc-B"},
        locations_en={"Citizen office centre": "loc-1"},  # loc-2 EN missing on purpose
    )


def _render(sub, slots, *, catalog=None):
    return render_digest_text(
        sub, slots, unsubscribe_url="https://x/unsubscribe/tok",
        public_base_url="https://x", kofi_url="https://ko-fi.com/me",
        catalog=catalog)


# ---------- existing baseline (no catalog → uuid fallback path) ----------

def test_render_digest_de():
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    text = _render(_sub("de"), slots)
    assert "10.06." in text          # weekday + dd.mm. (2026-06-10 is a Wednesday)
    assert "10:30" in text
    assert "schnell vergriffen" in text            # burst-congestion line
    assert "nur neue Termine" in text              # no-repeat note
    assert "https://x/unsubscribe/tok" in text


def test_render_digest_en():
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    text = _render(_sub("en"), slots)
    assert "taken quickly" in text.lower()
    assert "new since your last notification" in text


# ---------- "Deine Auswahl" selection header ----------

def test_selection_header_shows_service_and_locations_de():
    sub = _sub("de", appointment_types=["svc-A"], locations=["loc-1", "loc-2"])
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA"),
             Slot("2026-06-13", "08:00", "loc-2", "svc-A", "tB")]
    text = _render(sub, slots, catalog=_cat())
    assert "Deine Auswahl" in text
    assert "Personalausweis" in text
    assert "Bürgerbüro Mitte" in text
    assert "Bürgerbüro Nord" in text


def test_selection_header_all_locations_label():
    sub = _sub("de", appointment_types=["svc-A"], locations="all")
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA")]
    text = _render(sub, slots, catalog=_cat())
    assert "Alle Standorte" in text


def test_selection_header_english_labels():
    sub = _sub("en", appointment_types=["svc-A"], locations=["loc-1"])
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA")]
    text = _render(sub, slots, catalog=_cat())
    assert "Your selection" in text
    assert "Identity card" in text
    assert "Citizen office centre" in text


# ---------- per-office grouping ----------

def test_slots_grouped_by_office():
    sub = _sub("de", appointment_types=["svc-A"], locations="all")
    slots = [
        Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA"),
        Slot("2026-06-12", "10:40", "loc-1", "svc-A", "tB"),
        Slot("2026-06-13", "08:00", "loc-2", "svc-A", "tC"),
    ]
    text = _render(sub, slots, catalog=_cat())
    mitte = text.index("Bürgerbüro Mitte")
    nord = text.index("Bürgerbüro Nord")
    assert mitte < nord                       # offices sorted by name
    assert mitte < text.index("09:20") < nord  # Mitte's slots under its header
    assert mitte < text.index("10:40") < nord
    assert text.index("08:00") > nord          # Nord's slot under its header


def test_slot_line_has_weekday_date_and_time():
    sub = _sub("de", appointment_types=["svc-A"], locations="all")
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tok-A")]
    text = _render(sub, slots, catalog=_cat())
    line = next(ln for ln in text.splitlines() if "09:20" in ln)
    assert "Fr 12.06." in line                       # 2026-06-12 is a Friday
    assert "tok-A" not in text                       # booking token never renders


def test_digest_links_once_to_city_booking_page():
    """Per-slot deep links are gone (session-bound upstream — they only ever
    landed on the start page). The digest carries exactly ONE booking link,
    the never-expiring city-level /go/<city>."""
    sub = _sub("de", appointment_types=["svc-A"], locations=["loc-1"])
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA"),
             Slot("2026-06-13", "10:00", "loc-2", "svc-A", "tB")]
    text = _render(sub, slots, catalog=_cat())
    assert "https://x/go/leipzig" in text
    assert "/go/leipzig:" not in text
    assert text.count("/go/") == 1


def test_digest_instructions_name_service_and_location():
    sub = _sub("de", appointment_types=["svc-A"], locations=["loc-1"])
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA")]
    text = _render(sub, slots, catalog=_cat())
    line = next(ln for ln in text.splitlines() if ln.startswith("Wähle dort"))
    assert "Anliegen „Personalausweis“" in line
    assert "Standort „Bürgerbüro Mitte“" in line
    assert "Direktlink" in text                      # honesty note present


def test_digest_instructions_drop_location_for_single_location_tenant():
    """Tenants with one location (e.g. leipzig-abh) have no location step in
    the booking flow — the instruction must not tell users to pick one."""
    single_loc = Catalog(
        city="leipzig", appointment_types={"Abholung": "svc-A"},
        locations={"Otto-Schill-Straße": "loc-1"}, scraper_config={})
    sub = _sub("de", appointment_types=["svc-A"], locations="all")
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA")]
    text = _render(sub, slots, catalog=single_loc)
    line = next(ln for ln in text.splitlines() if ln.startswith("Wähle dort"))
    assert "Anliegen „Abholung“" in line
    assert "dann den Standort" not in line


def test_digest_en_booking_link_carries_lang_param():
    sub = _sub("en", appointment_types=["svc-A"], locations=["loc-1"])
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA")]
    text = _render(sub, slots, catalog=_cat())
    assert "https://x/go/leipzig?lang=en" in text


# ---------- per-slot service only when the filter spans >1 type ----------

def test_multi_service_filter_labels_each_line():
    sub = _sub("de", appointment_types=["svc-A", "svc-B"], locations="all")
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA"),
             Slot("2026-06-12", "10:40", "loc-1", "svc-B", "tB")]
    text = _render(sub, slots, catalog=_cat())
    line_a = next(ln for ln in text.splitlines() if "09:20" in ln)
    line_b = next(ln for ln in text.splitlines() if "10:40" in ln)
    assert "Personalausweis" in line_a
    assert "Reisepass" in line_b


def test_single_service_filter_omits_per_line_service():
    sub = _sub("de", appointment_types=["svc-A"], locations="all")
    slots = [Slot("2026-06-12", "09:20", "loc-1", "svc-A", "tA")]
    text = _render(sub, slots, catalog=_cat())
    line = next(ln for ln in text.splitlines() if "09:20" in ln)
    assert "Personalausweis" not in line   # header already names the one service


# ---------- robustness ----------

def test_out_of_catalog_location_uuid_renders_uuid_not_crash():
    sub = _sub("de", appointment_types=["svc-A"], locations="all")
    slots = [Slot("2026-06-12", "09:20", "ghost-loc", "svc-A", "tA")]
    text = _render(sub, slots, catalog=_cat())
    assert "ghost-loc" in text  # raw uuid as the office header, no exception


def test_digest_echoes_max_days_ahead_window():
    from dataclasses import replace
    sub = _sub("de")
    sub = replace(sub, sub_filter=replace(sub.sub_filter, max_days_ahead=7))
    slots = [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")]
    text = _render(sub, slots, catalog=_cat())
    assert "innerhalb der nächsten 7 Tage" in text
    text_en = _render(replace(sub, language="en"), slots, catalog=_cat())
    assert "within the next 7 days" in text_en


def test_digest_omits_window_line_when_unlimited():
    text = _render(_sub("de"), [Slot("2026-06-10", "10:30", "loc-1", "svc-A", "t")],
                   catalog=_cat())
    assert "Zeitraum" not in text


def test_digest_caps_slots_at_soonest_and_summarizes_rest():
    """More matches than MAX_SLOTS_PER_DIGEST → render only the soonest N and
    one count line for the rest (keeps abundant tenants under Gmail's ~102KB
    clipping threshold). The soonest slot must survive the cut; the latest
    must not."""
    from app.digest import MAX_SLOTS_PER_DIGEST
    n_total = MAX_SLOTS_PER_DIGEST + 40
    # Distinct (date, time) per slot so rendered lines are unique.
    slots = [Slot(f"2026-07-{(i % 28) + 1:02d}", f"{8 + (i % 10):02d}:{i % 60:02d}",
                  "loc-1", "svc-A", f"tok-{i}") for i in range(n_total)]
    text = _render(_sub("de"), slots, catalog=_cat())
    assert len(_slot_lines(text)) == MAX_SLOTS_PER_DIGEST
    assert "40 weitere passende Termine" in text
    # soonest-first selection: the earliest is in, the latest is out.
    soonest = min(slots, key=lambda s: (s.date, s.time_str))
    latest = max(slots, key=lambda s: (s.date, s.time_str))
    assert f"  {_format_date(soonest.date, 'de')}  {soonest.time_str}" in text
    assert f"  {_format_date(latest.date, 'de')}  {latest.time_str}" not in text
    # sanity: body stays far below Gmail's clipping threshold
    assert len(text.encode()) < 20_000


def test_digest_no_summary_line_when_under_cap():
    from app.digest import MAX_SLOTS_PER_DIGEST
    slots = [Slot("2026-07-01", f"{9 + i // 60:02d}:{i % 60:02d}", "loc-1",
                  "svc-A", f"t{i}") for i in range(MAX_SLOTS_PER_DIGEST)]
    text = _render(_sub("de"), slots, catalog=_cat())
    assert "weitere passende Termine" not in text
    assert len(_slot_lines(text)) == MAX_SLOTS_PER_DIGEST
    assert text.count("/go/") == 1                   # just the city booking link
