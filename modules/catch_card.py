"""Drawn creature card for the catch detail screen.

There is no creature art yet, so the card is built from the creature's own data: an
element-coloured background, a rarity-coloured medallion with an element sigil, level and
type chips, stat bars and an XP bar. Colours come from ``catch_theme`` (so the owner's
theme edits apply). When real art exists it can be pasted into the medallion later.

Rendering is plain Pillow, cached by the visible fields, and runs in a worker thread.
Callers must treat a failure as "no card" and fall back to the text embed.
"""

from __future__ import annotations

import asyncio
import io
import math
import os
import random
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from modules import catch_theme
from modules.catch_emoji import STAT_BAR_MAX
from modules.catch_game import LEVEL_MAX
from modules.catch_xp import xp_to_next

W, H = 720, 400
SCALE = 2  # draw at 2x, shrink at the end: smooth edges
STAT_ORDER = ("vigor", "power", "guard", "speed", "spirit")
_FONT_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "DejaVuSans-Bold.ttf"))
_INK = (10, 12, 22)
_WHITE = (245, 247, 252)
_MUTED = (160, 168, 184)


def _rgb(colour) -> tuple[int, int, int]:
    v = colour.value
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _on(colour) -> tuple[int, int, int]:
    """Readable text colour for a filled chip."""
    lum = 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]
    return _INK if lum > 140 else _WHITE


@lru_cache(maxsize=32)
def _font(size: int):
    try:
        return ImageFont.truetype(_FONT_FILE, size * SCALE)
    except Exception:
        try:
            return ImageFont.load_default(size=size * SCALE)
        except TypeError:
            return ImageFont.load_default()


def _s(n: float) -> int:
    return int(round(n * SCALE))


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    return int(draw.textlength(text, font=font))


def _fit_font(draw, text: str, max_w: int, start: int, floor: int = 18):
    size = start
    while size > floor and _text_w(draw, text, _font(size)) > max_w * SCALE:
        size -= 2
    return _font(size)


def _pill(draw, x: float, y: float, text: str, fill, *, size: int = 14, pad: int = 10, h: int = 28) -> float:
    font = _font(size)
    w = _text_w(draw, text, font) / SCALE + pad * 2
    if x + w > W - 20:  # never run off the card: skip a chip that does not fit
        return x
    draw.rounded_rectangle((_s(x), _s(y), _s(x + w), _s(y + h)), radius=_s(h / 2), fill=fill)
    draw.text((_s(x + w / 2), _s(y + h / 2)), text, font=font, fill=_on(fill), anchor="mm")
    return x + w + 8


# --- sigils: simple geometry per element, drawn inside a unit circle -------------------

def _poly(draw, pts, cx, cy, r, fill, rot: float = 0.0):
    c, s = math.cos(rot), math.sin(rot)
    out = [(_s(cx + (x * c - y * s) * r), _s(cy + (x * s + y * c) * r)) for x, y in pts]
    draw.polygon(out, fill=fill)


def _sigil(draw, img, element: str, cx: float, cy: float, r: float, fill, dark) -> None:
    if element == "ember":
        _poly(draw, [(0, -1), (0.35, -0.5), (0.72, 0.05), (0.62, 0.62), (0.25, 0.97), (-0.25, 0.97),
                     (-0.62, 0.62), (-0.72, 0.08), (-0.34, -0.3), (-0.2, -0.62)], cx, cy, r, fill)
        _poly(draw, [(0, 0.05), (0.3, 0.5), (0.18, 0.88), (-0.18, 0.88), (-0.3, 0.5)], cx, cy, r, dark)
    elif element == "tide":
        pts = [(0, -1)] + [(0.66 * math.cos(math.radians(a)), 0.3 + 0.66 * math.sin(math.radians(a)))
                           for a in range(-35, 216, 12)]
        _poly(draw, pts, cx, cy, r, fill)
        _poly(draw, [(-0.28, 0.35), (-0.12, 0.62), (-0.34, 0.6)], cx, cy, r, dark)
    elif element == "verdant":
        pts = [(0.62 * (1 - t * t), t) for t in [i / 10 for i in range(-10, 11)]]
        pts += [(-0.62 * (1 - t * t), t) for t in [i / 10 for i in range(10, -11, -1)]]
        _poly(draw, pts, cx, cy, r, fill, rot=math.radians(35))
        draw.line((_s(cx - r * 0.5), _s(cy + r * 0.5), _s(cx + r * 0.5), _s(cy - r * 0.5)), fill=dark, width=_s(4))
    elif element == "volt":
        _poly(draw, [(0.15, -1), (-0.55, 0.1), (-0.05, 0.1), (-0.2, 1), (0.55, -0.15), (0.05, -0.15)], cx, cy, r, fill)
    elif element == "frost":
        for k in range(3):
            a = math.radians(k * 60 + 90)
            dx, dy = math.cos(a) * r, math.sin(a) * r
            draw.line((_s(cx - dx), _s(cy - dy), _s(cx + dx), _s(cy + dy)), fill=fill, width=_s(7))
            for sign in (1, -1):
                bx, by = cx + dx * 0.6 * sign, cy + dy * 0.6 * sign
                for off in (-35, 35):
                    b = a + math.radians(off) + (math.pi if sign < 0 else 0)
                    draw.line((_s(bx), _s(by), _s(bx + math.cos(b) * r * 0.28), _s(by + math.sin(b) * r * 0.28)),
                              fill=fill, width=_s(5))
    elif element == "stone":
        hexa = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in range(30, 390, 60)]
        _poly(draw, hexa, cx, cy, r, fill)
        for i in (0, 2, 4):
            draw.line((_s(cx), _s(cy), _s(cx + hexa[i][0] * r), _s(cy + hexa[i][1] * r)), fill=dark, width=_s(4))
    elif element == "gale":
        for k, rr in enumerate((1.0, 0.72, 0.44)):
            box = (_s(cx - r * rr), _s(cy - r * rr), _s(cx + r * rr), _s(cy + r * rr))
            draw.arc(box, start=200 + k * 40, end=470 + k * 40, fill=fill, width=_s(7))
    elif element == "umbra":
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        md.ellipse((_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)), fill=255)
        md.ellipse((_s(cx - r * 0.3), _s(cy - r * 1.05), _s(cx + r * 1.5), _s(cy + r * 0.95)), fill=0)
        img.paste(Image.new("RGB", img.size, fill), (0, 0), mask)
    elif element == "lumen":
        draw.ellipse((_s(cx - r * 0.5), _s(cy - r * 0.5), _s(cx + r * 0.5), _s(cy + r * 0.5)), fill=fill)
        for k in range(8):
            a = math.radians(k * 45)
            draw.line((_s(cx + math.cos(a) * r * 0.68), _s(cy + math.sin(a) * r * 0.68),
                       _s(cx + math.cos(a) * r), _s(cy + math.sin(a) * r)), fill=fill, width=_s(7))
    else:
        _poly(draw, [(0, -1), (0.8, 0), (0, 1), (-0.8, 0)], cx, cy, r, fill)


def _sparkle(draw, x: float, y: float, r: float, fill) -> None:
    pts = [(0, -1), (0.18, -0.18), (1, 0), (0.18, 0.18), (0, 1), (-0.18, 0.18), (-1, 0), (-0.18, -0.18)]
    _poly(draw, pts, x, y, r, fill)


def _glow(base: Image.Image, cx: float, cy: float, r: float, colour, strength: int) -> Image.Image:
    layer = Image.new("RGBA", base.size, colour + (0,))
    ImageDraw.Draw(layer).ellipse((_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)), fill=colour + (strength,))
    layer = layer.filter(ImageFilter.GaussianBlur(_s(r * 0.45)))
    return Image.alpha_composite(base.convert("RGBA"), layer).convert("RGB")


def _draw_card(*, species_id: int, name: str, rarity: str, element: str, element2: str | None, level: int,
               xp: int, shiny: bool, special: bool, stats: tuple[int, ...], creature_id: int) -> bytes:
    c1 = _rgb(catch_theme.element_color(element))
    c2 = _rgb(catch_theme.element_color(element2)) if element2 else c1
    rar = _rgb(catch_theme.rarity_color(rarity, shiny=shiny, special=special))

    img = Image.new("RGB", (W * SCALE, H * SCALE), _INK)
    draw = ImageDraw.Draw(img)
    top, bottom = _mix(c1, _INK, 0.80), _mix(c2, _INK, 0.90)
    for y in range(H * SCALE):  # vertical gradient
        draw.line((0, y, W * SCALE, y), fill=_mix(top, bottom, y / (H * SCALE)))
    img = _glow(img, 175, 200, 190, c1, 150)
    img = _glow(img, 640, 380, 200, c2, 90)
    draw = ImageDraw.Draw(img)

    rng = random.Random(species_id * 1009 + level)
    for _ in range(26):  # faint background dots
        x, y, rr = rng.uniform(0, W), rng.uniform(0, H), rng.uniform(1.2, 3)
        draw.ellipse((_s(x - rr), _s(y - rr), _s(x + rr), _s(y + rr)), fill=_mix(top, _WHITE, 0.14))

    # medallion
    mx, my, mr = 175, 200, 112
    draw.ellipse((_s(mx - mr), _s(my - mr), _s(mx + mr), _s(my + mr)), fill=_mix(c1, _INK, 0.62))
    draw.ellipse((_s(mx - mr), _s(my - mr), _s(mx + mr), _s(my + mr)), outline=rar, width=_s(7))
    draw.ellipse((_s(mx - mr + 14), _s(my - mr + 14), _s(mx + mr - 14), _s(my + mr - 14)),
                 outline=_mix(rar, _INK, 0.55), width=_s(2))
    _sigil(draw, img, element, mx, my, 62, _mix(c1, _WHITE, 0.55), _mix(c1, _INK, 0.5))
    draw = ImageDraw.Draw(img)
    if element2:  # second type as a small badge on the ring
        bx, by = mx + 78, my + 78
        draw.ellipse((_s(bx - 26), _s(by - 26), _s(bx + 26), _s(by + 26)), fill=_mix(c2, _INK, 0.55),
                     outline=rar, width=_s(3))
        _sigil(draw, img, element2, bx, by, 15, _mix(c2, _WHITE, 0.6), _mix(c2, _INK, 0.5))
        draw = ImageDraw.Draw(img)
    if shiny or special:
        spark = rar
        for _ in range(9):
            a = rng.uniform(0, math.tau)
            d = rng.uniform(mr + 6, mr + 46)
            _sparkle(draw, mx + math.cos(a) * d, my + math.sin(a) * d, rng.uniform(5, 11), spark)

    # text panel
    px = 335
    title_font = _fit_font(draw, name, W - px - 130, 42)
    draw.text((_s(px), _s(54)), name, font=title_font, fill=_WHITE, anchor="lm")
    if shiny or special:
        label = "SHINY" if shiny else "SPECIAL"
        _pill(draw, W - 24 - (_text_w(draw, label, _font(14)) / SCALE + 24), 20, label, rar, size=14, h=26)
    x = _pill(draw, px, 92, f"LV {level}", _mix(_WHITE, c1, 0.15), size=14)
    x = _pill(draw, x, 92, rarity.upper(), rar, size=14)
    x = _pill(draw, x, 92, element.upper(), c1, size=14)
    if element2:
        _pill(draw, x, 92, element2.upper(), c2, size=14)

    # stat bars
    y0 = 142
    for i, key in enumerate(STAT_ORDER):
        v = stats[i] if i < len(stats) else 0
        y = y0 + i * 31
        draw.text((_s(px), _s(y + 9)), key.upper(), font=_font(13), fill=_MUTED, anchor="lm")
        bx0, bx1 = px + 78, W - 78
        draw.rounded_rectangle((_s(bx0), _s(y + 2), _s(bx1), _s(y + 16)), radius=_s(7), fill=_mix(_INK, _WHITE, 0.10))
        frac = max(0.0, min(1.0, v / STAT_BAR_MAX))
        if frac > 0:
            fx = bx0 + max(14, (bx1 - bx0) * frac)
            draw.rounded_rectangle((_s(bx0), _s(y + 2), _s(fx), _s(y + 16)), radius=_s(7), fill=_mix(c1, _WHITE, 0.25))
        draw.text((_s(W - 24), _s(y + 9)), str(v), font=_font(15), fill=_WHITE, anchor="rm")

    # xp bar
    need = xp_to_next(level) if level < LEVEL_MAX else 0
    yb = 322
    label = "MAX LEVEL" if need == 0 else f"XP  {xp:,} / {need:,}"
    draw.text((_s(px), _s(yb)), label, font=_font(13), fill=_MUTED, anchor="lm")
    draw.rounded_rectangle((_s(px), _s(yb + 14), _s(W - 24), _s(yb + 26)), radius=_s(6), fill=_mix(_INK, _WHITE, 0.10))
    frac = 1.0 if need == 0 else max(0.0, min(1.0, xp / need))
    if frac > 0:
        draw.rounded_rectangle((_s(px), _s(yb + 14), _s(px + (W - 24 - px) * frac), _s(yb + 26)),
                               radius=_s(6), fill=_mix(rar, _WHITE, 0.15))
    draw.text((_s(24), _s(H - 22)), f"#{creature_id}", font=_font(13), fill=_MUTED, anchor="lm")

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()


@lru_cache(maxsize=128)
def _cached(key: tuple) -> bytes:
    (species_id, name, rarity, element, element2, level, xp, shiny, special, stats, creature_id) = key
    return _draw_card(species_id=species_id, name=name, rarity=rarity, element=element, element2=element2,
                      level=level, xp=xp, shiny=shiny, special=special, stats=stats, creature_id=creature_id)


def _key(d) -> tuple:
    return (
        d.species_id, d.name, d.rarity, d.element, d.element2, int(d.level), int(d.xp), bool(d.shiny),
        bool(d.special), tuple(int(d.stats.get(k, 0)) for k in STAT_ORDER), d.id,
    )


async def creature_card_png(detail) -> bytes:
    """PNG bytes for a ``CreatureDetail``. Raises on failure: callers fall back to text."""
    return await asyncio.to_thread(_cached, _key(detail))


def clear_card_cache() -> None:
    _cached.cache_clear()


__all__ = ["H", "W", "clear_card_cache", "creature_card_png"]
