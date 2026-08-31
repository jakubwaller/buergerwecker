from __future__ import annotations
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

I18N_ROOT = Path(__file__).parent

@lru_cache(maxsize=4)
def load(lang: str) -> dict[str, str]:
    if lang not in ("de", "en"):
        lang = "de"
    return json.loads((I18N_ROOT / f"{lang}.json").read_text(encoding="utf-8"))

def t(lang: str, key: str, **kwargs) -> str:
    bundle = load(lang)
    template = bundle.get(key, key)
    return template.format(**kwargs)

def format_date(d: date, lang: str) -> str:
    """One date format per language, shared by every page and mail that names
    a subscription's end date, so they can never disagree about the same day."""
    if lang == "en":
        return f"{d.day} {d.strftime('%B %Y')}"
    return d.strftime("%d.%m.%Y")
