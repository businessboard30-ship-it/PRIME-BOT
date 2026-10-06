"""Drawn coin banner (shop, sell, wallet, bag) and daily reward card.

Same idea as ``catch_card``: built from game data with Pillow, colours from ``catch_theme``,
and always optional. ``coin_art`` and ``daily_art`` return a ``discord.File`` (or ``None``
when drawing fails) and point the embed at it; the caller falls back to the plain embed.

Only numbers and catalogue item names are drawn (never player text).
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
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _pill, _rgb, _s, _text_w,
    edit_kwargs, send_kwargs,
)

logger = logging.getLogger(__name__)

BANNER_H = 200
DAILY_H = 250
COIN_FILE = "coins.png"
DAILY_FILE = "daily.png"
STREAK_PIPS = 7


def _gold() -> tuple[int, int, int]:
    return _rgb(catch_theme.state_color("warning"))


def _canvas(h: int, cx: float, cy: float):
    gold = _gold()
    img = Image.new("RGB", (W * SCALE, h * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(gold, _INK, 0.82), _mix(gold, _INK, 0.94)
    for y in range(h * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (h * SCALE)))
    img = _glow(img, cx, cy, 140, gold, 80)
    return img, ImageDraw.Draw(img), gold


def _coin(draw, cx: float, cy: float, r: float, gold) -> None:
    dark, light = _mix(gold, _INK, 0.45), _mix(gold, _WHITE, 0.45)
    draw.ellipse((_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)), fill=dark)
    draw.ellipse((_s(cx - r + 6), _s(cy - r + 6), _s(cx + r - 6), _s(cy + r - 6)), fill=gold)
    draw.ellipse((_s(cx - r * 0.72), _s(cy - r * 0.72), _s(cx + r * 0.72), _s(cy + r * 0.72)),
                 outline=dark, width=_s(4))
    pts = [(cx, cy - r * 0.4), (cx + r * 0.3, cy), (cx, cy + r * 0.4), (cx - r * 0.3, cy)]  # diamond emblem
    draw.polygon([(_s(x), _s(y)) for x, y in pts], fill=dark)
    draw.arc((_s(cx - r + 14), _s(cy - r + 14), _s(cx + r - 14), _s(cy + r - 14)), start=200, end=262,
             fill=light, width=_s(5))  # highlight


def _png(img: Image.Image, h: int) -> bytes:
    out = io.BytesIO()
    img.resize((W, h), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_banner(label: str, coins: int) -> bytes:
    img, draw, gold = _canvas(BANNER_H, 105, 100)
    _coin(draw, 105, 100, 64, gold)
    draw.text((_s(205), _s(52)), label, font=_font(20), fill=_MUTED, anchor="lm")
    number = f"{max(0, int(coins)):,}"
    draw.text((_s(205), _s(108)), number, font=_fit_font(draw, number, W - 205 - 24, 62, 24), fill=_WHITE, anchor="lm")
    draw.text((_s(205), _s(158)), "COINS", font=_font(18), fill=gold, anchor="lm")
    return _png(img, BANNER_H)


def _draw_daily(coins: int, streak: int, items: tuple[tuple[str, int], ...]) -> bytes:
    img, draw, gold = _canvas(DAILY_H, 105, 125)
    _coin(draw, 105, 125, 72, gold)
    draw.text((_s(210), _s(44)), "DAILY REWARD", font=_font(20), fill=_MUTED, anchor="lm")
    number = f"+{max(0, int(coins)):,}"
    font = _fit_font(draw, number, 300, 64, 28)
    draw.text((_s(210), _s(98)), number, font=font, fill=_WHITE, anchor="lm")
    draw.text((_s(210 + _text_w(draw, number, font) / SCALE + 14), _s(112)), "COINS", font=_font(20), fill=gold, anchor="lm")
    streak = max(0, int(streak))
    filled = 0 if streak <= 0 else ((streak - 1) % STREAK_PIPS) + 1
    draw.text((_s(210), _s(152)), "STREAK", font=_font(14), fill=_MUTED, anchor="lm")
    for i in range(STREAK_PIPS):
        x = 292 + i * 28
        on = i < filled
        draw.ellipse((_s(x - 9), _s(152 - 9), _s(x + 9), _s(152 + 9)), fill=gold if on else _mix(_INK, _WHITE, 0.12),
                     outline=_mix(gold, _INK, 0.5) if on else None, width=_s(2))
    tag = f"DAY {streak}"
    draw.text((_s(W - 24), _s(152)), tag, font=_font(18), fill=_WHITE, anchor="rm")
    x = 210
    for name, qty in items:
        x = _pill(draw, x, 186, f"{name} \u00d7{qty}", _mix(_INK, _WHITE, 0.16), size=14, h=30)
    return _png(img, DAILY_H)


@lru_cache(maxsize=64)
def _cached_banner(label: str, coins: int) -> bytes:
    return _draw_banner(label, coins)


@lru_cache(maxsize=64)
def _cached_daily(coins: int, streak: int, items: tuple) -> bytes:
    return _draw_daily(coins, streak, items)


def clear_coin_cache() -> None:
    _cached_banner.cache_clear()
    _cached_daily.cache_clear()


def _drop_field(embed: discord.Embed, name: str | None) -> None:
    if not name:
        return
    for i, field in enumerate(embed.fields):
        if field.name == name:
            embed.remove_field(i)
            return


async def coin_art(embed: discord.Embed, *, label: str, coins: int, drop_field: str | None = None) -> discord.File | None:
    """Attach a coin banner to ``embed``; the plain balance field is dropped when it renders."""
    try:
        data = await asyncio.to_thread(_cached_banner, label, int(coins))
    except Exception:
        logger.warning("Catch coin banner not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{COIN_FILE}")
    _drop_field(embed, drop_field)
    return discord.File(io.BytesIO(data), filename=COIN_FILE)


async def daily_art(embed: discord.Embed, *, coins: int, streak: int, items: dict[str, int],
                    drop_fields: tuple[str, ...] = ()) -> discord.File | None:
    """Attach the daily reward card to ``embed``; the plain reward fields are dropped when it renders."""
    try:
        key = tuple((str(name), int(qty)) for name, qty in items.items())
        data = await asyncio.to_thread(_cached_daily, int(coins), int(streak), key)
    except Exception:
        logger.warning("Catch daily card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{DAILY_FILE}")
    for name in drop_fields:
        _drop_field(embed, name)
    return discord.File(io.BytesIO(data), filename=DAILY_FILE)


__all__ = ["clear_coin_cache", "coin_art", "daily_art", "edit_kwargs", "send_kwargs"]
