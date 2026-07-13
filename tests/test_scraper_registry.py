"""Every city the catalog offers MUST resolve to a scraper.

run_cycle swallows per-plan scraper errors (a broken city must not take down
the others), so an unregistered city fails silently: people sign up, the
poller raises UnsupportedCity every cycle, and nobody is ever notified.
leipzig-abh shipped exactly this way — catalog present, registry entry
missing — which this invariant would have caught.
"""
from app.catalog import available_cities
from app.scrapers import get_scraper


def test_every_catalog_city_resolves_to_a_scraper():
    for city in available_cities():
        module = get_scraper(city)
        assert callable(module.poll), city
