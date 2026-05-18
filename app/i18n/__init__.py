from __future__ import annotations
import json
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
