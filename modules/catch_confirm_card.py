"""Drawn cards for the evolve and release confirm screens of the catch game.

Evolve: the creature now, an arrow, and what it becomes, with the five stats before and after.
Release: a quiet farewell card for the creature about to be set free.

Same approach as ``catch_card``: Pillow, colours from ``catch_theme``, helpers imported from
``catch_card``, renders cached and run in a worker thread. Player-chosen text (nicknames) is never
drawn. Callers treat any exception as "no card" and fall back to the plain embed.
"""

from __future__ import annotations

import asyncio
import io
from functools import lru_cache

import discord
from PIL import Image

from modules import catch_theme
from modules.catch_card import (
    _INK, _MUTED, _WHITE, H, SCALE, STAT_ORDER, W, _backdrop, _fit_font, _font, _medallion, _mix, _pill, _rgb, _s,
    _text_w,
)

EVOLVE_FILE = "evolve.png"
RELEASE_FILE = "release.png"
_GREY = (120, 126, 140)


def _colours(species: dict, *, shiny: bool, special: bool, muted: float = 0.0):
    c1 = _rgb(catch_theme.element_color(species["element"]))
    e2 = species.get("element2")
    c2 = _rgb(catch_theme.element_color(e2)) if e2 else c1
    rar = _rgb(catch_theme.rarity_color(species["rarity"], shiny=shiny, special=special))
    if muted:
        c1, c2, rar = _mix(c1, _GREY, muted), _mix(c2, _GREY, muted), _mix(rar, _GREY, muted)
    return c1, c2, rar


def _arrow(draw, cx: float, cy: float, colour) -> None:
    pts = [(cx - 34, cy - 9), (cx + 6, cy - 9), (cx + 6, cy - 20), (cx + 34, cy), (cx + 6, cy + 20), (cx + 6, cy + 9),
           (cx - 34, cy + 9)]
    draw.polygon([(_s(x), _s(y)) for x, y in pts], fill=colour)


def _draw_evolve(*, a: tuple, b: tuple, level: int, shiny: bool, special: bool,
                 before: tuple[int, ...], after: tuple[int, ...]) -> bytes:
    (aid, aname, arar, ael, ael2), (bid, bname, brar, bel, bel2) = a, b
    sa = {"element": ael, "element2": ael2, "rarity": arar}
    sb = {"element": bel, "element2": bel2, "rarity": brar}
    a1, a2, ar = _colours(sa, shiny=shiny, special=special)
    b1, b2, br = _colours(sb, shiny=shiny, special=special)
    img, draw, rng = _backdrop(b1, a1, aid * 977 + bid * 31 + level)

    ya = 120
    draw = _medallion(img, draw, rng, ael, ael2, a1, a2, ar, 130, ya, 70, sparkles=False)
    draw = _medallion(img, draw, rng, bel, bel2, b1, b2, br, W - 130, ya, 70, sparkles=True)
    _arrow(draw, W / 2, ya, _mix(_rgb(catch_theme.state_color("success")), _WHITE, 0.2))
    draw.text((_s(W / 2), _s(ya - 46)), "EVOLVE?", font=_font(18), fill=_mix(br, _WHITE, 0.4), anchor="mm")
    draw.text((_s(W / 2), _s(ya + 44)), f"LV {level}", font=_font(16), fill=_MUTED, anchor="mm")

    for cx, name, rar, el, el2, c1, c2 in ((130, aname, arar, ael, ael2, a1, a2), (W - 130, bname, brar, bel, bel2, b1, b2)):
        draw.text((_s(cx), _s(244)), name, font=_fit_font(draw, name, 250, 28, 18), fill=_WHITE, anchor="mm")
        labels = [(rar.upper(), _rgb(catch_theme.rarity_color(rar, shiny=shiny, special=special))), (el.upper(), c1)]
        if el2:
            labels.append((el2.upper(), c2))
        total = sum(_text_w(draw, t, _font(12)) / SCALE + 20 + 6 for t, _ in labels) - 6
        x = cx - total / 2
        for t, col in labels:
            x = _pill(draw, x, 270, t, col, size=12, pad=10, h=24) + 6

    ok = _rgb(catch_theme.state_color("success"))
    n = len(STAT_ORDER)
    gap, x0 = 10, 24
    tw = (W - 2 * x0 - gap * (n - 1)) / n
    for i, key in enumerate(STAT_ORDER):
        tx = x0 + i * (tw + gap)
        y0, y1 = 304, 390
        draw.rounded_rectangle((_s(tx), _s(y0), _s(tx + tw), _s(y1)), radius=_s(12), fill=_mix(_INK, _WHITE, 0.08),
                               outline=_mix(_INK, _WHITE, 0.16), width=_s(1))
        v0 = before[i] if i < len(before) else 0
        v1 = after[i] if i < len(after) else 0
        cx = tx + tw / 2
        draw.text((_s(cx), _s(y0 + 16)), key.upper(), font=_font(12), fill=_MUTED, anchor="mm")
        draw.text((_s(cx), _s(y0 + 44)), f"{v0} > {v1}", font=_fit_font(draw, f"{v0} > {v1}", int(tw - 12), 20, 12),
                  fill=_WHITE, anchor="mm")
        d = v1 - v0
        col = ok if d > 0 else (_MUTED if d == 0 else _rgb(catch_theme.state_color("danger")))
        draw.text((_s(cx), _s(y0 + 68)), f"{d:+d}", font=_font(15), fill=col, anchor="mm")

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_release(*, species_id: int, name: str, rarity: str, element: str, element2: str | None, level: int,
                  shiny: bool, special: bool, buddy: bool) -> bytes:
    sp = {"element": element, "element2": element2, "rarity": rarity}
    c1, c2, rar = _colours(sp, shiny=shiny, special=special, muted=0.55)
    img, draw, rng = _backdrop(c1, _GREY, species_id * 613 + level)
    draw = _medallion(img, draw, rng, element, element2, c1, c2, rar, 175, 200, 112, sparkles=False)

    px = 335
    draw.text((_s(px), _s(60)), "RELEASE?", font=_font(22), fill=_mix(_rgb(catch_theme.state_color("warning")), _WHITE, 0.2),
              anchor="lm")
    draw.text((_s(px), _s(112)), name, font=_fit_font(draw, name, W - px - 24, 44, 22), fill=_WHITE, anchor="lm")
    x = _pill(draw, px, 160, f"LV {level}", _mix(_WHITE, c1, 0.15), size=14)
    x = _pill(draw, x, 160, rarity.upper(), rar, size=14)
    x = _pill(draw, x, 160, element.upper(), c1, size=14)
    if element2:
        x = _pill(draw, x, 160, element2.upper(), c2, size=14)
    x = px
    if shiny or special:
        x = _pill(draw, x, 204, "SHINY" if shiny else "SPECIAL", rar, size=14)
    if buddy:
        _pill(draw, x, 204, "YOUR BUDDY", _rgb(catch_theme.state_color("warning")), size=14)
    draw.text((_s(px), _s(280)), "Set free for good.", font=_font(20), fill=_WHITE, anchor="lm")
    draw.text((_s(px), _s(312)), "No coins, no undo.", font=_font(15), fill=_MUTED, anchor="lm")

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=64)
def _cached_evolve(key: tuple) -> bytes:
    a, b, level, shiny, special, before, after = key
    return _draw_evolve(a=a, b=b, level=level, shiny=shiny, special=special, before=before, after=after)


@lru_cache(maxsize=64)
def _cached_release(key: tuple) -> bytes:
    return _draw_release(**dict(key))


def _sp(s: dict) -> tuple:
    return (int(s["id"]), str(s["name"]), str(s["rarity"]), str(s["element"]), s.get("element2") or None)


async def evolve_card_png(species: dict, target: dict, *, level: int, shiny: bool = False, special: bool = False,
                          before: dict[str, int] | None = None, after: dict[str, int] | None = None) -> bytes:
    """``species`` / ``target`` are entries of ``catch_species.all_species()``; stats are keyed by stat name."""
    before, after = before or {}, after or {}
    key = (_sp(species), _sp(target), int(level), bool(shiny), bool(special),
           tuple(int(before.get(k, 0)) for k in STAT_ORDER), tuple(int(after.get(k, 0)) for k in STAT_ORDER))
    return await asyncio.to_thread(_cached_evolve, key)


async def release_card_png(species: dict, *, level: int, shiny: bool = False, special: bool = False,
                           buddy: bool = False) -> bytes:
    key = tuple(sorted({
        "species_id": int(species["id"]), "name": str(species["name"]), "rarity": str(species["rarity"]),
        "element": str(species["element"]), "element2": species.get("element2") or None, "level": int(level),
        "shiny": bool(shiny), "special": bool(special), "buddy": bool(buddy),
    }.items()))
    return await asyncio.to_thread(_cached_release, key)


def confirm_file(data: bytes, filename: str) -> discord.File:
    """A fresh ``discord.File`` per send (never reuse one)."""
    return discord.File(io.BytesIO(data), filename=filename)


def clear_confirm_cache() -> None:
    _cached_evolve.cache_clear()
    _cached_release.cache_clear()


__all__ = ["EVOLVE_FILE", "RELEASE_FILE", "clear_confirm_cache", "confirm_file", "evolve_card_png", "release_card_png"]
