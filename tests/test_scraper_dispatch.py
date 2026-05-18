import pytest
from app.scrapers import get_scraper, UnsupportedCity

def test_get_scraper_leipzig():
    scraper = get_scraper("leipzig")
    assert hasattr(scraper, "poll")

def test_get_scraper_unknown():
    with pytest.raises(UnsupportedCity):
        get_scraper("atlantis")
