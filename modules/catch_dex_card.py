"""Drawn Dex page (a grid of the page's 12 species plus per-rarity completion) and species card.

Spoiler rule: what is drawn follows what the player has discovered, exactly like the text
Dex. An undiscovered species is a "?" tile; its element and rarity never reach the drawing
code or the cache key. A seen-but-not-caught species is a dim sigil with no rarity colour;
only a caught species gets its rarity ring, and only a caught species shows base stats.

Everything is optional: ``dex_art`` / ``species_art`` return ``None`` (embed untouched) when
drawing fails, and the caller sends the plain embed.
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
    _INK, _MUTED, _WHITE, SCALE, STAT_BAR_MAX, STAT_ORDER, W, _backdrop, _fit_font, _font, _glow, _medallion,
    _mix, _pill, _rgb, _s, _sigil, _sparkle,
    edit_kwargs, send_kwargs,
)
from modules.catch_collection import DEX_PAGE_SIZE, dex_page, dex_summary, page_count
from modules.catch_dex import SpeciesInfo, rarity_completion

logger = logging.getLogger(__name__)

DEX_H = 410
SPECIES_H = 400
DEX_FILE = "dex.png"
SPECIES_FILE = "species.png"
COLS = 6
TILE_W, TILE_H, TILE_GAP = 100, 104, 16
GRID_X, GRID_Y = 20, 86


def tiles_for(chunk) -> tuple[tuple, ...]:
    """(species_id, state, rarity, element, shiny) per entry. Undiscovered entries are blanked."""
    out = []
    for e in chunk:
        if e.caught:
            out.append((e.species_id, "caught", e.rarity, e.element, bool(e.shiny_caught)))
        elif e.seen:
            out.append((e.species_id, "seen", "", e.element, False))
        else:
            out.append((e.species_id, "unknown", "", "", False))
    return tuple(out)


def _png(img: Image.Image, h: int) -> bytes:
    out = io.BytesIO()
    img.resize((W, h), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _dex_canvas():
    base = _rgb(catch_theme.state_color("info"))
    img = Image.new("RGB", (W * SCALE, DEX_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(base, _INK, 0.80), _mix(base, _INK, 0.93)
    for y in range(DEX_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (DEX_H * SCALE)))
    img = _glow(img, 620, 40, 170, base, 70)
    return img, ImageDraw.Draw(img), base


def _draw_dex(tiles: tuple, summary: tuple[int, int, int], progress: tuple, page: int, pages: int) -> bytes:
    caught, seen, total = summary
    img, draw, base = _dex_canvas()
    bright = _mix(base, _WHITE, 0.35)

    draw.text((_s(24), _s(34)), "DEX", font=_font(30), fill=_WHITE, anchor="lm")
    pct = 0 if total <= 0 else int(100 * caught / total)
    draw.text((_s(W - 24), _s(34)), f"{caught:,} / {total:,}  \u00b7  {pct}%", font=_font(22), fill=_WHITE, anchor="rm")
    draw.rounded_rectangle((_s(24), _s(56), _s(W - 24), _s(66)), radius=_s(5), fill=_mix(_INK, _WHITE, 0.10))
    if total > 0 and seen > 0:
        draw.rounded_rectangle((_s(24), _s(56), _s(24 + (W - 48) * min(1.0, seen / total)), _s(66)),
                               radius=_s(5), fill=_mix(base, _INK, 0.45))
    if total > 0 and caught > 0:
        draw.rounded_rectangle((_s(24), _s(56), _s(max(36, 24 + (W - 48) * min(1.0, caught / total))), _s(66)),
                               radius=_s(5), fill=bright)

    for i, (species_id, state, rarity, element, shiny) in enumerate(tiles):
        x = GRID_X + (i % COLS) * (TILE_W + TILE_GAP)
        y = GRID_Y + (i // COLS) * (TILE_H + 12)
        ring = _MUTED
        if state == "caught":
            ring = _rgb(catch_theme.rarity_color(rarity, shiny=False))
        outline = ring if state == "caught" else _mix(_INK, _WHITE, 0.22 if state == "seen" else 0.12)
        draw.rounded_rectangle((_s(x), _s(y), _s(x + TILE_W), _s(y + TILE_H)), radius=_s(14),
                               fill=_mix(_INK, _WHITE, 0.07), outline=outline, width=_s(2))
        draw.text((_s(x + 10), _s(y + 14)), f"#{species_id:03d}", font=_font(11), fill=_MUTED, anchor="lm")
        cx, cy = x + TILE_W / 2, y + 58
        if state == "unknown":
            draw.text((_s(cx), _s(cy)), "?", font=_font(38), fill=_mix(_INK, _WHITE, 0.28), anchor="mm")
            continue
        c1 = _rgb(catch_theme.element_color(element))
        if state == "caught":
            fill, dark = _mix(c1, _WHITE, 0.55), _mix(c1, _INK, 0.5)
        else:
            fill, dark = _mix(c1, _INK, 0.45), _mix(c1, _INK, 0.75)
        _sigil(draw, img, element, cx, cy, 26, fill, dark)
        draw = ImageDraw.Draw(img)
        if shiny:
            _sparkle(draw, x + TILE_W - 14, y + 16, 7, _rgb(catch_theme.rarity_color("shiny", shiny=True)))

    count = max(1, len(progress))
    gap = 10
    bar_w = (W - 40 - gap * (count - 1)) / count
    for i, (rarity, got, tot) in enumerate(progress):
        x = 20 + i * (bar_w + gap)
        colour = _rgb(catch_theme.rarity_color(rarity, shiny=False))
        draw.text((_s(x), _s(330)), rarity.upper(), font=_font(12), fill=_MUTED, anchor="lm")
        draw.text((_s(x + bar_w), _s(330)), f"{got}/{tot}", font=_font(13), fill=_WHITE, anchor="rm")
        draw.rounded_rectangle((_s(x), _s(342), _s(x + bar_w), _s(352)), radius=_s(5), fill=_mix(_INK, _WHITE, 0.10))
        if tot > 0 and got > 0:
            draw.rounded_rectangle((_s(x), _s(342), _s(max(x + 10, x + bar_w * got / tot)), _s(352)), radius=_s(5), fill=colour)
    draw.text((_s(W - 24), _s(390)), f"PAGE {page + 1} / {max(1, pages)}", font=_font(12), fill=_MUTED, anchor="rm")
    return _png(img, DEX_H)


def _draw_species(*, species_id: int, name: str, rarity: str, element: str, element2: str | None, caught: bool,
                  stats: tuple[int, ...] | None, count: int, shiny: int) -> bytes:
    c1 = _rgb(catch_theme.element_color(element))
    c2 = _rgb(catch_theme.element_color(element2)) if element2 else c1
    rar = _rgb(catch_theme.rarity_color(rarity, shiny=False)) if caught else _MUTED
    if not caught:
        c1, c2 = _mix(c1, _INK, 0.45), _mix(c2, _INK, 0.45)
    img, draw, rng = _backdrop(c1, c2, species_id * 13)
    draw = _medallion(img, draw, rng, element, element2, c1, c2, rar, 175, 200, 112, sparkles=bool(shiny))
    px = 335
    draw.text((_s(px), _s(54)), name, font=_fit_font(draw, name, W - px - 24, 44), fill=_WHITE, anchor="lm")
    x = _pill(draw, px, 92, f"#{species_id:03d}", _mix(_WHITE, c1, 0.15), size=14)
    x = _pill(draw, x, 92, rarity.upper(), rar, size=14)
    x = _pill(draw, x, 92, element.upper(), _rgb(catch_theme.element_color(element)), size=14)
    if element2:
        _pill(draw, x, 92, element2.upper(), _rgb(catch_theme.element_color(element2)), size=14)
    if caught and stats:
        for i, key in enumerate(STAT_ORDER):
            v = stats[i] if i < len(stats) else 0
            y = 142 + i * 31
            draw.text((_s(px), _s(y + 9)), key.upper(), font=_font(13), fill=_MUTED, anchor="lm")
            bx0, bx1 = px + 78, W - 78
            draw.rounded_rectangle((_s(bx0), _s(y + 2), _s(bx1), _s(y + 16)), radius=_s(7), fill=_mix(_INK, _WHITE, 0.10))
            frac = max(0.0, min(1.0, v / STAT_BAR_MAX))
            if frac > 0:
                draw.rounded_rectangle((_s(bx0), _s(y + 2), _s(bx0 + max(14, (bx1 - bx0) * frac)), _s(y + 16)),
                                       radius=_s(7), fill=_mix(c1, _WHITE, 0.25))
            draw.text((_s(W - 24), _s(y + 9)), str(v), font=_font(15), fill=_WHITE, anchor="rm")
        px2 = _pill(draw, px, 340, f"CAUGHT \u00d7{count}", _mix(_INK, _WHITE, 0.16), size=14)
        if shiny:
            _pill(draw, px2, 340, f"SHINY \u00d7{shiny}", _rgb(catch_theme.rarity_color("shiny", shiny=True)), size=14)
    else:
        _pill(draw, px, 160, "DISCOVERED", _mix(_INK, _WHITE, 0.16), size=14)
        draw.text((_s(px), _s(214)), "Catch one to reveal its stats.", font=_font(16), fill=_MUTED, anchor="lm")
    return _png(img, SPECIES_H)


@lru_cache(maxsize=64)
def _cached_dex(tiles: tuple, summary: tuple, progress: tuple, page: int, pages: int) -> bytes:
    return _draw_dex(tiles, summary, progress, page, pages)


@lru_cache(maxsize=128)
def _cached_species(key: tuple) -> bytes:
    (species_id, name, rarity, element, element2, caught, stats, count, shiny) = key
    return _draw_species(species_id=species_id, name=name, rarity=rarity, element=element, element2=element2,
                         caught=caught, stats=stats, count=count, shiny=shiny)


def species_key(info: SpeciesInfo) -> tuple:
    stats = tuple(int(info.stats.get(k, 0)) for k in STAT_ORDER) if (info.caught and info.stats) else None
    return (info.species_id, info.name, info.rarity, info.element, info.element2 or None, bool(info.caught), stats,
            int(info.caught_count) if info.caught else 0, int(info.shiny_caught) if info.caught else 0)


def clear_dex_card_cache() -> None:
    _cached_dex.cache_clear()
    _cached_species.cache_clear()


def _drop_field(embed: discord.Embed, name: str) -> None:
    for i, field in enumerate(embed.fields):
        if field.name == name:
            embed.remove_field(i)
            return


async def dex_art(embed: discord.Embed, entries, page: int, *, drop_field: str | None = None) -> discord.File | None:
    """Attach the Dex page card to ``embed``; the plain completion field is dropped when it renders."""
    try:
        chunk, page = dex_page(list(entries), page)
        progress = tuple((p.rarity, p.caught, p.total) for p in rarity_completion(list(entries)))
        pages = page_count(len(entries), DEX_PAGE_SIZE)
        data = await asyncio.to_thread(_cached_dex, tiles_for(chunk), dex_summary(list(entries)), progress, page, pages)
    except Exception:
        logger.warning("Catch dex card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{DEX_FILE}")
    if drop_field:
        _drop_field(embed, drop_field)
    return discord.File(io.BytesIO(data), filename=DEX_FILE)


async def species_art(embed: discord.Embed, info: SpeciesInfo, *, drop_field: str | None = None) -> discord.File | None:
    """Attach the species card; the plain stats field is dropped when it renders."""
    try:
        data = await asyncio.to_thread(_cached_species, species_key(info))
    except Exception:
        logger.warning("Catch species card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{SPECIES_FILE}")
    if drop_field:
        _drop_field(embed, drop_field)
    return discord.File(io.BytesIO(data), filename=SPECIES_FILE)


__all__ = ["clear_dex_card_cache", "dex_art", "edit_kwargs", "send_kwargs", "species_art", "tiles_for"]
