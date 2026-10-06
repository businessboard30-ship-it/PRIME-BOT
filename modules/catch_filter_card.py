"""Drawn card for the collection filter screen.

Shows the rarity and element pickers as rows (the chosen one lit) and the Shiny, Favourite and
Search toggles as on/off chips. The search term itself is player text and is NEVER drawn (the
bundled font cannot show every character); the card only shows whether a search is on, and the
embed keeps the term as text. Same approach as ``catch_card``: Pillow, colours from
``catch_theme``, helpers imported from ``catch_card``, renders cached and run in a worker thread.
Callers treat an exception as "no card" and keep the plain embed.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
from functools import lru_cache

import discord
from PIL import Image, ImageDraw

from modules import catch_theme
from modules.catch_card import (
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _rgb, _s, _sigil,
)
from modules.catch_game import ELEMENTS, RARITIES

logger = logging.getLogger(__name__)

FILTER_H = 290
FILTER_FILE = "filter.png"


def _count(rarity, element, shiny, favorite, searching) -> int:
    return sum(1 for on in (rarity, element, shiny, favorite, searching) if on)


def _draw_filter(rarity: str | None, element: str | None, shiny: bool, favorite: bool, searching: bool) -> bytes:
    base = _rgb(catch_theme.element_color(element)) if element else _rgb(catch_theme.state_color("info"))
    img = Image.new("RGB", (W * SCALE, FILTER_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(base, _INK, 0.80), _mix(base, _INK, 0.93)
    for y in range(FILTER_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (FILTER_H * SCALE)))
    img = _glow(img, 620, 40, 170, base, 70)
    draw = ImageDraw.Draw(img)
    rng = random.Random(7)
    for _ in range(16):
        x, y, rr = rng.uniform(0, W), rng.uniform(0, FILTER_H), rng.uniform(1.2, 3)
        draw.ellipse((_s(x - rr), _s(y - rr), _s(x + rr), _s(y + rr)), fill=_mix(top, _WHITE, 0.14))

    n = _count(rarity, element, shiny, favorite, searching)
    draw.text((_s(28), _s(32)), "FILTER", font=_font(28), fill=_WHITE, anchor="lm")
    draw.text((_s(W - 28), _s(32)), f"{n} ACTIVE" if n else "NO FILTERS", font=_font(18),
              fill=_mix(base, _WHITE, 0.45) if n else _MUTED, anchor="rm")

    x0, x1 = 24, W - 24
    dim, dim_edge = _mix(_INK, _WHITE, 0.07), _mix(_INK, _WHITE, 0.16)

    # --- rarity row: ANY plus the five tiers
    draw.text((_s(28), _s(68)), "RARITY", font=_font(12), fill=_MUTED, anchor="lm")
    cells = [(None, "ANY", _rgb(catch_theme.state_color("info")))] + [
        (r.key, r.key.upper(), _rgb(catch_theme.rarity_color(r.key))) for r in RARITIES
    ]
    gap = 8
    cw = (x1 - x0 - gap * (len(cells) - 1)) / len(cells)
    ry0, ry1 = 80, 116
    for i, (key, label, col) in enumerate(cells):
        cx0 = x0 + i * (cw + gap)
        on = key == rarity
        draw.rounded_rectangle((_s(cx0), _s(ry0), _s(cx0 + cw), _s(ry1)), radius=_s(12),
                               fill=_mix(col, _INK, 0.45) if on else dim,
                               outline=_mix(col, _WHITE, 0.4) if on else dim_edge, width=_s(3 if on else 1))
        if key:
            dot = col if on else _mix(col, _INK, 0.45)
            draw.ellipse((_s(cx0 + 12), _s(ry0 + 13), _s(cx0 + 22), _s(ry0 + 23)), fill=dot)
        draw.text((_s(cx0 + cw / 2 + (6 if key else 0)), _s((ry0 + ry1) / 2)), label,
                  font=_fit_font(draw, label, int(cw - (34 if key else 16)), 14, 9),
                  fill=_WHITE if on else _MUTED, anchor="mm")

    # --- element row: ANY plus the nine elements
    draw.text((_s(28), _s(134)), "ELEMENT", font=_font(12), fill=_MUTED, anchor="lm")
    ecells = [None, *ELEMENTS]
    egap = 6
    ew = (x1 - x0 - egap * (len(ecells) - 1)) / len(ecells)
    ey0, ey1 = 146, 210
    for i, key in enumerate(ecells):
        cx0 = x0 + i * (ew + egap)
        on = key == element
        col = _rgb(catch_theme.element_color(key)) if key else _rgb(catch_theme.state_color("info"))
        draw.rounded_rectangle((_s(cx0), _s(ey0), _s(cx0 + ew), _s(ey1)), radius=_s(12),
                               fill=_mix(col, _INK, 0.45) if on else dim,
                               outline=_mix(col, _WHITE, 0.4) if on else dim_edge, width=_s(3 if on else 1))
        mid = cx0 + ew / 2
        if key:
            glyph = _mix(col, _WHITE, 0.6) if on else _mix(col, _INK, 0.35)
            _sigil(draw, img, key, mid, ey0 + 26, 15, glyph, _mix(col, _INK, 0.5))
            draw = ImageDraw.Draw(img)
            label = key.upper()
            draw.text((_s(mid), _s(ey1 - 12)), label, font=_fit_font(draw, label, int(ew - 6), 10, 7),
                      fill=_WHITE if on else _MUTED, anchor="mm")
        else:
            draw.text((_s(mid), _s((ey0 + ey1) / 2)), "ANY", font=_font(16), fill=_WHITE if on else _MUTED, anchor="mm")

    # --- toggles: Shiny, Favourite, Search (on or off only; the typed term is never drawn)
    gold = _rgb(catch_theme.state_color("warning"))
    info = _rgb(catch_theme.state_color("info"))
    toggles = (("SHINY", shiny, gold), ("FAVOURITE", favorite, gold), ("SEARCH", searching, info))
    tgap = 10
    tw = (x1 - x0 - tgap * (len(toggles) - 1)) / len(toggles)
    ty0, ty1 = 226, 268
    for i, (label, on, col) in enumerate(toggles):
        cx0 = x0 + i * (tw + tgap)
        draw.rounded_rectangle((_s(cx0), _s(ty0), _s(cx0 + tw), _s(ty1)), radius=_s(14),
                               fill=_mix(col, _INK, 0.55) if on else dim,
                               outline=_mix(col, _WHITE, 0.4) if on else dim_edge, width=_s(3 if on else 1))
        ccx, ccy = cx0 + 26, (ty0 + ty1) / 2
        draw.ellipse((_s(ccx - 9), _s(ccy - 9), _s(ccx + 9), _s(ccy + 9)),
                     fill=col if on else _mix(_INK, _WHITE, 0.1), outline=_mix(col, _WHITE, 0.4) if on else dim_edge,
                     width=_s(2))
        if on:  # tick
            draw.line((_s(ccx - 4.5), _s(ccy), _s(ccx - 1), _s(ccy + 4), _s(ccx + 5), _s(ccy - 4)),
                      fill=_INK, width=_s(2.4), joint="curve")
        draw.text((_s(cx0 + 46), _s(ccy)), label, font=_font(17), fill=_WHITE if on else _MUTED, anchor="lm")
        draw.text((_s(cx0 + tw - 16), _s(ccy)), "ON" if on else "OFF", font=_font(13),
                  fill=_mix(col, _WHITE, 0.5) if on else _mix(_MUTED, _INK, 0.35), anchor="rm")

    out = io.BytesIO()
    img.resize((W, FILTER_H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=64)
def _cached(key: tuple) -> bytes:
    return _draw_filter(*key)


def _key(flt) -> tuple:
    """Cache key from a cleaned ``CollectionFilter``: the search term is reduced to on/off."""
    flt = flt.clean()
    return (flt.rarity, flt.element, bool(flt.shiny), bool(flt.favorite), bool(flt.search))


async def filter_card_png(flt) -> bytes:
    """PNG for the filter screen. Raises on a bad filter; callers treat that as "no card"."""
    return await asyncio.to_thread(_cached, _key(flt))


def filter_file(data: bytes) -> discord.File:
    """A fresh ``discord.File`` per send (never reuse one)."""
    return discord.File(io.BytesIO(data), filename=FILTER_FILE)


def clear_filter_cache() -> None:
    _cached.cache_clear()


async def filter_art(embed: discord.Embed, flt) -> discord.File | None:
    """Attach the filter card to ``embed``; ``None`` with the embed untouched if it cannot render."""
    try:
        data = await filter_card_png(flt)
    except Exception:
        logger.warning("Catch filter card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{FILTER_FILE}")
    return filter_file(data)


__all__ = ["FILTER_FILE", "FILTER_H", "clear_filter_cache", "filter_art", "filter_card_png", "filter_file"]
