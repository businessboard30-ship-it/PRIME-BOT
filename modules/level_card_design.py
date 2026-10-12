# path: modules/level_card_design.py
"""Custom level-up card (card plan). Validation, preview and render for a member's saved design.

Security: a design is data only. Colours must match #RRGGBB, fonts/shapes/backgrounds come from fixed
allowlists (no uploads in v1), the bio is plain text capped at BIO_MAX. The browser never decides access:
design_for_user() returns a design only while the card_plan entitlement is effective (expiry falls back to
the default card, the saved design is kept and comes back on renewal).
"""
import asyncio
import base64
import functools
import io
import logging
import os
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from modules import entitlements as ent
from modules import level_card as lc

logger = logging.getLogger(__name__)

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
DEFAULT_DESIGN = {"background": "slate", "accent": "#57F287", "font": "classic", "shape": "circle", "bio": "",
                  "custom_bg": False, "logo": False}


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
    for key in ("custom_bg", "logo"):
        if key in raw:
            if not isinstance(raw[key], bool):
                return None, f"{key} must be true or false."
            d[key] = raw[key]
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


@functools.lru_cache(maxsize=48)
def _gradient_raw(top: str, bottom: str, w: int, h: int) -> bytes:
    """The vertical gradient as raw RGB bytes, built once per (colours, size) and reused for every card after that.
    One 1-pixel-wide column is computed and stretched, instead of drawing one line per row on every level-up."""
    a, b = lc._hex_to_rgb(top), lc._hex_to_rgb(bottom)
    col = Image.new("RGB", (1, h))
    col.putdata([tuple(int(a[i] + (b[i] - a[i]) * (y / max(1, h - 1))) for i in range(3)) for y in range(h)])
    return col.resize((w, h)).tobytes()


def _gradient(top: str, bottom: str, w: int = lc.CARD_WIDTH, h: int = lc.CARD_HEIGHT) -> Image.Image:
    return Image.frombytes("RGB", (w, h), _gradient_raw(top, bottom, w, h))


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


def _darken_text_zone(bg: Image.Image, scale: float = 1.0) -> Image.Image:
    """Uploaded backgrounds get a dark panel behind the text so white text stays readable."""
    from PIL import ImageStat
    x0, _y0, x1, _y1 = card_assets_zone()
    x0, x1, h = round(x0 * scale), round(x1 * scale), bg.height
    mean = ImageStat.Stat(bg.crop((x0, 0, x1, h)).convert("L")).mean[0]
    alpha = int(255 * min(0.85, 0.40 + max(0.0, mean - 90) / 255))
    layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((x0, round(14 * scale), x1, h - round(14 * scale)), radius=round(18 * scale),
                                            fill=(0, 0, 0, alpha))
    return Image.alpha_composite(bg.convert("RGBA"), layer).convert("RGB")


def card_assets_zone():
    from modules import card_assets
    return card_assets.TEXT_ZONE


def card_scale(bg_png: Optional[bytes]) -> float:
    """1.0 normally. A large uploaded background makes a larger card (up to 2x, 1800x600)."""
    if not bg_png:
        return 1.0
    try:
        w = Image.open(io.BytesIO(bg_png)).size[0]
    except Exception:
        return 1.0
    return max(1.0, min(2.0, w / lc.CARD_WIDTH))


def render_custom_level_card(avatar_bytes: bytes, username: str, new_level: int, cur_xp: int, need_xp: int,
                             design: dict, bg_png: Optional[bytes] = None, logo_png: Optional[bytes] = None,
                             title: str = "LEVEL UP!", rank: Optional[int] = None) -> bytes:
    """Draws the member's card. Everything is laid out at 900x300 and multiplied by `scale`, so a big uploaded
    background gives a bigger, sharper card. `title`/`rank` let /rank and My Rank reuse the same card."""
    d, _ = validate(design or {})
    d = d or dict(DEFAULT_DESIGN)
    top, bottom, _label = BACKGROUNDS[d["background"]]
    use_bg = bg_png if (bg_png and d.get("custom_bg")) else None
    S = card_scale(use_bg)
    W, H = round(lc.CARD_WIDTH * S), round(lc.CARD_HEIGHT * S)

    def k(v):
        return int(round(v * S))

    bg = None
    if use_bg:
        try:
            bg = _darken_text_zone(Image.open(io.BytesIO(use_bg)).convert("RGB").resize((W, H)), S)
        except Exception:
            bg = None
    if bg is None:
        bg = _gradient(top, bottom, W, H)
    if logo_png and d.get("logo"):
        try:
            from modules import card_assets
            lg = Image.open(io.BytesIO(logo_png)).convert("RGBA")
            x0, y0, x1, y1 = (k(v) for v in card_assets.LOGO_BOX)
            lg.thumbnail((x1 - x0, y1 - y0), Image.LANCZOS)
            bg.paste(lg, (x0 + (x1 - x0 - lg.width) // 2, y0 + (y1 - y0 - lg.height) // 2), lg)
        except Exception:
            pass
    draw = ImageDraw.Draw(bg)
    accent = lc._hex_to_rgb(d["accent"])
    draw.rectangle([(0, 0), (k(14), H)], fill=accent)

    size = k(lc.AVATAR_SIZE)
    try:
        av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((size, size))
    except Exception:
        av = Image.new("RGBA", (size, size), accent)
    ax, ay, ring = k(60), (H - size) // 2, k(6)
    ring_img = Image.new("RGBA", (size + 2 * ring, size + 2 * ring), accent + (255,))
    bg.paste(ring_img, (ax - ring, ay - ring), _shape_mask(d["shape"], size + 2 * ring))
    bg.paste(av, (ax, ay), _shape_mask(d["shape"], size))

    tx = ax + size + k(50)
    head = (f"{title}  #{int(rank)}" if rank else title)
    draw.text((tx, k(55)), head, font=_font(d["font"], k(26)), fill=accent)
    name = str(username or "")[:40]
    if d["font"] != "classic" and name.isascii() and name.isprintable():
        draw.text((tx, k(90)), name, font=_font(d["font"], k(44)), fill=(255, 255, 255))
    else:
        lc.draw_text_fallback(draw, (tx, k(90)), name, k(44), (255, 255, 255))
    draw.text((tx, k(148)), f"Level {int(new_level)}", font=_font(d["font"], k(34)), fill=(255, 255, 255))

    bx, by, bw, bh = tx, k(205), W - tx - k(60), k(26)
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=bh // 2, fill=(60, 63, 68))
    frac = max(0.0, min(1.0, cur_xp / need_xp)) if need_xp > 0 else 1.0
    fw = max(bh, int(bw * frac)) if frac > 0 else 0
    if fw:
        draw.rounded_rectangle((bx, by, bx + fw, by + bh), radius=bh // 2, fill=accent)
    draw.text((bx, by + bh + k(8)), f"{int(cur_xp)}/{int(need_xp)} XP", font=_font(d["font"], k(20)), fill=(200, 200, 200))
    if d["bio"]:
        lc.draw_text_fallback(draw, (bx, by + bh + k(40)), d["bio"], k(18), (225, 225, 225))

    out = io.BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()


async def render_member_card(db, member, level: int, cur_xp: int, need_xp: int, rank: Optional[int] = None,
                             title: str = "RANK") -> Optional[bytes]:
    """The member's own saved card for /rank and My Rank, or None when they have no active Level Pass / no design
    (the caller then shows the normal plain card). Never raises."""
    try:
        card = await card_for_user(db, member.id)
        if card is None:
            return None
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except Exception:
            avatar = placeholder_avatar(card[0].get("accent", "#57F287"))
        return await asyncio.to_thread(render_custom_level_card, avatar, getattr(member, "display_name", ""), level,
                                       cur_xp, need_xp, *card, title=title, rank=rank)
    except Exception:
        logger.warning("[level-card] couldn't draw the member's card", exc_info=True)
        return None


def preview_data_url(design: dict, bg_png: Optional[bytes] = None, logo_png: Optional[bytes] = None) -> str:
    png = render_custom_level_card(placeholder_avatar(design.get("accent", "#57F287")), "Your name", 7, 40, 100, design,
                                   bg_png, logo_png)
    return "data:image/png;base64," + base64.b64encode(png).decode()


async def preview_data_url_async(design: dict, bg_png: Optional[bytes] = None, logo_png: Optional[bytes] = None) -> str:
    return await asyncio.to_thread(preview_data_url, design, bg_png, logo_png)


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


async def card_for_user(db, uid, now=None) -> Optional[tuple]:
    """(design, background_png|None, logo_png|None) for the bot, or None. Only APPROVED assets are ever returned."""
    d = await design_for_user(db, uid, now)
    if d is None:
        return None
    bg = logo = None
    for kind, flag in (("background", "custom_bg"), ("logo", "logo")):
        if d.get(flag):
            a = await db.card_asset_get(str(uid), kind)
            if a and a.get("status") == "approved":
                if kind == "background":
                    bg = bytes(a["data"])
                else:
                    logo = bytes(a["data"])
    return d, bg, logo
