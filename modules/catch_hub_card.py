"""Drawn card for the catch hub menu.

Shows the six categories as tabs (the selected one lit) and the selected category's actions.
Built only from the hub's own static data: no database, no player text. Same approach as
``catch_card``: Pillow, colours from ``catch_theme``, helpers imported from ``catch_card``, renders
cached and run in a worker thread. Callers treat an exception as "no card" and use the plain embed.
"""

from __future__ import annotations

import asyncio
import io
import random
from functools import lru_cache

import discord
from PIL import Image, ImageDraw

from modules import catch_theme
from modules.catch_card import (
    _INK, _MUTED, _WHITE, SCALE, W, _fit_font, _font, _glow, _mix, _rgb, _s, _sigil,
)

HUB_H = 360
HUB_FILE = "hub.png"
# Category key -> element whose colour and sigil represent it (unknown keys fall back to "stone").
CATEGORY_ELEMENT = {
    "play": "ember", "collect": "verdant", "social": "tide", "economy": "lumen", "battle": "volt", "info": "frost",
}


def _element(key: str) -> str:
    return CATEGORY_ELEMENT.get(key, "stone")


def _draw_hub(cats: tuple, selected: str) -> bytes:
    """``cats`` is ``((key, label, description, ((action, blurb), ...)), ...)``."""
    sel = next((c for c in cats if c[0] == selected), cats[0])
    c1 = _rgb(catch_theme.element_color(_element(sel[0])))
    img = Image.new("RGB", (W * SCALE, HUB_H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(c1, _INK, 0.80), _mix(c1, _INK, 0.93)
    for y in range(HUB_H * SCALE):
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (HUB_H * SCALE)))
    img = _glow(img, 120, 40, 220, c1, 120)
    draw = ImageDraw.Draw(img)
    rng = random.Random(sum(map(ord, sel[0])))
    for _ in range(20):
        x, y, rr = rng.uniform(0, W), rng.uniform(0, HUB_H), rng.uniform(1.2, 3)
        draw.ellipse((_s(x - rr), _s(y - rr), _s(x + rr), _s(y + rr)), fill=_mix(top, _WHITE, 0.14))

    draw.text((_s(28), _s(34)), "CATCH HUB", font=_font(26), fill=_WHITE, anchor="lm")
    draw.text((_s(W - 28), _s(34)), sel[1].upper(), font=_font(18), fill=_mix(c1, _WHITE, 0.45), anchor="rm")
    draw.text((_s(28), _s(66)), sel[2], font=_fit_font(draw, sel[2], W - 56, 15, 11), fill=_MUTED, anchor="lm")

    n = len(cats)
    gap, x0, y0, y1 = 8, 24, 92, 178
    tw = (W - 2 * x0 - gap * (n - 1)) / n
    for i, (key, label, _d, _a) in enumerate(cats):
        tx = x0 + i * (tw + gap)
        ec = _rgb(catch_theme.element_color(_element(key)))
        on = key == sel[0]
        fill = _mix(ec, _INK, 0.45) if on else _mix(_INK, _WHITE, 0.07)
        draw.rounded_rectangle((_s(tx), _s(y0), _s(tx + tw), _s(y1)), radius=_s(14), fill=fill,
                               outline=_mix(ec, _WHITE, 0.35) if on else _mix(_INK, _WHITE, 0.16), width=_s(3 if on else 1))
        cx = tx + tw / 2
        glyph = _mix(ec, _WHITE, 0.6) if on else _mix(ec, _INK, 0.35)
        _sigil(draw, img, _element(key), cx, y0 + 34, 20, glyph, _mix(ec, _INK, 0.5))
        draw = ImageDraw.Draw(img)
        draw.text((_s(cx), _s(y1 - 18)), label, font=_fit_font(draw, label, int(tw - 12), 15, 10),
                  fill=_WHITE if on else _MUTED, anchor="mm")

    py = 198
    draw.rounded_rectangle((_s(24), _s(py), _s(W - 24), _s(HUB_H - 20)), radius=_s(14), fill=_mix(_INK, _WHITE, 0.06))
    rowh = (HUB_H - 20 - py) / max(1, len(sel[3]))
    for i, (name, blurb) in enumerate(sel[3]):
        cy = py + rowh * i + rowh / 2
        draw.ellipse((_s(46), _s(cy - 6), _s(58), _s(cy + 6)), fill=_mix(c1, _WHITE, 0.3))
        draw.text((_s(74), _s(cy)), name, font=_font(19), fill=_WHITE, anchor="lm")
        draw.text((_s(W - 44), _s(cy)), blurb, font=_fit_font(draw, blurb, 360, 15, 11), fill=_MUTED, anchor="rm")

    out = io.BytesIO()
    img.resize((W, HUB_H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=32)
def _cached(key: tuple) -> bytes:
    return _draw_hub(*key)


async def hub_card_png(categories, selected: str) -> bytes:
    """PNG for the hub. ``categories`` is the hub's ``CATEGORIES`` (objects with key, label, description, actions).

    Raises on bad input (no categories); callers treat that as "no card".
    """
    cats = tuple((c.key, c.label, c.description, tuple((str(a), str(b)) for a, b in c.actions)) for c in categories)
    if not cats:
        raise ValueError("no categories")
    return await asyncio.to_thread(_cached, (cats, str(selected)))


def hub_file(data: bytes) -> discord.File:
    """A fresh ``discord.File`` per send (never reuse one)."""
    return discord.File(io.BytesIO(data), filename=HUB_FILE)


def clear_hub_cache() -> None:
    _cached.cache_clear()


__all__ = ["CATEGORY_ELEMENT", "HUB_FILE", "HUB_H", "clear_hub_cache", "hub_card_png", "hub_file"]
