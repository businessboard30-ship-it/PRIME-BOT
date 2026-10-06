"""Drawn "it got away" card for an expired wild spawn.

Replaces the wild-encounter card on the public spawn message when nobody caught the creature
in time. Same style as ``catch_card`` (Pillow, colours from ``catch_theme``, cached, rendered in
a worker thread) but washed out: a grey backdrop and a dim medallion with no sparkles.

The expired spawn row only carries the species, so the card shows the species name, rarity and
element from the species table (no level). Callers must treat a failure as "no card".
"""

from __future__ import annotations

import asyncio
import io
from functools import lru_cache

import discord
from PIL import Image

from modules import catch_theme
from modules.catch_card import (
    _MUTED, _WHITE, W, H, _backdrop, _fit_font, _font, _medallion, _mix, _pill, _rgb, _s,
)

FLED_FILE = "fled.png"
_GREY = (92, 98, 112)


def _wash(colour, amount: float = 0.62) -> tuple[int, int, int]:
    """Pull a colour toward grey so the whole card reads as faded."""
    return _mix(_rgb(colour), _GREY, amount)


def _draw_fled(*, species_id: int, name: str, rarity: str, element: str, element2: str | None) -> bytes:
    c1 = _wash(catch_theme.element_color(element))
    c2 = _wash(catch_theme.element_color(element2)) if element2 else c1
    rar = _wash(catch_theme.rarity_color(rarity), 0.5)
    img, draw, rng = _backdrop(c1, c2, species_id * 13 + 5)
    draw = _medallion(img, draw, rng, element, element2, c1, c2, rar, 175, 200, 112, sparkles=False)

    px = 335
    draw.text((_s(px), _s(70)), "IT GOT AWAY...", font=_font(24), fill=_mix(_MUTED, _WHITE, 0.25), anchor="lm")
    draw.text((_s(px), _s(138)), name, font=_fit_font(draw, name, W - px - 24, 54, 22),
              fill=_mix(_WHITE, _GREY, 0.2), anchor="lm")
    x = _pill(draw, px, 186, rarity.upper(), rar)
    x = _pill(draw, x, 186, element.upper(), c1)
    if element2:
        _pill(draw, x, 186, element2.upper(), c2)
    draw.text((_s(px), _s(262)), "Nobody caught it in time.", font=_font(16), fill=_MUTED, anchor="lm")

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=64)
def _cached(key: tuple) -> bytes:
    species_id, name, rarity, element, element2 = key
    return _draw_fled(species_id=species_id, name=name, rarity=rarity, element=element, element2=element2)


async def fled_card_png(species: dict) -> bytes:
    """PNG for an expired spawn. ``species`` is one entry of ``catch_species.all_species()``.

    Raises on bad input; callers treat that as "no card" and keep the plain embed.
    """
    key = (int(species["id"]), str(species["name"]), str(species["rarity"]), str(species["element"]),
           species.get("element2") or None)
    return await asyncio.to_thread(_cached, key)


def fled_file(data: bytes) -> discord.File:
    """A fresh ``discord.File`` per send (never reuse one)."""
    return discord.File(io.BytesIO(data), filename=FLED_FILE)


def clear_fled_cache() -> None:
    _cached.cache_clear()


__all__ = ["FLED_FILE", "clear_fled_cache", "fled_card_png", "fled_file"]
