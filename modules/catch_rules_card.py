"""Drawn banner for the server rules screen.

Same approach as the other cards: Pillow, colours from ``catch_theme``, always optional.
It draws only numbers and fixed words: ON/OFF, the speed preset (one of ``SPEED_PRESETS``, anything
else is drawn as ``normal``) and the three timings. Channel and role lists stay in the embed fields,
which are never removed. No owner or player text is drawn.
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
from modules.catch_setup import SPEED_PRESETS

logger = logging.getLogger(__name__)

RULES_H = 250
RULES_FILE = "rules.png"
MAX_SHOWN = 99999  # keeps a silly value from overflowing a tile


def _duration(seconds: int) -> str:
    seconds = max(0, min(int(seconds), MAX_SHOWN * 60))
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} MIN" if minutes and not rest else f"{seconds} SEC"


def _key(enabled, preset, every, wait, despawn) -> tuple:
    preset = preset if preset in SPEED_PRESETS else "normal"
    return (bool(enabled), preset, max(0, min(int(every), MAX_SHOWN)), _duration(wait), _duration(despawn))


def _draw_rules(enabled: bool, preset: str, every: int, wait: str, despawn: str) -> bytes:
    state = _rgb(catch_theme.state_color("success" if enabled else "disabled"))
    img = Image.new("RGB", (W * SCALE, RULES_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(state, _INK, 0.82), _mix(state, _INK, 0.93)
    for y in range(RULES_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (RULES_H * SCALE)))
    img = _glow(img, 70, 60, 110, state, 50)
    draw = ImageDraw.Draw(img)
    ring = _mix(state, _WHITE, 0.25)
    draw.ellipse((_s(34), _s(24), _s(106), _s(96)), fill=_mix(state, _INK, 0.6), outline=ring, width=_s(3))
    _sigil(draw, img, "frost", 70, 60, 22, _mix(state, _WHITE, 0.6), _mix(state, _INK, 0.5))
    draw = ImageDraw.Draw(img)
    draw.text((_s(124), _s(44)), "SERVER RULES", font=_font(32), fill=_WHITE, anchor="lm")
    draw.text((_s(124), _s(78)), f"SPAWN SPEED: {preset.upper()}", font=_font(16), fill=_MUTED, anchor="lm")
    label = "CATCHING ON" if enabled else "CATCHING OFF"
    pw = draw.textlength(label, font=_font(18)) / SCALE + 28
    draw.rounded_rectangle((_s(W - 24 - pw), _s(28), _s(W - 24), _s(62)), radius=_s(17),
                           fill=_mix(state, _INK, 0.5), outline=ring, width=_s(2))
    draw.text((_s(W - 24 - pw / 2), _s(45)), label, font=_font(18), fill=_WHITE, anchor="mm")

    tiles = (("SPAWN EVERY", f"{every} MSGS"), ("MIN WAIT", wait), ("TIME TO CATCH", despawn))
    gap = 14
    tw = (W - 48 - gap * 2) / 3
    for i, (name, value) in enumerate(tiles):
        tx = 24 + i * (tw + gap)
        draw.rounded_rectangle((_s(tx), _s(120), _s(tx + tw), _s(226)), radius=_s(14),
                               fill=_mix(_INK, _WHITE, 0.07), outline=_mix(_INK, _WHITE, 0.16), width=_s(1))
        draw.text((_s(tx + tw / 2), _s(144)), name, font=_font(14), fill=_MUTED, anchor="mm")
        draw.text((_s(tx + tw / 2), _s(184)), value, font=_fit_font(draw, value, int(tw - 24), 34, 14),
                  fill=_WHITE, anchor="mm")
    return _png(img, RULES_H)


@lru_cache(maxsize=64)
def _cached_rules(enabled: bool, preset: str, every: int, wait: str, despawn: str) -> bytes:
    return _draw_rules(enabled, preset, every, wait, despawn)


def clear_rules_cache() -> None:
    _cached_rules.cache_clear()


async def rules_art(embed: discord.Embed, setup) -> discord.File | None:
    """Attach the rules banner to ``embed``. The embed text is kept: it holds the channel lists."""
    try:
        key = _key(setup.enabled, setup.speed_preset, setup.spawn_every_n_messages,
                   setup.min_seconds_between_spawns, setup.despawn_seconds)
        data = await asyncio.to_thread(_cached_rules, *key)
    except Exception:
        logger.warning("Catch rules card not drawn", exc_info=True)
        return None
    embed.set_image(url=f"attachment://{RULES_FILE}")
    return discord.File(io.BytesIO(data), filename=RULES_FILE)


__all__ = ["RULES_FILE", "RULES_H", "clear_rules_cache", "rules_art"]
