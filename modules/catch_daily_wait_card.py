"""Drawn card for the "daily reward not ready" screen.

Same approach as ``catch_coin_card``: Pillow, colours from ``catch_theme``, and always optional.
``daily_wait_art`` returns a ``discord.File`` (or ``None`` when drawing fails) and points the embed
at it; the caller keeps the plain embed. Only the streak number is drawn: the live countdown stays
in the embed text as a Discord relative timestamp, because an image cannot tick down and a cached
image would go stale. No player text is drawn.
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
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _rgb, _s,
)
from modules.catch_coin_card import DAILY_H, STREAK_PIPS, _coin, _png

logger = logging.getLogger(__name__)

WAIT_FILE = "daily_wait.png"
_GREY = (120, 126, 140)
_MUTE = 0.5  # how far the gold is pulled toward grey: reads as "locked", still part of the coin family


def _gold() -> tuple[int, int, int]:
    return _mix(_rgb(catch_theme.state_color("warning")), _GREY, _MUTE)


def _clock(draw, cx: float, cy: float, r: float, gold) -> None:
    """Small clock badge: a dark disc, a gold ring and two hands."""
    draw.ellipse((_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)), fill=_INK, outline=gold, width=_s(4))
    draw.line((_s(cx), _s(cy), _s(cx), _s(cy - r * 0.6)), fill=_WHITE, width=_s(4))
    draw.line((_s(cx), _s(cy), _s(cx + r * 0.45), _s(cy + r * 0.2)), fill=_WHITE, width=_s(4))
    draw.ellipse((_s(cx - 3), _s(cy - 3), _s(cx + 3), _s(cy + 3)), fill=gold)


def _draw_wait(streak: int) -> bytes:
    gold = _gold()
    img = Image.new("RGB", (W * SCALE, DAILY_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(gold, _INK, 0.84), _mix(gold, _INK, 0.95)
    for y in range(DAILY_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (DAILY_H * SCALE)))
    img = _glow(img, 105, 125, 130, gold, 55)
    draw = ImageDraw.Draw(img)
    _coin(draw, 105, 125, 72, gold)
    _clock(draw, 105 + 54, 125 + 54, 26, gold)
    draw.text((_s(210), _s(44)), "DAILY REWARD", font=_font(20), fill=_MUTED, anchor="lm")
    headline = "NOT READY YET"
    draw.text((_s(210), _s(98)), headline, font=_fit_font(draw, headline, W - 210 - 24, 56, 24), fill=_WHITE, anchor="lm")
    draw.text((_s(210), _s(142)), "COME BACK SOON", font=_font(18), fill=gold, anchor="lm")
    streak = int(streak)  # callers clamp; the checks below treat anything <= 0 as no streak
    filled = 0 if streak <= 0 else ((streak - 1) % STREAK_PIPS) + 1
    draw.text((_s(210), _s(194)), "STREAK", font=_font(14), fill=_MUTED, anchor="lm")
    for i in range(STREAK_PIPS):
        x = 292 + i * 28
        on = i < filled
        draw.ellipse((_s(x - 9), _s(194 - 9), _s(x + 9), _s(194 + 9)), fill=gold if on else _mix(_INK, _WHITE, 0.12),
                     outline=_mix(gold, _INK, 0.5) if on else None, width=_s(2))
    if streak > 0:
        draw.text((_s(W - 24), _s(194)), f"DAY {streak}", font=_font(18), fill=_WHITE, anchor="rm")
    return _png(img, DAILY_H)


@lru_cache(maxsize=64)
def _cached_wait(streak: int) -> bytes:
    return _draw_wait(streak)


def clear_daily_wait_cache() -> None:
    _cached_wait.cache_clear()


async def daily_wait_art(embed: discord.Embed, *, streak: int) -> discord.File | None:
    """Attach the not-ready card to ``embed``. The embed text is kept: it holds the live countdown."""
    try:
        data = await asyncio.to_thread(_cached_wait, max(0, int(streak)))
    except Exception:
        logger.warning("Catch daily wait card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{WAIT_FILE}")
    return discord.File(io.BytesIO(data), filename=WAIT_FILE)


__all__ = ["WAIT_FILE", "clear_daily_wait_cache", "daily_wait_art"]
