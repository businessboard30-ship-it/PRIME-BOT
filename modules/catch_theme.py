"""Catch game colours and button styles (rule 0.10, P1-03).

Views never hardcode a ``discord.ButtonStyle`` or hex value; they ask this
module by role/state name. Defaults come from data/catch/theme.json; the
owner's edits live in catch_theme and are applied with ``refresh_overrides``.
Button roles are not editable (Discord only has four colours).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import discord

from modules import catch_db

THEME_PATH = Path(__file__).resolve().parent.parent / "data" / "catch" / "theme.json"
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_STYLE_BY_NAME = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}
EDITABLE_GROUPS = ("states", "rarities", "elements")
_GROUP_PREFIX = {"state": "states", "rarity": "rarities", "element": "elements"}

_defaults: dict | None = None
_overrides: dict[str, str] = {}


def _load_defaults() -> dict:
    global _defaults
    if _defaults is None:
        with open(THEME_PATH, encoding="utf-8") as fh:
            _defaults = json.load(fh)
    return _defaults


def parse_hex(value: str) -> int:
    """'#57F287' or '57f287' -> int. Raises ValueError on anything else."""
    match = _HEX_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"not a 6-digit hex colour: {value!r}")
    return int(match.group(1), 16)


def button_style(role: str) -> discord.ButtonStyle:
    roles = _load_defaults()["button_roles"]
    if role not in roles:
        raise KeyError(f"unknown button role: {role}")
    return _STYLE_BY_NAME[roles[role]]


def button_roles() -> tuple[str, ...]:
    return tuple(_load_defaults()["button_roles"])


def _color(prefix: str, name: str) -> discord.Colour:
    key = f"{prefix}:{name}"
    if key in _overrides:
        return discord.Colour(parse_hex(_overrides[key]))
    group = _load_defaults()[_GROUP_PREFIX[prefix]]
    if name not in group:
        raise KeyError(f"unknown {prefix}: {name}")
    return discord.Colour(parse_hex(group[name]))


def state_color(state: str) -> discord.Colour:
    return _color("state", state)


def rarity_color(rarity: str, *, shiny: bool = False, special: bool = False) -> discord.Colour:
    if special:
        return _color("rarity", "special")
    if shiny:
        return _color("rarity", "shiny")
    return _color("rarity", rarity)


def element_color(element: str) -> discord.Colour:
    return _color("element", element)


def validate_override(key: str, value: str) -> str:
    """Check an owner edit before saving. Returns the normalised '#RRGGBB'."""
    prefix, _, name = key.partition(":")
    group = _GROUP_PREFIX.get(prefix)
    if group is None or name not in _load_defaults()[group]:
        raise KeyError(f"unknown theme key: {key}")
    return f"#{parse_hex(value):06X}"


def apply_overrides(rows: dict[str, str]) -> None:
    """Replace the in-memory overrides; invalid rows are ignored, never fatal."""
    global _overrides
    clean: dict[str, str] = {}
    for key, value in rows.items():
        try:
            clean[key] = validate_override(key, value)
        except (KeyError, ValueError):
            continue
    _overrides = clean


async def refresh_overrides(conn=None) -> None:
    async with catch_db.connection(conn) as c:
        rows = await c.fetch("SELECT key, value FROM catch_theme")
    apply_overrides({r["key"]: r["value"] for r in rows})


async def set_override(key: str, value: str, updated_by: int, conn=None) -> str:
    normalised = validate_override(key, value)
    async with catch_db.transaction(conn) as c:
        await c.execute(
            """
            INSERT INTO catch_theme (key, value, updated_by, updated_at) VALUES ($1, $2, $3, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                updated_by = EXCLUDED.updated_by, updated_at = now()
            """,
            key, normalised, updated_by,
        )
    _overrides[key] = normalised
    return normalised


async def reset_override(key: str, conn=None) -> None:
    async with catch_db.transaction(conn) as c:
        await c.execute("DELETE FROM catch_theme WHERE key = $1", key)
    _overrides.pop(key, None)
