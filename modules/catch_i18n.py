"""Small, dependency-free localization loader for Catch Phase 1 UI."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_LOCALE_DIR = Path(__file__).resolve().parent.parent / "locales"


@lru_cache(maxsize=16)
def _load(code: str) -> dict[str, Any]:
    path = _LOCALE_DIR / f"{code.lower().replace('-', '_')}.json"
    if not path.is_file():
        path = _LOCALE_DIR / "en.json"
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def text(key: str, *, locale: str = "en", **values: object) -> str:
    """Return a catch.* key, falling back per-key to English."""
    normalized = key if key.startswith("catch.") else f"catch.{key}"
    current = _load(locale)
    english = _load("en")
    value = current.get(normalized, english.get(normalized, normalized))
    return value.format(**values) if isinstance(value, str) else normalized


def clear_cache() -> None:
    _load.cache_clear()


__all__ = ["clear_cache", "text"]
