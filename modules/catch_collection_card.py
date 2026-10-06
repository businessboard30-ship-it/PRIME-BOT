"""Drawn collection box: the page's creatures as a 5x2 grid of medallions.

Each tile shows the creature's rarity ring, element sigil, species name, level, owned id and
its flags (favourite star, lock, shiny/special sparkle). Species names come from the species
table; nicknames are never drawn (the text list under the card has them), so no player text
can reach the renderer.

Optional like every card: ``collection_art`` returns ``None`` (embed untouched) when drawing
fails, and the caller sends the plain embed.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from functools import lru_cache

import discord
from PIL import Image, ImageDraw

from modules import catch_theme
from modules.catch_card import (
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _rgb, _s, _sigil, _sparkle,
    edit_kwargs, send_kwargs,
)
from modules.catch_collection import COLLECTION_PAGE_SIZE, page_count
from modules.catch_species import all_species

logger = logging.getLogger(__name__)

FILE = "box.png"
COLS = 5
TILE_W, TILE_H, GAP_X, GAP_Y = 128, 150, 12, 12
GRID_X, GRID_Y = 16, 66
GOLD = (255, 214, 80)


def tiles_for(rows) -> tuple[tuple, ...]:
    """(owned_id, species name, rarity, element, level, shiny, special, favorite, locked) per row."""
    species = all_species()
    out = []
    for r in rows:
        element = str((species.get(int(r.species_id)) or {}).get("element") or "stone")
        out.append((int(r.id), str(r.name), str(r.rarity), element, int(r.level), bool(r.shiny), bool(r.special),
                    bool(r.favorite), bool(r.locked)))
    return tuple(out)


def _star(draw, cx: float, cy: float, r: float, fill) -> None:
    pts = []
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((_s(cx + math.cos(a) * rad), _s(cy + math.sin(a) * rad)))
    draw.polygon(pts, fill=fill)


def _lock(draw, cx: float, cy: float, r: float, fill) -> None:
    draw.arc((_s(cx - r * 0.55), _s(cy - r * 1.0), _s(cx + r * 0.55), _s(cy + r * 0.3)), start=180, end=360,
             fill=fill, width=_s(2.5))
    draw.rounded_rectangle((_s(cx - r * 0.8), _s(cy - r * 0.2), _s(cx + r * 0.8), _s(cy + r * 0.9)),
                           radius=_s(2), fill=fill)


def _height(count: int) -> int:
    rows = max(1, -(-count // COLS))
    return GRID_Y + rows * TILE_H + (rows - 1) * GAP_Y + 44


def _canvas(h: int):
    base = _rgb(catch_theme.state_color("info"))
    img = Image.new("RGB", (W * SCALE, h * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(base, _INK, 0.80), _mix(base, _INK, 0.93)
    for y in range(h * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (h * SCALE)))
    img = _glow(img, 620, 40, 170, base, 70)
    return img, ImageDraw.Draw(img)


def _draw_box(tiles: tuple, page: int, pages: int, total: int) -> bytes:
    h = _height(len(tiles))
    img, draw = _canvas(h)
    draw.text((_s(24), _s(32)), "COLLECTION", font=_font(28), fill=_WHITE, anchor="lm")
    draw.text((_s(W - 24), _s(32)), f"{total:,} CREATURES", font=_font(18), fill=_MUTED, anchor="rm")

    for i, (owned_id, name, rarity, element, level, shiny, special, favorite, locked) in enumerate(tiles):
        x = GRID_X + (i % COLS) * (TILE_W + GAP_X)
        y = GRID_Y + (i // COLS) * (TILE_H + GAP_Y)
        rar = _rgb(catch_theme.rarity_color(rarity, shiny=shiny, special=special))
        c1 = _rgb(catch_theme.element_color(element))
        draw.rounded_rectangle((_s(x), _s(y), _s(x + TILE_W), _s(y + TILE_H)), radius=_s(16),
                               fill=_mix(_INK, _WHITE, 0.07), outline=_mix(rar, _INK, 0.55), width=_s(2))
        draw.text((_s(x + 12), _s(y + 16)), f"#{owned_id}", font=_font(12), fill=_MUTED, anchor="lm")
        fx = x + TILE_W - 14
        if favorite:
            _star(draw, fx, y + 16, 8, GOLD)
            fx -= 20
        if locked:
            _lock(draw, fx, y + 15, 7, _MUTED)
        cx, cy, mr = x + TILE_W / 2, y + 66, 36
        draw.ellipse((_s(cx - mr), _s(cy - mr), _s(cx + mr), _s(cy + mr)), fill=_mix(c1, _INK, 0.62))
        draw.ellipse((_s(cx - mr), _s(cy - mr), _s(cx + mr), _s(cy + mr)), outline=rar, width=_s(4))
        _sigil(draw, img, element, cx, cy, 22, _mix(c1, _WHITE, 0.55), _mix(c1, _INK, 0.5))
        draw = ImageDraw.Draw(img)
        if shiny or special:
            _sparkle(draw, cx + mr - 4, cy - mr + 6, 8, rar)
            _sparkle(draw, cx - mr + 2, cy + mr - 8, 5, rar)
        draw.text((_s(cx), _s(y + 118)), name, font=_fit_font(draw, name, TILE_W - 16, 15, 10), fill=_WHITE, anchor="mm")
        draw.text((_s(cx), _s(y + 136)), f"LV {level}", font=_font(12), fill=_MUTED, anchor="mm")

    draw.text((_s(W - 24), _s(h - 20)), f"PAGE {page + 1} / {max(1, pages)}", font=_font(12), fill=_MUTED, anchor="rm")
    out = io.BytesIO()
    img.resize((W, h), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=64)
def _cached_box(tiles: tuple, page: int, pages: int, total: int) -> bytes:
    return _draw_box(tiles, page, pages, total)


def clear_collection_card_cache() -> None:
    _cached_box.cache_clear()


async def collection_art(embed: discord.Embed, rows, page: int, total: int) -> discord.File | None:
    """Attach the box card to ``embed`` (nothing to draw for an empty page); ``None`` if it cannot render."""
    rows = list(rows)
    if not rows:
        return None
    try:
        pages = page_count(int(total), COLLECTION_PAGE_SIZE)
        data = await asyncio.to_thread(_cached_box, tiles_for(rows), int(page), pages, int(total))
    except Exception:
        logger.warning("Catch collection card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{FILE}")
    return discord.File(io.BytesIO(data), filename=FILE)


__all__ = ["clear_collection_card_cache", "collection_art", "edit_kwargs", "send_kwargs", "tiles_for"]
