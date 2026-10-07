"""Drawn banner for the wild zone screen.

Same approach as the other cards: Pillow, colours from ``catch_theme``, always optional.
The banner shows one orb per live spawn (up to ``SLOTS``, soonest to leave first, the same order
as the embed list), filled with the rarity colour and a sparkle when shiny. Empty slots stay
dim, so a quiet zone reads at a glance. The count on the right uses the real total. No player
or creature text is drawn; the embed keeps the full list and is never removed.
"""

from __future__ import annotations

import asyncio
import io
import logging
from functools import lru_cache

import discord
from PIL import Image, ImageDraw

from modules import catch_theme
from modules.catch_card import (
    _INK, _MUTED, _WHITE, SCALE, W, _font, _glow, _mix, _rgb, _s, _sparkle,
)
from modules.catch_coin_card import _png

logger = logging.getLogger(__name__)

WILD_H = 200
WILD_FILE = "wild.png"
SLOTS = 10
ORB_Y = 128
ORB_R = 22


def _orb_x(i: int, count: int = SLOTS) -> float:
    pad = 24 + ORB_R
    return pad + i * ((W - 2 * pad) / (count - 1))


def _draw_wild(slots: tuple[tuple[str, bool], ...], total: int) -> bytes:
    base = _rgb(catch_theme.element_color("verdant"))
    img = Image.new("RGB", (W * SCALE, WILD_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(base, _INK, 0.80), _mix(base, _INK, 0.92)
    for y in range(WILD_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (WILD_H * SCALE)))
    img = _glow(img, 120, 20, 160, base, 50)
    draw = ImageDraw.Draw(img)
    draw.text((_s(24), _s(40)), "WILD ZONE", font=_font(34), fill=_WHITE, anchor="lm")
    label = f"{total} IN THE WILD" if total > 0 else "QUIET"
    draw.text((_s(W - 24), _s(40)), label, font=_font(16), fill=_WHITE if total > 0 else _MUTED, anchor="rm")
    for i in range(SLOTS):
        cx = _orb_x(i)
        if i >= len(slots):
            draw.ellipse((_s(cx - ORB_R), _s(ORB_Y - ORB_R), _s(cx + ORB_R), _s(ORB_Y + ORB_R)),
                         fill=_mix(base, _INK, 0.86), outline=_mix(base, _INK, 0.6), width=_s(2))
            continue
        rarity, shiny = slots[i]
        colour = _rgb(catch_theme.rarity_color(rarity, shiny=shiny))
        img = _glow(img, cx, ORB_Y, ORB_R * 1.6, colour, 90)
        draw = ImageDraw.Draw(img)
        draw.ellipse((_s(cx - ORB_R), _s(ORB_Y - ORB_R), _s(cx + ORB_R), _s(ORB_Y + ORB_R)),
                     fill=_mix(colour, _INK, 0.25), outline=_mix(colour, _WHITE, 0.45), width=_s(3))
        draw.ellipse((_s(cx - ORB_R * 0.45), _s(ORB_Y - ORB_R * 0.55), _s(cx + ORB_R * 0.05), _s(ORB_Y - ORB_R * 0.1)),
                     fill=_mix(colour, _WHITE, 0.55))
        if shiny:
            _sparkle(draw, cx + ORB_R * 0.8, ORB_Y - ORB_R * 0.8, 9, _WHITE)
    if len(slots) >= SLOTS and total > SLOTS:
        draw.text((_s(W - 24), _s(ORB_Y + ORB_R + 22)), f"+{total - SLOTS} MORE", font=_font(14),
                  fill=_MUTED, anchor="rm")
    return _png(img, WILD_H)


@lru_cache(maxsize=64)
def _cached_wild(slots: tuple[tuple[str, bool], ...], total: int) -> bytes:
    return _draw_wild(slots, total)


def clear_wild_cache() -> None:
    _cached_wild.cache_clear()


async def wild_art(embed: discord.Embed, spawns, total: int) -> discord.File | None:
    """Attach the wild zone banner to ``embed``. The embed text is kept: it is the list itself."""
    slots = tuple((str(s.rarity), bool(s.shiny)) for s in list(spawns)[:SLOTS])
    try:
        data = await asyncio.to_thread(_cached_wild, slots, max(0, int(total)))
    except Exception:
        logger.warning("Catch wild zone card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{WILD_FILE}")
    return discord.File(io.BytesIO(data), filename=WILD_FILE)


__all__ = ["SLOTS", "WILD_FILE", "WILD_H", "clear_wild_cache", "wild_art"]
