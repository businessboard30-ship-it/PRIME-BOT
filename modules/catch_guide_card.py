"""Drawn banner for the guide screen (How to play).

Same approach as the other cards: Pillow, colours from ``catch_theme``, always optional.
The card is static (no inputs), so it is drawn once and cached. It only shows the five section
headings as tiles; the full guide text stays in the embed fields, which are never removed.
No player text is drawn.
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
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _rgb, _s, _sigil,
)
from modules.catch_coin_card import _png

logger = logging.getLogger(__name__)

GUIDE_H = 260
GUIDE_FILE = "guide.png"
# Section key -> (tile label, element whose colour and sigil represent it). Keys follow GUIDE_SECTIONS.
TILES = (
    ("catching", "CATCH", "ember"),
    ("items", "CAPSULES", "tide"),
    ("rarity", "RARITY", "volt"),
    ("coins", "COINS", "lumen"),
    ("collection", "COLLECTION", "verdant"),
)
TILE_Y = (92, 236)
RARITY_ORDER = ("common", "uncommon", "rare", "epic", "mythic")


def _element_rgb(element: str):
    return _rgb(catch_theme.element_color(element))


def _draw_guide() -> bytes:
    base = _element_rgb("frost")
    img = Image.new("RGB", (W * SCALE, GUIDE_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(base, _INK, 0.80), _mix(base, _INK, 0.92)
    for y in range(GUIDE_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (GUIDE_H * SCALE)))
    img = _glow(img, 120, 20, 160, base, 50)
    draw = ImageDraw.Draw(img)
    draw.text((_s(24), _s(40)), "HOW TO PLAY", font=_font(34), fill=_WHITE, anchor="lm")
    draw.text((_s(W - 24), _s(40)), "5 QUICK STEPS", font=_font(16), fill=_MUTED, anchor="rm")
    gap = 12
    tw = (W - 48 - gap * (len(TILES) - 1)) / len(TILES)
    y0, y1 = TILE_Y
    for i, (key, label, element) in enumerate(TILES):
        tx = 24 + i * (tw + gap)
        ec = _element_rgb(element)
        draw.rounded_rectangle((_s(tx), _s(y0), _s(tx + tw), _s(y1)), radius=_s(14),
                               fill=_mix(ec, _INK, 0.7), outline=_mix(ec, _WHITE, 0.3), width=_s(2))
        cx = tx + tw / 2
        draw.ellipse((_s(tx + 10), _s(y0 + 10), _s(tx + 34), _s(y0 + 34)), fill=_mix(ec, _INK, 0.35))
        draw.text((_s(tx + 22), _s(y0 + 22)), str(i + 1), font=_font(14), fill=_WHITE, anchor="mm")
        _sigil(draw, img, element, cx, y0 + 62, 26, _mix(ec, _WHITE, 0.6), _mix(ec, _INK, 0.5))
        draw = ImageDraw.Draw(img)
        if key == "rarity":
            for j, rarity in enumerate(RARITY_ORDER):
                dx = cx + (j - 2) * 16
                draw.ellipse((_s(dx - 5), _s(y0 + 100), _s(dx + 5), _s(y0 + 110)),
                             fill=_rgb(catch_theme.rarity_color(rarity)))
        draw.text((_s(cx), _s(y1 - 18)), label, font=_fit_font(draw, label, int(tw - 12), 16, 10),
                  fill=_WHITE, anchor="mm")
    return _png(img, GUIDE_H)


@lru_cache(maxsize=1)
def _cached_guide() -> bytes:
    return _draw_guide()


def clear_guide_cache() -> None:
    _cached_guide.cache_clear()


async def guide_art(embed: discord.Embed) -> discord.File | None:
    """Attach the guide banner to ``embed``. The embed text is kept: it is the guide itself."""
    try:
        data = await asyncio.to_thread(_cached_guide)
    except Exception:
        logger.warning("Catch guide card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{GUIDE_FILE}")
    return discord.File(io.BytesIO(data), filename=GUIDE_FILE)


__all__ = ["GUIDE_FILE", "GUIDE_H", "clear_guide_cache", "guide_art"]
