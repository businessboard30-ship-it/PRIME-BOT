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
# Per-element switches (all on by default). The avatar and the logo are not switchable. Old saved designs have no
# such keys, so they validate to "everything shown".
SHOW_LABELS = {"show_title": "Heading (LEVEL UP! / RANK)", "show_name": "Name", "show_level": "Level line",
               "show_bar": "XP bar", "show_xp": "XP numbers", "show_bio": "Bio"}
SHOW_KEYS = tuple(SHOW_LABELS)
DEFAULT_DESIGN = {"background": "slate", "accent": "#57F287", "font": "classic", "shape": "circle", "bio": "",
                  "custom_bg": False, "logo": False, **{k: True for k in SHOW_KEYS}}


def options() -> dict:
    return {"backgrounds": {k: v[2] for k, v in BACKGROUNDS.items()}, "fonts": dict(FONT_LABELS),
            "shapes": dict(SHAPES), "bio_max": BIO_MAX, "show": dict(SHOW_LABELS)}


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
    for key in ("custom_bg", "logo") + SHOW_KEYS:
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


# ── layout ───────────────────────────────────────────────────────────────────
# The card is as big as the uploaded background (no background: 900x300). Layout rules, one set for every shape:
#   * S = min(W / 900, H / 300): the 900x300 reference layout is scaled by the smaller ratio, so it always fits.
#   * That 900S x 300S "band" is centred on the image (horizontally on wide images, vertically on tall/square ones).
#     Inside the band: avatar on the left, text block to its right, logo box on the right edge.
#   * The text block is centred vertically on the image, and when elements are hidden the rest re-flows (no gaps).
#   * The dark text panel sits behind the text zone, inside the band. The accent stripe runs the full height at x=0.
REF_W, REF_H = lc.CARD_WIDTH, lc.CARD_HEIGHT
# (switch key, slot height, gap after it) in reference pixels. Gap before the bar is a little larger.
_SLOTS = (("show_title", 30, 8), ("show_name", 50, 8), ("show_level", 40, 12), ("show_bar", 26, 8),
          ("show_xp", 20, 8), ("show_bio", 18, 0))
PREVIEW_BOX = (1200, 1200)             # the designer preview and the website preview are drawn no bigger than this


def layout_scale(w: int, h: int) -> float:
    return min(w / REF_W, h / REF_H)


def _band(w: int, h: int) -> tuple:
    """-> (S, ox, oy): scale and the top-left of the 900Sx300S band."""
    S = layout_scale(w, h)
    return S, (w - REF_W * S) / 2, (h - REF_H * S) / 2


def _darken_text_zone(bg: Image.Image, S: float, ox: float, oy: float) -> Image.Image:
    """Uploaded backgrounds get a dark panel behind the text so white text stays readable."""
    from PIL import ImageStat
    zx0, _zy0, zx1, _zy1 = card_assets_zone()
    x0, x1 = round(ox + zx0 * S), round(ox + zx1 * S)
    y0, y1 = round(oy + 14 * S), round(oy + (REF_H - 14) * S)
    mean = ImageStat.Stat(bg.crop((x0, y0, x1, y1)).convert("L")).mean[0]
    alpha = int(255 * min(0.85, 0.40 + max(0.0, mean - 90) / 255))
    layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((x0, y0, x1, y1), radius=round(18 * S), fill=(0, 0, 0, alpha))
    return Image.alpha_composite(bg.convert("RGBA"), layer).convert("RGB")


def card_assets_zone():
    from modules import card_assets
    return card_assets.TEXT_ZONE


def card_size(bg_bytes: Optional[bytes]) -> tuple:
    """(width, height) of the card for this background: the image's own size, or 900x300 with none."""
    if not bg_bytes:
        return REF_W, REF_H
    try:
        return Image.open(io.BytesIO(bg_bytes)).size
    except Exception:
        return REF_W, REF_H


def _open_background(data: bytes, fit_box: Optional[tuple]) -> Image.Image:
    """The uploaded background as RGB, downscaled to fit `fit_box` (previews only) without changing its shape.
    A JPEG is decoded at reduced size straight away, so a big photo is cheap to preview."""
    im = Image.open(io.BytesIO(data))
    if fit_box and (im.width > fit_box[0] or im.height > fit_box[1]):
        ratio = min(fit_box[0] / im.width, fit_box[1] / im.height)
        size = (max(1, round(im.width * ratio)), max(1, round(im.height * ratio)))
        if im.format == "JPEG":
            im.draft("RGB", size)
        return im.convert("RGB").resize(size, Image.LANCZOS)
    return im.convert("RGB")


def render_custom_level_card(avatar_bytes: bytes, username: str, new_level: int, cur_xp: int, need_xp: int,
                             design: dict, bg_png: Optional[bytes] = None, logo_png: Optional[bytes] = None,
                             title: str = "LEVEL UP!", rank: Optional[int] = None,
                             fit_box: Optional[tuple] = None) -> bytes:
    """Draws the member's card as PNG bytes. The canvas is the uploaded background's size (any shape, 900x300 with no
    background) and everything is laid out relative to it, see the layout rules above. `fit_box` (previews) draws a
    smaller copy of the same card. `title`/`rank` let /rank and My Rank reuse the same card. Use render_for_send()
    for anything that goes to Discord."""
    d, _ = validate(design or {})
    d = d or dict(DEFAULT_DESIGN)
    top, bottom, _label = BACKGROUNDS[d["background"]]
    use_bg = bg_png if (bg_png and d.get("custom_bg")) else None
    bg = None
    if use_bg:
        try:
            bg = _open_background(use_bg, fit_box)
        except Exception:
            bg = None
    if bg is not None:
        W, H = bg.size
        S, ox, oy = _band(W, H)
        bg = _darken_text_zone(bg, S, ox, oy)
    else:
        W, H = REF_W, REF_H
        S, ox, oy = 1.0, 0.0, 0.0
        bg = _gradient(top, bottom, W, H)

    def k(v):
        return int(round(v * S))

    # logo: right edge of the band
    logo_x0 = None
    if logo_png and d.get("logo"):
        try:
            from modules import card_assets
            lg = Image.open(io.BytesIO(logo_png)).convert("RGBA")
            lx0, ly0, lx1, ly1 = card_assets.LOGO_BOX
            x0, x1 = round(ox + lx0 * S), round(ox + lx1 * S)
            y0, y1 = round(oy + ly0 * S), round(oy + ly1 * S)
            lg.thumbnail((x1 - x0, y1 - y0), Image.LANCZOS)
            bg.paste(lg, (x0 + (x1 - x0 - lg.width) // 2, y0 + (y1 - y0 - lg.height) // 2), lg)
            logo_x0 = x0
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
    ax, ay, ring = round(ox) + k(60), (H - size) // 2, k(6)
    ring_img = Image.new("RGBA", (size + 2 * ring, size + 2 * ring), accent + (255,))
    bg.paste(ring_img, (ax - ring, ay - ring), _shape_mask(d["shape"], size + 2 * ring))
    bg.paste(av, (ax, ay), _shape_mask(d["shape"], size))

    # text block: only the elements that are switched on (and have something to show), centred vertically
    tx = ax + size + k(50)
    head = (f"{title}  #{int(rank)}" if rank else title)
    name = str(username or "")[:40]
    on = [(key, h, gap) for key, h, gap in _SLOTS
          if d.get(key, True) and (key != "show_bio" or d["bio"])]
    total = sum(h + gap for _k, h, gap in on) - (on[-1][2] if on else 0)
    y = (H - k(total)) / 2
    bar_right = (logo_x0 - k(20)) if logo_x0 is not None else (round(ox) + k(REF_W - 60))
    font = d["font"]
    for key, h, gap in on:
        yy = int(round(y))
        if key == "show_title":
            draw.text((tx, yy), head, font=_font(font, k(26)), fill=accent)
        elif key == "show_name":
            if font != "classic" and name.isascii() and name.isprintable():
                draw.text((tx, yy), name, font=_font(font, k(44)), fill=(255, 255, 255))
            else:
                lc.draw_text_fallback(draw, (tx, yy), name, k(44), (255, 255, 255))
        elif key == "show_level":
            draw.text((tx, yy), f"Level {int(new_level)}", font=_font(font, k(34)), fill=(255, 255, 255))
        elif key == "show_bar":
            bw, bh = max(k(40), bar_right - tx), k(h)
            draw.rounded_rectangle((tx, yy, tx + bw, yy + bh), radius=bh // 2, fill=(60, 63, 68))
            frac = max(0.0, min(1.0, cur_xp / need_xp)) if need_xp > 0 else 1.0
            fw = max(bh, int(bw * frac)) if frac > 0 else 0
            if fw:
                draw.rounded_rectangle((tx, yy, tx + fw, yy + bh), radius=bh // 2, fill=accent)
        elif key == "show_xp":
            draw.text((tx, yy), f"{int(cur_xp)}/{int(need_xp)} XP", font=_font(font, k(20)), fill=(200, 200, 200))
        elif key == "show_bio":
            lc.draw_text_fallback(draw, (tx, yy), d["bio"], k(18), (225, 225, 225))
        y += k(h + gap)

    out = io.BytesIO()
    bg.save(out, format="PNG")
    return out.getvalue()


# ── sending ──────────────────────────────────────────────────────────────────
DISCORD_MAX_BYTES = 9 * 1024 * 1024            # Discord's bot upload limit is about 10 MB; stay under it with margin


def fit_for_discord(png: bytes, limit: int = DISCORD_MAX_BYTES) -> bytes:
    """PNG bytes as they are when small enough; otherwise JPEG q92 of the same card; otherwise a smaller JPEG copy.
    Only the COPY being sent is changed, never the stored background. Check file_ext() for the file name."""
    if len(png) <= limit:
        return png
    im = Image.open(io.BytesIO(png)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92, optimize=True)
    if buf.tell() <= limit:
        return buf.getvalue()
    scale = 1.0
    for _ in range(6):
        scale *= 0.8
        small = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=85, optimize=True)
        if buf.tell() <= limit:
            break
    return buf.getvalue()


def file_ext(data: bytes) -> str:
    return "jpg" if data[:3] == b"\xff\xd8\xff" else "png"


def file_name(base: str, data: bytes) -> str:
    return f"{base}.{file_ext(data)}"


def render_for_send(*args, **kwargs) -> bytes:
    """render_custom_level_card + the Discord size fallback (a big PNG card becomes a JPEG copy). Synchronous."""
    return fit_for_discord(render_custom_level_card(*args, **kwargs))


# At most two cards are drawn at the same time: a big background is ~100 MB while it is decoded and drawn.
_RENDER_SEM: Optional[asyncio.Semaphore] = None


async def render_async(*args, send: bool = True, **kwargs) -> bytes:
    """Draw a card off the event loop, two at a time. send=True also applies the Discord size fallback."""
    global _RENDER_SEM
    if _RENDER_SEM is None:
        _RENDER_SEM = asyncio.Semaphore(2)
    fn = render_for_send if send else render_custom_level_card
    async with _RENDER_SEM:
        return await asyncio.to_thread(fn, *args, **kwargs)


async def render_member_card(db, member, level: int, cur_xp: int, need_xp: int, rank: Optional[int] = None,
                             title: str = "RANK", clone_id=None) -> Optional[bytes]:
    """The member's own saved card for /rank and My Rank, or None when they have no active Level Pass / no design
    (the caller then shows the normal plain card). Never raises."""
    try:
        card = await card_for_user(db, member.id, guild_id=getattr(getattr(member, "guild", None), "id", None),
                                   clone_id=clone_id)
        if card is None:
            return None
        try:
            avatar = await member.display_avatar.replace(size=256, format="png").read()
        except Exception:
            avatar = placeholder_avatar(card[0].get("accent", "#57F287"))
        return await render_async(avatar, getattr(member, "display_name", ""), level, cur_xp, need_xp, *card,
                                  title=title, rank=rank)
    except Exception:
        logger.warning("[level-card] couldn't draw the member's card", exc_info=True)
        return None


def preview_data_url(design: dict, bg_png: Optional[bytes] = None, logo_png: Optional[bytes] = None) -> str:
    png = render_custom_level_card(placeholder_avatar(design.get("accent", "#57F287")), "Your name", 7, 40, 100, design,
                                   bg_png, logo_png, fit_box=PREVIEW_BOX)
    return "data:image/png;base64," + base64.b64encode(png).decode()


async def preview_data_url_async(design: dict, bg_png: Optional[bytes] = None, logo_png: Optional[bytes] = None) -> str:
    global _RENDER_SEM
    if _RENDER_SEM is None:
        _RENDER_SEM = asyncio.Semaphore(2)
    async with _RENDER_SEM:
        return await asyncio.to_thread(preview_data_url, design, bg_png, logo_png)


async def guild_card_access(db, guild_id, clone_id=None) -> bool:
    """True while this server has Premium (grace included) or an active server XP boost: members of such a server use
    their own custom level-up card there with no Level Pass. Any lookup problem means False. Never raises."""
    if not guild_id:
        return False
    try:
        if await db.is_guild_premium_active(guild_id, clone_id):
            return True
        return bool(await db.get_active_guild_xp_boost(guild_id, clone_id=clone_id))
    except Exception:
        logger.warning("[level-card] couldn't check this server's card access", exc_info=True)
        return False


async def design_for_user(db, uid, now=None, guild_id=None, clone_id=None) -> Optional[dict]:
    """The design the bot should draw for this member, or None (no plan, plan expired, nothing saved). With a
    guild, a Premium / boosted server counts as access too (see guild_card_access)."""
    rows = await db.entitlements_list(str(uid))
    if not ent.has_access(rows, ("card_plan",), now) and not await guild_card_access(db, guild_id, clone_id):
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


async def card_for_user(db, uid, now=None, guild_id=None, clone_id=None) -> Optional[tuple]:
    """(design, background_png|None, logo_png|None) for the bot, or None. Only APPROVED assets are ever returned."""
    d = await design_for_user(db, uid, now, guild_id, clone_id)
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
