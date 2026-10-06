"""Drawn card for the player's status screen.

Six tiles: coins, total catches, collection size (with shiny and favourite counts), catch streak,
daily streak (ready or waiting) and Dex progress. Every number is the player's own and only numbers
are drawn: no names, nicknames or other player text. The live Daily countdown is NOT drawn (an image
cannot tick down and a cached one would go stale); the embed keeps its Daily field for that.

Same approach as ``catch_card``: Pillow, colours from ``catch_theme``, helpers imported from
``catch_card`` and ``catch_coin_card``, renders cached and run in a worker thread. ``status_art``
treats any failure as "no card" and leaves the plain embed untouched.
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
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _pill, _rgb, _s, _sigil,
)
from modules.catch_coin_card import _coin, _drop_field
from modules.catch_i18n import text

logger = logging.getLogger(__name__)

STATUS_H = 324
STATUS_FILE = "status.png"
# Locale keys of the plain fields the card replaces. The Daily field stays: it carries the live countdown.
DROPPED_FIELDS = ("status.coins", "status.catches", "status.collection", "status.streak", "status.dex")

_TILE_W, _TILE_H, _GAP, _X0, _Y0 = 216, 112, 12, 24, 64
# tile -> (label, element whose colour and sigil accent it; None = the gold coin)
_TILES = (
    ("COINS", None), ("CATCHES", "ember"), ("COLLECTION", "verdant"),
    ("CATCH STREAK", "volt"), ("DAILY STREAK", "lumen"), ("DEX", "frost"),
)


def _n(value) -> int:
    return max(0, int(value))


def _key(status) -> tuple:
    """Everything the card draws, as plain non-negative ints (the Daily state is reduced to ready or not)."""
    return (
        _n(status.coins), _n(status.total_catches), _n(status.owned), _n(status.shinies), _n(status.favourites),
        _n(status.catch_streak), _n(status.best_streak), _n(status.daily_streak), status.daily_ready_at is None,
        _n(status.dex_caught), _n(status.dex_seen), _n(status.dex_total),
    )


def _tile_xy(i: int) -> tuple[float, float]:
    return _X0 + (i % 3) * (_TILE_W + _GAP), _Y0 + (i // 3) * (_TILE_H + _GAP)


def _lines(coins, catches, owned, shinies, favourites, streak, best, daily, dex_caught, dex_seen, dex_total):
    """The big number and the small line of each tile, in tile order (the Daily tile has a chip instead of a line)."""
    big = (f"{coins:,}", f"{catches:,}", f"{owned:,}", f"{streak:,}", f"{daily:,}", f"{dex_caught:,} / {dex_total:,}")
    sub = ("", "ALL TIME", f"{shinies:,} SHINY  \u00b7  {favourites:,} FAV", f"BEST {best:,}", None, f"{dex_seen:,} SEEN")
    return big, sub


def _draw_status(coins, catches, owned, shinies, favourites, streak, best, daily, ready, dex_caught, dex_seen,
                 dex_total) -> bytes:
    base = _rgb(catch_theme.state_color("info"))
    gold = _rgb(catch_theme.state_color("warning"))
    img = Image.new("RGB", (W * SCALE, STATUS_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(base, _INK, 0.80), _mix(base, _INK, 0.93)
    for y in range(STATUS_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (STATUS_H * SCALE)))
    img = _glow(img, 600, 30, 190, base, 70)
    draw = ImageDraw.Draw(img)
    draw.text((_s(28), _s(34)), "YOUR STATUS", font=_font(28), fill=_WHITE, anchor="lm")

    big, sub = _lines(coins, catches, owned, shinies, favourites, streak, best, daily, dex_caught, dex_seen, dex_total)
    for i, (label, element) in enumerate(_TILES):
        x, y = _tile_xy(i)
        accent = gold if element is None else _rgb(catch_theme.element_color(element))
        draw.rounded_rectangle((_s(x), _s(y), _s(x + _TILE_W), _s(y + _TILE_H)), radius=_s(16),
                               fill=_mix(_INK, _WHITE, 0.08), outline=_mix(accent, _INK, 0.55), width=_s(2))
        draw.rounded_rectangle((_s(x + 10), _s(y + 16), _s(x + 14), _s(y + 36)), radius=_s(2),
                               fill=_mix(accent, _WHITE, 0.2))
        draw.text((_s(x + 24), _s(y + 26)), label, font=_font(12), fill=_MUTED, anchor="lm")
        ix, iy = x + _TILE_W - 30, y + 28
        if element is None:
            _coin(draw, ix, iy, 15, gold)
        else:
            _sigil(draw, img, element, ix, iy, 14, _mix(accent, _WHITE, 0.55), _mix(accent, _INK, 0.5))
            draw = ImageDraw.Draw(img)
        dex_tile = i == 5  # smaller and higher, to leave room for the bar and the seen line below it
        draw.text((_s(x + 24), _s(y + (56 if dex_tile else 62))),
                  big[i], font=_fit_font(draw, big[i], _TILE_W - 44, 30 if dex_tile else 36, 16),
                  fill=_WHITE, anchor="lm")
        if i == 4:  # daily: ready or waiting (the countdown itself stays in the embed text)
            chip = _rgb(catch_theme.state_color("success")) if ready else _mix(_INK, _WHITE, 0.30)
            _pill(draw, x + 24, y + 80, "READY" if ready else "WAITING", chip, size=12, pad=10, h=22)
        elif i == 5:  # dex: caught bar with the seen part dimmer
            bx0, bx1, by = x + 24, x + _TILE_W - 20, y + 80
            draw.rounded_rectangle((_s(bx0), _s(by), _s(bx1), _s(by + 8)), radius=_s(4), fill=_mix(_INK, _WHITE, 0.12))
            if dex_total > 0:
                seen_to = min(1.0, (dex_caught + dex_seen) / dex_total)
                if dex_seen:
                    draw.rounded_rectangle((_s(bx0), _s(by), _s(bx0 + (bx1 - bx0) * seen_to), _s(by + 8)),
                                           radius=_s(4), fill=_mix(accent, _INK, 0.55))
                if dex_caught:
                    draw.rounded_rectangle((_s(bx0), _s(by), _s(bx0 + (bx1 - bx0) * min(1.0, dex_caught / dex_total)),
                                            _s(by + 8)), radius=_s(4), fill=_mix(accent, _WHITE, 0.15))
            draw.text((_s(x + 24), _s(y + 98)), sub[i], font=_font(12), fill=_MUTED, anchor="lm")
        elif sub[i]:
            draw.text((_s(x + 24), _s(y + 92)), sub[i], font=_fit_font(draw, sub[i], _TILE_W - 44, 13, 9),
                      fill=_MUTED, anchor="lm")

    out = io.BytesIO()
    img.resize((W, STATUS_H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=64)
def _cached(key: tuple) -> bytes:
    return _draw_status(*key)


async def status_card_png(status) -> bytes:
    """PNG for a ``PlayerStatus``. Raises on a bad value; callers treat that as "no card"."""
    return await asyncio.to_thread(_cached, _key(status))


def status_file(data: bytes) -> discord.File:
    """A fresh ``discord.File`` per send (never reuse one)."""
    return discord.File(io.BytesIO(data), filename=STATUS_FILE)


def clear_status_cache() -> None:
    _cached.cache_clear()


async def status_art(embed: discord.Embed, status) -> discord.File | None:
    """Attach the status card to ``embed`` and drop the plain fields it replaces.

    Returns ``None`` with the embed untouched if the card cannot be drawn. The Daily field is kept.
    """
    try:
        data = await status_card_png(status)
    except Exception:
        logger.warning("Catch status card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{STATUS_FILE}")
    for key in DROPPED_FIELDS:
        _drop_field(embed, text(key))
    return status_file(data)


__all__ = ["DROPPED_FIELDS", "STATUS_FILE", "STATUS_H", "clear_status_cache", "status_art", "status_card_png",
           "status_file"]
