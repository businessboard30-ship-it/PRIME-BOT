# path: modules/level_card_design.py
"""Custom level-up card (card plan). Validation, preview and render for a member's saved design.

Security: a design is data only. Colours must match #RRGGBB, fonts/shapes/backgrounds come from fixed
allowlists (no uploads in v1), the bio is plain text capped at BIO_MAX. The browser never decides access:
design_for_user() returns a design only while the card_plan entitlement is effective (expiry falls back to
the default card, the saved design is kept and comes back on renewal).
"""
import asyncio
import base64
import io
import os
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from modules import entitlements as ent
from modules import level_card as lc

BIO_MAX = 60
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")

FONTS = {"classic": "DejaVuSans-Bold.ttf", "clean": "FreeSansBold.ttf"}
FONT_LABELS = {"classic": "Classic", "clean": "Clean"}
SHAPES = {"circle": "Circle", "rounded": "Rounded square", "square": "Square"}
# Built-in background tiers: (top, bottom) colours, drawn as a vertical gradient.
BACKGROUNDS = {
    "slate": ("#2b2d31", "#2b2d31", "Slate"),
    "midnight": ("#05070d", "#11172b", "Midnight"),
    "ocean": ("#08233b", "#0f5a7a", "Ocean"),
    "sunset": ("#3b0f2e", "#8a3a1f", "Sunset"),
    "forest": ("#0b2a1c", "#1f5a3a", "Forest"),
    "royal": ("#1a1040", "#3d2a8f", "Royal"),
}
DEFAULT_DESIGN = {"background": "slate", "accent": "#57F287", "font": "classic", "shape": "circle", "bio": ""}


def options() -> dict:
    return {"backgrounds": {k: v[2] for k, v in BACKGROUNDS.items()}, "fonts": dict(FONT_LABELS),
            "shapes": dict(SHAPES), "bio_max": BIO_MAX}


def validate(raw) -> tuple:
    """-> (design, None) or (None, message). Unknown keys are ignored, never stored."""
    if not isinstance(raw, dict):
        return None, "Design must be an object."
    d = dict(DEFAULT_DESIGN)
    for key, allowed in (("background", BACKGROUNDS), ("font", FONTS), ("shape", SHAPES)):
        if key in raw:
            v = raw[key]
            if not isinstance(v, str) or v not in allowed:
                return None, f"Unknown {key}."
            d[key] = v
    if "accent" in raw:
        v = raw["accent"]
        if not isinstance(v, str) or not _HEX.match(v):
            return None, "Accent must be a colour like #57F287."
        d["accent"] = v.upper()
    if "bio" in raw:
        v = raw["bio"]
        if not isinstance(v, str):
            return None, "Bio must be text."
        v = _CTRL.sub("", v).strip()
        if len(v) > BIO_MAX:
            return None, f"Bio can be at most {BIO_MAX} characters."
        d["bio"] = v
    return d, None


def _font(name: str, size: int):
    path = os.path.join(lc._FONT_DIR, FONTS.get(name, FONTS["classic"]))
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return lc._load_font(size)


def _gradient(top: str, bottom: str) -> Image.Image:
    a, b = lc._hex_to_rgb(top), lc._hex_to_rgb(bottom)
    img = Image.new("RGB", (lc.CARD_WIDTH, lc.CARD_HEIGHT))
    px = ImageDraw.Draw(img)
    for y in range(lc.CARD_HEIGHT):
        t = y / max(1, lc.CARD_HEIGHT - 1)
        px.line([(0, y), (lc.CARD_WIDTH, y)], fill=tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return img


def _shape_mask(shape: str, size: int, pad: int = 0) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    box = (pad, pad, size - pad, size - pad)
    if shape == "square":
        d.rectangle(box, fill=255)
    elif shape == "rounded":
        d.rounded_rectangle(box, radius=size // 5, fill=255)
    else:
        d.ellipse(box, fill=255)
    return m


def placeholder_avatar(accent: str = "#57F287") -> bytes:
    img = Image.new("RGB", (256, 256), lc._hex_to_rgb(accent))
    d = ImageDraw.Draw(img)
    d.ellipse((88, 50, 168, 130), fill=(255, 255, 255))
    d.ellipse((48, 140, 208, 330), fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def render_custom_level_card(avatar_bytes: bytes, username: str, new_level: int, cur_xp: int, need_xp: int,
                             design: dict) -> bytes:
    d, _ = validate(design or {})
    d = d or dict(DEFAULT_DESIGN)
    top, bottom, _label = BACKGROUNDS[d["background"]]
    bg = _gradient(top, bottom)
    draw = ImageDraw.Draw(bg)
    accent = lc._hex_to_rgb(d["accent"])
    draw.rectangle([(0, 0), (14, lc.CARD_HEIGHT)], fill=accent)

    size = lc.AVATAR_SIZE
    try:
        av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((size, size))
    except Exception:
        av = Image.new("RGBA", (size, size), accent)
    ax, ay, ring = 60, (lc.CARD_HEIGHT - size) // 2, 6
    ring_img = Image.new("RGBA", (size + 2 * ring, size + 2 * ring), accent + (255,))
    bg.paste(ring_img, (ax - ring, ay - ring), _shape_mask(d["shape"], size + 2 * ring))
    bg.paste(av, (ax, ay), _shape_mask(d["shape"], size))

    tx = ax + size + 50
    draw.text((tx, 55), "LEVEL UP!", font=_font(d["font"], 26), fill=accent)
    name = str(username or "")[:40]
    if d["font"] != "classic" and name.isascii() and name.isprintable():
        draw.text((tx, 90), name, font=_font(d["font"], 44), fill=(255, 255, 255))
    else:
        lc.draw_text_fallback(draw, (tx, 90), name, 44, (255, 255, 255))
    draw.text((tx, 148), f"Level {int(new_level)}", font=_font(d["font"], 34), fill=(255, 255, 255))

    bx, by, bw, bh = tx, 205, lc.CARD_WIDTH - tx - 60, 26
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh // 2, fill=(60, 63, 68))
    frac = max(0.0, min(1.0, cur_xp / need_xp)) if need_xp > 0 else 1.0
    fw = max(bh, int(bw * frac)) if frac > 0 else 0
    if fw:
        draw.rounded_rectangle((bx, by, bx + fw, by + bh), radius=bh // 2, fill=accent)
    draw.text((bx, by + bh + 8), f"{int(cur_xp)}/{int(need_xp)} XP", font=_font(d["font"], 20), fill=(200, 200, 200))
    if d["bio"]:
        lc.draw_text_fallback(draw, (bx, by + bh + 40), d["bio"], 18, (225, 225, 225))

    out = io.BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()


def preview_data_url(design: dict) -> str:
    png = render_custom_level_card(placeholder_avatar(design.get("accent", "#57F287")), "Your name", 7, 40, 100, design)
    return "data:image/png;base64," + base64.b64encode(png).decode()


async def preview_data_url_async(design: dict) -> str:
    return await asyncio.to_thread(preview_data_url, design)


async def design_for_user(db, uid, now=None) -> Optional[dict]:
    """The design the bot should draw for this member, or None (no plan, plan expired, nothing saved)."""
    rows = await db.entitlements_list(str(uid))
    if not ent.has_access(rows, ("card_plan",), now):
        return None
    raw = await db.user_card_get(str(uid))
    if not raw:
        return None
    import json
    try:
        d, _ = validate(json.loads(raw))
    except Exception:
        return None
    return d
