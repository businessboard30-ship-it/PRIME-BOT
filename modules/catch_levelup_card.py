"""Drawn level-up card for the catch game.

Shown as a second, ephemeral message when the buddy levels up after a catch. Same idea as
``catch_card``: built from game data with Pillow, colours from ``catch_theme``, drawing
helpers imported from ``catch_card``, rendering cached and run in a worker thread.

Player-chosen text (nicknames) is never drawn: the bundled font cannot show every
character. Callers must treat a failure as "no card" and fall back to the plain text.
"""

from __future__ import annotations

import asyncio
import io
import random
from functools import lru_cache

import discord
from PIL import Image, ImageDraw

from modules import catch_theme
from modules.catch_card import (
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _medallion, _mix, _pill, _rgb, _s, _text_w,
)
from modules.catch_game import LEVEL_MAX
from modules.catch_xp import xp_to_next

LEVELUP_H = 300
LEVELUP_FILE = "levelup.png"


def _canvas(c1, c2, seed: int):
    h = LEVELUP_H
    img = Image.new("RGB", (W * SCALE, h * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(c1, _INK, 0.80), _mix(c2, _INK, 0.90)
    for y in range(h * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (h * SCALE)))
    img = _glow(img, 130, 150, 170, c1, 150)
    img = _glow(img, 640, 290, 190, c2, 90)
    draw = ImageDraw.Draw(img)
    rng = random.Random(seed)
    for _ in range(22):
        x, y, rr = rng.uniform(0, W), rng.uniform(0, h), rng.uniform(1.2, 3)
        draw.ellipse((_s(x - rr), _s(y - rr), _s(x + rr), _s(y + rr)), fill=_mix(top, _WHITE, 0.14))
    return img, draw, rng


def _arrow(draw, x: float, y: float, colour) -> None:
    pts = [(x, y - 7), (x + 16, y - 7), (x + 16, y - 14), (x + 32, y), (x + 16, y + 14), (x + 16, y + 7), (x, y + 7)]
    draw.polygon([(_s(px), _s(py)) for px, py in pts], fill=colour)


def _draw_levelup(*, species_id: int, name: str, rarity: str, element: str, element2: str | None, shiny: bool,
                  special: bool, level_before: int, level_after: int, xp: int, evolve_ready: bool) -> bytes:
    c1 = _rgb(catch_theme.element_color(element))
    c2 = _rgb(catch_theme.element_color(element2)) if element2 else c1
    rar = _rgb(catch_theme.rarity_color(rarity, shiny=shiny, special=special))
    img, draw, rng = _canvas(c1, c2, species_id * 31 + level_after)
    draw = _medallion(img, draw, rng, element, element2, c1, c2, rar, 130, 150, 84, sparkles=True)

    px = 270
    draw.text((_s(px), _s(40)), "LEVEL UP!", font=_font(24), fill=_mix(rar, _WHITE, 0.35), anchor="lm")
    draw.text((_s(px), _s(84)), name, font=_fit_font(draw, name, W - px - 24, 40, 22), fill=_WHITE, anchor="lm")

    before, after = f"LV {level_before}", f"LV {level_after}"
    fb = _font(30)
    draw.text((_s(px), _s(142)), before, font=fb, fill=_MUTED, anchor="lm")
    ax = px + _text_w(draw, before, fb) / SCALE + 14
    _arrow(draw, ax, 142, _mix(rar, _WHITE, 0.2))
    fa = _fit_font(draw, after, W - (ax + 46) - 24, 54, 28)
    draw.text((_s(ax + 46), _s(142)), after, font=fa, fill=_WHITE, anchor="lm")

    x = _pill(draw, px, 186, rarity.upper(), rar, size=14)
    x = _pill(draw, x, 186, element.upper(), c1, size=14)
    if element2:
        x = _pill(draw, x, 186, element2.upper(), c2, size=14)
    if evolve_ready:
        _pill(draw, x, 186, "READY TO EVOLVE", _rgb(catch_theme.state_color("success")), size=14)

    need = xp_to_next(level_after) if level_after < LEVEL_MAX else 0
    label = "MAX LEVEL" if need == 0 else f"XP  {max(0, xp):,} / {need:,}"
    yb = 236
    draw.text((_s(px), _s(yb)), label, font=_font(13), fill=_MUTED, anchor="lm")
    draw.rounded_rectangle((_s(px), _s(yb + 14), _s(W - 24), _s(yb + 26)), radius=_s(6), fill=_mix(_INK, _WHITE, 0.10))
    frac = 1.0 if need == 0 else max(0.0, min(1.0, xp / need))
    if frac > 0:
        draw.rounded_rectangle((_s(px), _s(yb + 14), _s(px + (W - 24 - px) * frac), _s(yb + 26)),
                               radius=_s(6), fill=_mix(rar, _WHITE, 0.15))

    out = io.BytesIO()
    img.resize((W, LEVELUP_H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=64)
def _cached(key: tuple) -> bytes:
    (species_id, name, rarity, element, element2, shiny, special, level_before, level_after, xp, evolve_ready) = key
    return _draw_levelup(
        species_id=species_id, name=name, rarity=rarity, element=element, element2=element2, shiny=shiny,
        special=special, level_before=level_before, level_after=level_after, xp=xp, evolve_ready=evolve_ready,
    )


def evolve_ready(species: dict, level: int) -> bool:
    """True when a level-based evolution is available at ``level`` (item evolutions are not shown)."""
    target, at = species.get("evolves_to"), species.get("evolve_level")
    return target is not None and at is not None and int(level) >= int(at)


async def levelup_card_png(species: dict, *, level_before: int, level_after: int, xp: int = 0,
                           shiny: bool = False, special: bool = False) -> bytes:
    """PNG for a level-up. ``species`` is one entry of ``catch_species.all_species()``.

    Raises on bad input; callers treat that as "no card" and send the plain text.
    """
    key = (int(species["id"]), str(species["name"]), str(species["rarity"]), str(species["element"]),
           species.get("element2") or None, bool(shiny), bool(special), int(level_before), int(level_after),
           int(xp), evolve_ready(species, level_after))
    return await asyncio.to_thread(_cached, key)


def levelup_file(data: bytes) -> discord.File:
    """A fresh ``discord.File`` per send (never reuse one)."""
    return discord.File(io.BytesIO(data), filename=LEVELUP_FILE)


def clear_levelup_cache() -> None:
    _cached.cache_clear()


__all__ = ["LEVELUP_FILE", "LEVELUP_H", "clear_levelup_cache", "evolve_ready", "levelup_card_png", "levelup_file"]
