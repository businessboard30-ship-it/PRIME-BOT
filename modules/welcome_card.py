# path: modules/welcome_card.py

"""
Generates a welcome-card image (avatar + name + member count + optional
sticker) for discord_bot/cogs/welcome.py — ProBot-style welcome images.

By default, render_welcome_card() draws onto the designed background image
at assets/images/welcome_bg_wolf.png (TEMPLATE_BACKGROUND_PATH), compositing
the avatar into its member-card slot and redrawing the member count/username
live on every call. Pass guild_name to also swap the header/subtitle text to
a real server name. If that background asset is missing, it silently falls
back to the original flat-color card below (use_template=False forces this
path even when the asset exists).

Two flat-card output modes:
- Static PNG (original behavior): background + avatar + text only, or with
  a single non-animated sticker frame pasted in.
- Animated GIF: the same background/avatar/text drawn once as a base
  frame, then re-composited once per sticker frame with that frame pasted
  into the sticker box on the right — so the card sits still and only the
  sticker "dances" in that spot, matching how ProBot-style cards with a
  looping decoration behave.

Which mode is used is decided by the caller (welcome.py), based on the
guild's configured card_style ('static' or 'gif') and whether a usable
sticker was actually downloaded.

No custom font is bundled with this repo, so this falls back to Pillow's
built-in default font, which is legible but plain. Drop a .ttf into
bot/assets/fonts/ and point FONT_PATH at it for a nicer result — kept as a
simple module-level constant rather than a config setting since it's a
deploy-time asset choice, not something a guild admin configures per-guild.
"""

import io
import logging
import os
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageSequence

logger = logging.getLogger(__name__)

CARD_WIDTH = 900
CARD_HEIGHT = 300
AVATAR_SIZE = 180
FONT_PATH: Optional[str] = None  # e.g. "assets/fonts/Inter-Bold.ttf"

# ---------------------------------------------------------------------------
# Template card (default): a designed background image (assets/images/) with
# the avatar + live member-count/username + server name composited on top at
# render time. This is what render_welcome_card() uses whenever
# TEMPLATE_BACKGROUND_PATH points at a real file — which it does out of the
# box. Falls back to the plain flat-color card further down this file if the
# background asset is missing, so a broken/removed asset never crashes a
# join event.
# ---------------------------------------------------------------------------
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_BACKGROUND_PATH: Optional[str] = os.path.join(
    _MODULE_DIR, "..", "assets", "images", "welcome_bg_wolf.png"
)

# Premium card themes ("card pack") — same box layout as the free wolf
# template above, just a different background image swapped in at render
# time. A guild only sees these as selectable in /welcome theme once it has
# purchased the card pack (see discord_bot/cogs/welcome.py's `theme` command
# and database.py's card_pack_unlocked column) — this module itself doesn't
# enforce that gate, it just renders whatever theme name it's given, falling
# back to the free 'wolf' theme for an unrecognized name so a bad/removed
# theme value can never crash a join.
THEME_BACKGROUNDS: dict[str, str] = {
    "wolf": TEMPLATE_BACKGROUND_PATH,
    "reaper": os.path.join(_MODULE_DIR, "..", "assets", "images", "welcome_bg_reaper.png"),
    "shadow": os.path.join(_MODULE_DIR, "..", "assets", "images", "welcome_bg_shadow.png"),
    "sorcerer": os.path.join(_MODULE_DIR, "..", "assets", "images", "welcome_bg_sorcerer.png"),
    "spider": os.path.join(_MODULE_DIR, "..", "welcome_bg_spider.png"),
    "spider_pro": os.path.join(_MODULE_DIR, "..", "welcome_bg_spider_pro.png"),
}
# Themes that require the card pack purchase — everything except the
# original free 'wolf' template.
PREMIUM_THEMES: frozenset[str] = frozenset(k for k in THEME_BACKGROUNDS if k != "wolf")

TEMPLATE_WIDTH = 1536
TEMPLATE_HEIGHT = 1024

# Circular avatar slot (top-left "member card" box in the artwork) —
# per-theme since spider's hand-drawn ring sits at a different size/spot
# than the shared box the original 3 themes happen to share. Falls back to
# TEMPLATE_AVATAR_BOX (the historical single constant) for any theme not
# listed here, so wolf/reaper/shadow/sorcerer's behavior is unchanged.
TEMPLATE_AVATAR_BOX = (100, 398, 248, 546)
THEME_AVATAR_BOX = {
    "wolf": TEMPLATE_AVATAR_BOX,
    "reaper": TEMPLATE_AVATAR_BOX,
    "shadow": TEMPLATE_AVATAR_BOX,
    "sorcerer": TEMPLATE_AVATAR_BOX,
    "spider": (85, 399, 268, 581),
    "spider_pro": (713, 431, 954, 671),
}

# Text block cleared and redrawn each render: "MEMBER #N" + display name.
# Per-theme override since the baked-in text sits a bit higher on the 3
# premium artworks than on the wolf template — falls back to 'wolf' the
# same way the header/subtitle boxes above do.
THEME_MEMBER_TEXT_BOX = {
    "wolf": (270, 435, 690, 545),
    "reaper": (270, 408, 690, 545),
    "shadow": (270, 408, 690, 545),
    "sorcerer": (270, 408, 690, 545),
    "spider": (301, 414, 782, 544),
    # No left avatar cutout in this panel — the avatar sits out in the
    # artwork instead (see THEME_AVATAR_BOX["spider_pro"]) — so the text
    # gets the box's full width rather than starting after a circle.
    "spider_pro": (90, 414, 695, 544),
}
TEMPLATE_MEMBER_TEXT_BOX = THEME_MEMBER_TEXT_BOX["wolf"]
TEMPLATE_MEMBER_TEXT_BG = (5, 5, 5)  # sampled from the artwork's near-black panel

# Optional second line of static flavor text under the member-info box —
# only "spider" has room for it (its clean template left that whole lower
# panel blank; the other 3 themes either have no equivalent space or
# already bake their own text in). Same two lines every render — not
# per-member, not per-guild, just a fixed friendly line like the original
# mockup had baked in.
THEME_GREETING_BOX = {
    "spider": (100, 592, 762, 729),
    "spider_pro": (90, 564, 695, 769),
}
TEMPLATE_GREETING_LINES = ("Glad to have you here!", "We hope you have an amazing time with us.")

# Header label ("BOT ARCHIVES") and the "TO <server>!" subtitle line under
# the WELCOME wordmark — both optional: only redrawn when guild_name is
# passed in, otherwise the artwork's own baked-in text shows through.
# Header label ("BOT ARCHIVES") and "TO <server>!" subtitle clear/redraw
# boxes — tuned per theme, since each background image places its own
# baked-in title text at a slightly different position. Falls back to the
# 'wolf' entry for any theme not listed here (e.g. a future addition) so a
# missing override never crashes a render, just reuses wolf's box.
THEME_HEADER_LABEL_BOX = {
    "wolf": (72, 80, 400, 112),
    "reaper": (85, 68, 420, 110),
    "shadow": (75, 50, 420, 95),
    "sorcerer": (75, 70, 420, 115),
    "spider": (65, 55, 411, 95),
    "spider_pro": (65, 55, 411, 95),
}
THEME_SUBTITLE_BOX = {
    "wolf": (170, 325, 900, 368),
    "reaper": (150, 295, 900, 345),
    "shadow": (120, 290, 900, 345),
    "sorcerer": (80, 280, 900, 340),
    "spider": (100, 309, 701, 349),
    "spider_pro": (100, 309, 701, 349),
}
TEMPLATE_HEADER_LABEL_BOX = THEME_HEADER_LABEL_BOX["wolf"]
TEMPLATE_SUBTITLE_BOX = THEME_SUBTITLE_BOX["wolf"]
TEMPLATE_TEXT_BG = (5, 5, 5)

# "WELCOME" wordmark box — optional, per-theme. wolf/reaper/shadow/
# sorcerer all have this baked permanently into their artwork (it doesn't
# vary per-server, so there was never a reason to draw it in code). The
# spider artworks' clean templates both have that whole area intentionally
# left blank, so those are the ones that need it actually drawn here.
THEME_TITLE_BOX = {
    "spider": (65, 110, 902, 289),
    "spider_pro": (65, 110, 902, 289),
}

# Sticker box: mirrors the avatar on the opposite side of the card (the
# empty space to the right of the name/subtitle text).
STICKER_SIZE = 190
STICKER_X = CARD_WIDTH - STICKER_SIZE - 55
STICKER_Y = (CARD_HEIGHT - STICKER_SIZE) // 2
# Cap on frames pulled from the source sticker GIF — long/high-fps source
# GIFs would otherwise make the rendered card huge and slow to build/send
# on every join. 40 frames is plenty for a short looping "dance".
MAX_STICKER_FRAMES = 40


def _load_font(size: int):
    if FONT_PATH:
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            pass
    try:
        # Pillow >=10 default font supports a size arg; older versions don't.
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (43, 45, 49)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# Supported avatar frame shapes. Each maps to a function that draws a
# filled shape into a fresh L-mode mask (for cropping the avatar) — the
# same shape is also used, oversized by ring_pad, as the accent-colored
# ring behind it, so mask and ring always agree regardless of which shape
# is picked. 'circle' is the original/default behavior.
def _mask_circle(draw: ImageDraw.ImageDraw, box: tuple, fill: int):
    draw.ellipse(box, fill=fill)


def _mask_rounded_square(draw: ImageDraw.ImageDraw, box: tuple, fill: int):
    x0, y0, x1, y1 = box
    radius = int((x1 - x0) * 0.22)
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _mask_square(draw: ImageDraw.ImageDraw, box: tuple, fill: int):
    draw.rectangle(box, fill=fill)


def _mask_hexagon(draw: ImageDraw.ImageDraw, box: tuple, fill: int):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    points = [
        (x0 + w * 0.5, y0), (x1, y0 + h * 0.25), (x1, y0 + h * 0.75),
        (x0 + w * 0.5, y1), (x0, y0 + h * 0.75), (x0, y0 + h * 0.25),
    ]
    draw.polygon(points, fill=fill)


def _mask_diamond(draw: ImageDraw.ImageDraw, box: tuple, fill: int):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    points = [(x0 + w * 0.5, y0), (x1, y0 + h * 0.5), (x0 + w * 0.5, y1), (x0, y0 + h * 0.5)]
    draw.polygon(points, fill=fill)


AVATAR_SHAPES = {
    "circle": _mask_circle,
    "rounded_square": _mask_rounded_square,
    "square": _mask_square,
    "hexagon": _mask_hexagon,
    "diamond": _mask_diamond,
}
DEFAULT_AVATAR_SHAPE = "circle"


def _draw_base(username: str, subtitle: str, avatar_bytes: bytes,
                background_color: str, accent_color: str,
                avatar_shape: str = DEFAULT_AVATAR_SHAPE) -> Image.Image:
    """Renders the static part of the card (background, avatar, text) as
    an RGBA image. Shared by both the static-PNG path and every frame of
    the animated-GIF path, so the two modes stay visually identical apart
    from whether the sticker spot moves."""
    bg = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), _hex_to_rgb(background_color) + (255,))
    draw = ImageDraw.Draw(bg)
    accent_rgb = _hex_to_rgb(accent_color)
    shape_fn = AVATAR_SHAPES.get(avatar_shape, _mask_circle)

    # Accent stripe down the left edge
    draw.rectangle([(0, 0), (14, CARD_HEIGHT)], fill=accent_rgb)

    # Avatar, cropped to the configured shape, with a matching accent-colored ring
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank frame instead: {e}")
        avatar = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), accent_rgb)

    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    shape_fn(ImageDraw.Draw(mask), (0, 0, AVATAR_SIZE, AVATAR_SIZE), 255)
    avatar_x, avatar_y = 60, (CARD_HEIGHT - AVATAR_SIZE) // 2
    ring_pad = 6
    ring_box = (avatar_x - ring_pad, avatar_y - ring_pad, avatar_x + AVATAR_SIZE + ring_pad, avatar_y + AVATAR_SIZE + ring_pad)
    shape_fn(draw, ring_box, accent_rgb)
    bg.paste(avatar, (avatar_x, avatar_y), mask)

    text_x = avatar_x + AVATAR_SIZE + 50
    uname_size, uname_text = _fit_text_fallback(draw, username, CARD_WIDTH - text_x - 30, max_font_size=48, min_font_size=22)
    _draw_text_fb(draw, (text_x, 95), uname_text, uname_size, (255, 255, 255))
    draw.text((text_x, 155), subtitle, font=_load_font(28), fill=accent_rgb)

    return bg


def _fit_sticker_frame(frame: Image.Image) -> Image.Image:
    """Resizes one sticker frame to fit inside STICKER_SIZE x STICKER_SIZE
    keeping aspect ratio, on a transparent square canvas so it centers
    cleanly in the sticker box regardless of the source GIF's aspect."""
    frame = frame.convert("RGBA")
    frame.thumbnail((STICKER_SIZE, STICKER_SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0))
    off_x = (STICKER_SIZE - frame.width) // 2
    off_y = (STICKER_SIZE - frame.height) // 2
    canvas.paste(frame, (off_x, off_y), frame)
    return canvas


def _fit_text_to_box(draw: ImageDraw.ImageDraw, text: str, box_width: int,
                      max_font_size: int, min_font_size: int = 14) -> tuple:
    """Shrinks font size until `text` fits within box_width; if it still
    doesn't fit at min_font_size, truncates with an ellipsis instead of
    letting it overflow the clear-box and spill onto the artwork.

    Returns (font, text_to_draw). Used for the three welcome-card fields
    that previously overflowed on long server names / usernames: the
    header label, the "TO <server>!" subtitle, and the username line."""
    size = max_font_size
    font = _load_font(size)
    while size > min_font_size and draw.textlength(text, font=font) > box_width:
        size -= 2
        font = _load_font(size)

    if draw.textlength(text, font=font) <= box_width:
        return font, text

    # Still too wide at the smallest allowed size — truncate with an
    # ellipsis rather than let it run over the artwork.
    ellipsis = "..."
    truncated = text
    while truncated and draw.textlength(truncated + ellipsis, font=font) > box_width:
        truncated = truncated[:-1]
    return font, (truncated + ellipsis) if truncated else text


def _fit_text_fallback(draw: ImageDraw.ImageDraw, text: str, box_width: int,
                        max_font_size: int, min_font_size: int = 14) -> tuple:
    """Same shrink-to-fit / ellipsis-truncate behavior as _fit_text_to_box,
    but measures with level_card's per-character font fallback chain, and
    returns (font_size, text_to_draw) for use with _draw_text_fb(). Use this
    for user-controlled strings (usernames, server names, custom headings):
    with FONT_PATH unset, _load_font() falls back to Pillow's built-in
    bitmap font, which only covers basic Latin — any \"fancy\" Discord name
    (mathematical-alphanumeric, fullwidth, small-caps, symbols, CJK...) came
    out as a row of tofu boxes on the welcome card."""
    from modules.level_card import _textlength_fallback
    size = max_font_size
    while size > min_font_size and _textlength_fallback(draw, text, size) > box_width:
        size -= 2
    if _textlength_fallback(draw, text, size) <= box_width:
        return size, text
    ellipsis = "..."
    truncated = text
    while truncated and _textlength_fallback(draw, truncated + ellipsis, size) > box_width:
        truncated = truncated[:-1]
    return size, (truncated + ellipsis) if truncated else text


def _draw_text_fb(draw: ImageDraw.ImageDraw, xy: tuple, text: str, size: int, fill, **kwargs):
    """draw.text() for user-controlled strings, via level_card's fallback chain."""
    from modules.level_card import draw_text_fallback
    return draw_text_fallback(draw, xy, text, size, fill, **kwargs)


def _extract_member_number(subtitle: str) -> str:
    """Pulls the digits out of a 'Member #N' style subtitle so the template
    card can redraw just the number cleanly. Falls back to the raw subtitle
    text if it doesn't look like 'Member #N' (e.g. a fully custom string)."""
    match = re.search(r"(\d+)", subtitle)
    return f"MEMBER #{match.group(1)}" if match else subtitle.upper()


def _draw_template_card(username: str, subtitle: str, avatar_bytes: bytes,
                         guild_name: Optional[str] = None,
                         avatar_shape: str = DEFAULT_AVATAR_SHAPE,
                         theme: str = "wolf") -> Optional[Image.Image]:
    """Renders the designed-background welcome card (avatar + live member
    count/name composited over the theme's background image, with the
    server name optionally redrawn too). Returns None if the background
    asset can't be loaded, so the caller can fall back to the flat card.

    theme: key into THEME_BACKGROUNDS. Unknown values fall back to 'wolf'
    (the free default) rather than failing the render.

    avatar_shape: one of AVATAR_SHAPES's keys, same as the flat card. This
    is the one look-customization that IS supported in template mode (see
    render_welcome_card's use_template docstring) — colors/sticker/style
    aren't, since they'd clash with the fixed artwork."""
    background_path = THEME_BACKGROUNDS.get(theme, TEMPLATE_BACKGROUND_PATH)
    if not background_path or not os.path.isfile(background_path):
        return None

    try:
        bg = Image.open(background_path).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't load welcome card template background ({theme}), falling back to flat card: {e}")
        return None

    if bg.size != (TEMPLATE_WIDTH, TEMPLATE_HEIGHT):
        bg = bg.resize((TEMPLATE_WIDTH, TEMPLATE_HEIGHT))

    draw = ImageDraw.Draw(bg)

    # Avatar, cropped to a circle, into the member-card icon slot.
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank frame instead: {e}")
        avatar = Image.new("RGBA", (200, 200), (88, 101, 242, 255))

    ax0, ay0, ax1, ay1 = THEME_AVATAR_BOX.get(theme, TEMPLATE_AVATAR_BOX)
    avatar_size = (ax1 - ax0, ay1 - ay0)
    avatar = avatar.resize(avatar_size)
    mask = Image.new("L", avatar_size, 0)
    shape_fn = AVATAR_SHAPES.get(avatar_shape, _mask_circle)
    shape_fn(ImageDraw.Draw(mask), (0, 0, avatar_size[0], avatar_size[1]), 255)
    bg.paste(avatar, (ax0, ay0), mask)

    # Live "MEMBER #N" + display name, replacing the placeholder text baked
    # into the artwork.
    member_box = THEME_MEMBER_TEXT_BOX.get(theme, TEMPLATE_MEMBER_TEXT_BOX)
    draw.rectangle(member_box, fill=TEMPLATE_MEMBER_TEXT_BG)
    mx, my = member_box[0], member_box[1]
    member_box_width = member_box[2] - member_box[0] - 20  # minus left/right padding
    draw.text((mx + 10, my + 7), _extract_member_number(subtitle), font=_load_font(26), fill=(160, 160, 160))
    # Username: shrink-to-fit / truncate so long Discord usernames can't
    # spill out of the clear-box and over the character artwork.
    username_size, username_text = _fit_text_fallback(draw, username, member_box_width, max_font_size=56, min_font_size=22)
    _draw_text_fb(draw, (mx + 10, my + 45), username_text, username_size, (255, 255, 255))

    # Static two-line greeting under the member box, themes that have room
    # for it (see THEME_GREETING_BOX above).
    greeting_box = THEME_GREETING_BOX.get(theme)
    if greeting_box:
        gx0, gy0, gx1, gy1 = greeting_box
        greeting_width = gx1 - gx0
        line1_font, line1_text = _fit_text_to_box(draw, TEMPLATE_GREETING_LINES[0], greeting_width, max_font_size=32, min_font_size=18)
        draw.text((gx0, gy0), line1_text, font=line1_font, fill=(120, 190, 255))
        line2_font, line2_text = _fit_text_to_box(draw, TEMPLATE_GREETING_LINES[1], greeting_width, max_font_size=26, min_font_size=14)
        draw.text((gx0, gy0 + line1_font.size + 14), line2_text, font=line2_font, fill=(190, 195, 205))

    # "WELCOME" wordmark — only for themes listed in THEME_TITLE_BOX (see
    # its definition above); other themes have this baked into their own
    # artwork already and are left untouched.
    title_box = THEME_TITLE_BOX.get(theme)
    if title_box:
        tx0, ty0, tx1, ty1 = title_box
        title_font, title_text = _fit_text_to_box(draw, "WELCOME", tx1 - tx0, max_font_size=150, min_font_size=60)
        draw.text((tx0, ty0), title_text, font=title_font, fill=(225, 240, 255))

    # Server name, if the caller wants it swapped in (otherwise the
    # artwork's own baked-in header/subtitle text is left alone).
    if guild_name:
        header_box = THEME_HEADER_LABEL_BOX.get(theme, THEME_HEADER_LABEL_BOX["wolf"])
        subtitle_box = THEME_SUBTITLE_BOX.get(theme, THEME_SUBTITLE_BOX["wolf"])
        header_box_width = header_box[2] - header_box[0]
        subtitle_box_width = subtitle_box[2] - subtitle_box[0]

        draw.rectangle(header_box, fill=TEMPLATE_TEXT_BG)
        header_size, header_text = _fit_text_fallback(draw, guild_name.upper(), header_box_width, max_font_size=22, min_font_size=12)
        _draw_text_fb(draw, (header_box[0], header_box[1]), header_text, header_size, (255, 255, 255))

        draw.rectangle(subtitle_box, fill=TEMPLATE_TEXT_BG)
        subtitle_text = f"TO {guild_name.upper()}!"
        subtitle_size, subtitle_text = _fit_text_fallback(draw, subtitle_text, subtitle_box_width, max_font_size=34, min_font_size=16)
        _draw_text_fb(draw, (subtitle_box[0], subtitle_box[1]), subtitle_text, subtitle_size, (160, 160, 160))

    return bg


# ── Ultra card ("Customize Card") layout options ─────────────────────────
# Stored per guild as JSON in discord_welcome_config.ultra_card_json and
# edited by the Customize Card wizard (discord_bot/cogs/_views_card_customize.py).
# Anything missing/invalid falls back to these defaults, so an untouched
# guild renders exactly the classic layout.
ULTRA_DEFAULTS = {
    "banner": "bottom",        # bottom | top | none
    "dim": "medium",           # light | medium | heavy  (banner darkness)
    "avatar_side": "left",     # left | right
    "text_color": "white",     # key of ULTRA_TEXT_COLORS
    "heading": "",             # custom line; blank = "Welcome to {guild}!"
    "show_number": True,       # the "MEMBER #N" line
    # Optional style extras — all OFF by default so an untouched card keeps
    # the classic look; the wizard's "Style extras" menu turns them on.
    "ring": False,             # colored frame around the avatar
    "soft_edge": False,        # banner fades into the image instead of a hard edge
    "shadow": False,           # soft shadow under the text
    "big_name": False,         # larger username
    "glass": False,            # banner blurs the image behind it (frosted glass)
    "auto_contrast": False,    # bright image behind text -> extra outline + darker tint
    # Layout / font / crop (wizard selects). Defaults = classic look.
    "layout": "banner",        # banner | centered
    "font": "classic",         # key of ULTRA_FONTS
    "focus": "center",         # crop anchor: center | top | bottom | left | right
}
ULTRA_BOOL_KEYS = ("ring", "soft_edge", "shadow", "big_name", "glass", "auto_contrast")
ULTRA_LAYOUTS = ("banner", "centered")
ULTRA_FOCUS = ("center", "top", "bottom", "left", "right")
# Bundled OFL fonts (assets/fonts). "classic" = the card's normal font.
ULTRA_FONTS = {
    "classic": None,
    "clean": "Poppins-Bold.ttf",
    "tall": "BebasNeue-Regular.ttf",
    "script": "Pacifico-Regular.ttf",
}
ULTRA_AVATAR_COLOR = "avatar"  # text_color value: use the avatar's dominant color
ULTRA_BANNERS = ("bottom", "top", "none")
ULTRA_DIM_ALPHA = {"light": 100, "medium": 165, "heavy": 225}
ULTRA_AVATAR_SIDES = ("left", "right")
ULTRA_TEXT_COLORS = {
    "white": (255, 255, 255),
    "gold": (255, 215, 90),
    "cyan": (110, 220, 255),
    "pink": (255, 130, 190),
    "green": (120, 235, 150),
    "red": (255, 110, 110),
}
ULTRA_HEADING_MAX = 60


def parse_ultra_options(raw) -> dict:
    """Accepts a dict, a JSON string (as stored in the DB) or None and
    returns a fully-populated, validated options dict."""
    import json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    opts = dict(ULTRA_DEFAULTS)
    if raw.get("banner") in ULTRA_BANNERS:
        opts["banner"] = raw["banner"]
    if raw.get("dim") in ULTRA_DIM_ALPHA:
        opts["dim"] = raw["dim"]
    if raw.get("avatar_side") in ULTRA_AVATAR_SIDES:
        opts["avatar_side"] = raw["avatar_side"]
    if raw.get("text_color") in ULTRA_TEXT_COLORS or raw.get("text_color") == ULTRA_AVATAR_COLOR:
        opts["text_color"] = raw["text_color"]
    for k in ULTRA_BOOL_KEYS:
        if isinstance(raw.get(k), bool):
            opts[k] = raw[k]
    if raw.get("layout") in ULTRA_LAYOUTS:
        opts["layout"] = raw["layout"]
    if raw.get("font") in ULTRA_FONTS:
        opts["font"] = raw["font"]
    if raw.get("focus") in ULTRA_FOCUS:
        opts["focus"] = raw["focus"]
    if isinstance(raw.get("heading"), str):
        opts["heading"] = raw["heading"].strip()[:ULTRA_HEADING_MAX]
    if isinstance(raw.get("show_number"), bool):
        opts["show_number"] = raw["show_number"]
    return opts


def _avatar_dominant_color(avatar: Image.Image) -> tuple:
    """A readable accent taken from the avatar: average of its mid-bright,
    reasonably saturated pixels, lifted so it stays legible on a dark banner.
    Falls back to white if the avatar is flat/gray."""
    try:
        small = avatar.convert("RGB").resize((32, 32))
        picks = []
        for r, g, b in small.getdata():
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 60 and (mx - mn) > 40:
                picks.append((r, g, b))
        if not picks:
            return (255, 255, 255)
        r = sum(c[0] for c in picks) // len(picks)
        g = sum(c[1] for c in picks) // len(picks)
        b = sum(c[2] for c in picks) // len(picks)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if lum < 170:
            lift = (170 - lum) / max(1.0, 255 - lum)
            r, g, b = (int(c + (255 - c) * lift) for c in (r, g, b))
        return (r, g, b)
    except Exception:
        return (255, 255, 255)


_STYLE_FONT_CACHE: dict = {}


def _style_font(key: str, size: int):
    """Bundled style font at `size`, or None for 'classic'/missing files."""
    fname = ULTRA_FONTS.get(key)
    if not fname:
        return None
    ck = (key, size)
    if ck not in _STYLE_FONT_CACHE:
        try:
            _STYLE_FONT_CACHE[ck] = ImageFont.truetype(
                os.path.join(_MODULE_DIR, "..", "assets", "fonts", fname), size)
        except Exception:
            _STYLE_FONT_CACHE[ck] = None
    return _STYLE_FONT_CACHE[ck]


def _styled_ok(text: str) -> bool:
    """Style fonts only cover Latin; any other script keeps the fallback chain."""
    return all(ord(c) < 0x250 for c in text)


def _fit_styled(draw, text: str, max_w: int, max_size: int, min_size: int, font_key: str):
    """(size, text) for the style font, or None when it can't be used."""
    if font_key == "classic" or not text or not _styled_ok(text) or _style_font(font_key, min_size) is None:
        return None
    for size in range(max_size, min_size - 1, -2):
        if draw.textlength(text, font=_style_font(font_key, size)) <= max_w:
            return size, text
    return min_size, text


def _region_brightness(img: Image.Image, box: tuple) -> float:
    try:
        crop = img.crop(box).convert("L").resize((24, 24))
        px = list(crop.getdata())
        return sum(px) / len(px)
    except Exception:
        return 0.0


def _draw_custom_bg_card(username: str, subtitle: str, avatar_bytes: bytes,
                          background_bytes: bytes,
                          guild_name: Optional[str] = None,
                          avatar_shape: str = DEFAULT_AVATAR_SHAPE,
                          ultra_options=None) -> Optional[Image.Image]:
    """Renders the template-card layout over an ADMIN-SUPPLIED background
    (the ultra-pack / Customize Card feature) instead of one of the fixed
    THEME_BACKGROUNDS artworks. Returns None if background_bytes doesn't
    decode as an image, so the caller can fall back to a stock theme.

    Unlike _draw_template_card, there's no hand-designed "member card" box
    baked into this artwork — we don't know where it's safe to put text.
    So this draws a generic, always-safe layout: the image is cover-cropped
    to fill the card, and a semi-transparent dark banner holds the avatar,
    member number, username and heading — legible over any background.
    ultra_options (see ULTRA_DEFAULTS / parse_ultra_options) lets the admin
    move the banner (bottom/top/none), change its darkness, put the avatar
    on the left or right, recolor the text, replace the heading line and
    hide the member number.
    """
    opts = parse_ultra_options(ultra_options)
    centered = opts["layout"] == "centered"
    banner_pos = "bottom" if (centered and opts["banner"] == "top") else opts["banner"]
    try:
        bg = Image.open(io.BytesIO(background_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode custom welcome background, falling back to a stock theme: {e}")
        return None

    src_w, src_h = bg.size
    if src_w <= 0 or src_h <= 0:
        logger.warning("[v0] Custom welcome background decoded with a zero dimension, falling back to a stock theme")
        return None

    # Cover-crop to TEMPLATE_WIDTH x TEMPLATE_HEIGHT so an arbitrary
    # aspect-ratio upload never letterboxes or stretches oddly.
    target_ratio = TEMPLATE_WIDTH / TEMPLATE_HEIGHT
    src_ratio = src_w / src_h
    focus = opts["focus"]
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = 0 if focus == "left" else (src_w - new_w if focus == "right" else (src_w - new_w) // 2)
        bg = bg.crop((left, 0, left + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        top = 0 if focus == "top" else (src_h - new_h if focus == "bottom" else (src_h - new_h) // 2)
        bg = bg.crop((0, top, src_w, top + new_h))
    bg = bg.resize((TEMPLATE_WIDTH, TEMPLATE_HEIGHT))

    # Slight overall darkening so white text stays legible on bright uploads.
    bg = Image.alpha_composite(bg, Image.new("RGBA", bg.size, (0, 0, 0, 40)))

    if centered:
        band_top, band_bottom = int(TEMPLATE_HEIGHT * 0.42), TEMPLATE_HEIGHT
    elif banner_pos == "top":
        band_top, band_bottom = 0, int(TEMPLATE_HEIGHT * 0.28)
    else:  # "bottom", and "none" (no banner drawn, but same text placement)
        band_top, band_bottom = int(TEMPLATE_HEIGHT * 0.72), TEMPLATE_HEIGHT
    band_h = band_bottom - band_top

    if banner_pos != "none":
        # Real alpha blend (drawing an RGBA rectangle straight onto an RGBA
        # image overwrites pixels, which came out fully opaque black).
        banner = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        banner_alpha = ULTRA_DIM_ALPHA[opts["dim"]]
        bd = ImageDraw.Draw(banner)
        if opts["soft_edge"]:
            # Fade the banner's inner edge over ~90px instead of a hard line.
            fade = 90
            for i in range(fade):
                a = int(banner_alpha * (i + 1) / fade)
                y = (band_top - fade + i) if banner_pos != "top" else (band_bottom + fade - 1 - i)
                if 0 <= y < TEMPLATE_HEIGHT:
                    bd.line((0, y, TEMPLATE_WIDTH, y), fill=(0, 0, 0, a))
        if opts["glass"]:
            # Frosted glass: blur what is behind the band, lighter tint.
            region = bg.crop((0, band_top, TEMPLATE_WIDTH, band_bottom)).filter(ImageFilter.GaussianBlur(22))
            bg.paste(region, (0, band_top))
            banner_alpha = int(banner_alpha * 0.55)
        bd.rectangle((0, band_top, TEMPLATE_WIDTH, band_bottom), fill=(0, 0, 0, banner_alpha))
        bg = Image.alpha_composite(bg, banner)
    draw = ImageDraw.Draw(bg)

    # Without a solid banner (or with a light one) add a thin dark outline
    # so text stays readable on busy images.
    stroke = 2 if (banner_pos == "none" or opts["dim"] == "light") else 0
    if opts["auto_contrast"] and _region_brightness(bg, (0, band_top, TEMPLATE_WIDTH, band_bottom)) > 130:
        # Text sits on a bright area: outline it and darken that strip a bit.
        stroke = max(stroke, 3)
        bg = Image.alpha_composite(bg, Image.new("RGBA", bg.size, (0, 0, 0, 0)))
        shade = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        ImageDraw.Draw(shade).rectangle((0, band_top, TEMPLATE_WIDTH, band_bottom), fill=(0, 0, 0, 70))
        bg = Image.alpha_composite(bg, shade)
        draw = ImageDraw.Draw(bg)

    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank frame instead: {e}")
        avatar = Image.new("RGBA", (200, 200), (88, 101, 242, 255))

    avatar_dim = int(TEMPLATE_HEIGHT * 0.29) if centered else band_h - 40
    avatar = avatar.resize((avatar_dim, avatar_dim))
    mask = Image.new("L", (avatar_dim, avatar_dim), 0)
    shape_fn = AVATAR_SHAPES.get(avatar_shape, _mask_circle)
    shape_fn(ImageDraw.Draw(mask), (0, 0, avatar_dim, avatar_dim), 255)
    if centered:
        avatar_x = (TEMPLATE_WIDTH - avatar_dim) // 2
        avatar_y = band_top - avatar_dim // 2
        text_x = 60
        text_box_width = TEMPLATE_WIDTH - 120
    else:
        avatar_y = band_top + 20
        if opts["avatar_side"] == "right":
            avatar_x = TEMPLATE_WIDTH - 60 - avatar_dim
            text_x = 60
            text_box_width = avatar_x - 40 - text_x
        else:
            avatar_x = 60
            text_x = avatar_x + avatar_dim + 40
            text_box_width = TEMPLATE_WIDTH - text_x - 60
    if opts["text_color"] == ULTRA_AVATAR_COLOR:
        color = _avatar_dominant_color(avatar)
    else:
        color = ULTRA_TEXT_COLORS[opts["text_color"]]

    if opts["ring"]:
        ring_w = 8
        big = avatar_dim + ring_w * 2
        ring_mask = Image.new("L", (big, big), 0)
        shape_fn(ImageDraw.Draw(ring_mask), (0, 0, big, big), 255)
        bg.paste(Image.new("RGBA", (big, big), color + (255,)),
                 (avatar_x - ring_w, avatar_y - ring_w), ring_mask)
    bg.paste(avatar, (avatar_x, avatar_y), mask)

    heading = opts["heading"]
    if heading:
        heading = heading.replace("{guild}", guild_name or "").replace("{member}", username)
    elif guild_name:
        heading = f"Welcome to {guild_name}!"

    font_key = opts["font"]
    name_max = (84 if centered else 72) if opts["big_name"] else (64 if centered else 52)
    if font_key == "tall":   # Bebas Neue runs small for its size
        name_max = int(name_max * 1.25)
    name_fit = _fit_styled(draw, username, text_box_width, name_max, 22, font_key)
    if name_fit is None:
        name_fit = _fit_text_fallback(draw, username, text_box_width, max_font_size=name_max, min_font_size=22)
        name_styled = False
    else:
        name_styled = True
    username_size, username_text = name_fit
    heading_styled = False
    if heading:
        head_fit = _fit_styled(draw, heading, text_box_width, 34 if centered else 28, 14, font_key)
        if head_fit is None:
            head_fit = _fit_text_fallback(draw, heading, text_box_width, max_font_size=34 if centered else 28, min_font_size=14)
        else:
            heading_styled = True
        guild_size, guild_text = head_fit

    number_text = _extract_member_number(subtitle) if opts["show_number"] else ""
    if centered:
        name_y = avatar_y + avatar_dim + 34
        head_y = name_y + int(username_size * 1.4) + 14
        num_y = (head_y + int(guild_size * 1.5) + 18) if heading else (name_y + int(username_size * 1.4) + 18)
    else:
        name_y, head_y, num_y = band_top + 62, band_bottom - 50, band_top + 24

    from modules.level_card import _textlength_fallback as _tlf

    def _x_for(d, text, size, styled):
        """Left x of a line: text_x in banner layout, centered otherwise."""
        if not centered:
            return text_x
        if styled:
            w = d.textlength(text, font=_style_font(font_key, size))
        else:
            w = _tlf(d, text, size)
        return int((TEMPLATE_WIDTH - w) / 2)

    def _put(d, x, y, text, size, styled, col, st):
        if styled:
            d.text((x, y), text, font=_style_font(font_key, size), fill=col,
                   stroke_width=st, stroke_fill=(0, 0, 0))
        else:
            _draw_text_fb(d, (x, y), text, size, col, stroke_width=st, stroke_fill=(0, 0, 0))

    def _draw_texts(d, col, scol, off=(0, 0), st=0):
        ox, oy = off
        if number_text:
            nx = _x_for(d, number_text, 26, False)
            d.text((nx + ox, num_y + oy), number_text, font=_load_font(26),
                   fill=col if scol else (190, 190, 190), stroke_width=st, stroke_fill=(0, 0, 0))
        _put(d, _x_for(d, username_text, username_size, name_styled) + ox, name_y + oy,
             username_text, username_size, name_styled, col, st)
        if heading:
            _put(d, _x_for(d, guild_text, guild_size, heading_styled) + ox, head_y + oy,
                 guild_text, guild_size, heading_styled, col, st)

    if opts["shadow"]:
        layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        _draw_texts(ImageDraw.Draw(layer), (0, 0, 0, 235), True, off=(3, 4))
        layer = layer.filter(ImageFilter.GaussianBlur(4))
        bg = Image.alpha_composite(bg, layer)
        draw = ImageDraw.Draw(bg)
    _draw_texts(draw, color, False, st=stroke)

    return bg


def render_welcome_card(avatar_bytes: bytes, username: str, subtitle: str,
                         background_color: str = "#2b2d31", accent_color: str = "#5865F2",
                         sticker_bytes: Optional[bytes] = None, animate: bool = False,
                         avatar_shape: str = DEFAULT_AVATAR_SHAPE,
                         guild_name: Optional[str] = None,
                         use_template: bool = True,
                         theme: str = "wolf",
                         custom_background_bytes: Optional[bytes] = None,
                         ultra_options=None) -> tuple[bytes, str]:
    """Returns (image_bytes, image_format) where image_format is 'GIF' or
    'PNG'. subtitle is typically 'Member #N' or similar.

    sticker_bytes: raw bytes of a downloaded sticker image (static or
    animated GIF/WEBP). If None, the card renders with an empty sticker
    spot (original behavior).

    animate: if True AND sticker_bytes decodes to more than one frame,
    renders an animated GIF with the sticker looping in place while the
    rest of the card stays still. If False, or the sticker turns out to
    be a single-frame image, falls back to a static PNG with that one
    sticker frame pasted in (or no sticker, if none was given/decodable).

    avatar_shape: one of AVATAR_SHAPES's keys ('circle', 'rounded_square',
    'square', 'hexagon', 'diamond'). Unknown values fall back to 'circle'.

    guild_name: if given, the template card's "BOT ARCHIVES" header label
    and "TO <server>!" subtitle are redrawn with this server's name instead
    of the artwork's baked-in placeholder text. Ignored in flat-card mode.

    use_template: when True (the default) and TEMPLATE_BACKGROUND_PATH
    exists on disk, renders the designed-background template card (see
    _draw_template_card) instead of the plain flat-color card below.
    avatar_shape IS honored in template mode. Colors (background_color/
    accent_color), sticker_bytes, and animate are NOT — the artwork has a
    fixed palette and no sticker slot, so those three are silently ignored
    here; the wizard only lets an admin touch them after switching off
    use_template (see _views_welcome.py).

    theme: which THEME_BACKGROUNDS entry to render in template mode — the
    free 'wolf' default or one of the premium card-pack themes (see
    PREMIUM_THEMES). Callers must have already checked the guild is allowed
    to use a premium theme (discord_bot/cogs/welcome.py's `theme` command
    does this at set-time); this function just renders whatever it's given.

    custom_background_bytes: raw bytes of an ultra-pack admin-supplied
    png/jpeg (see discord_bot/cogs/welcome.py's `custombg` command and
    discord_welcome_config.custom_background_url). When given and
    use_template is True, this takes precedence over theme — a guild that
    bought the ultra pack sees THEIR image, not a stock artwork. Falls
    back to the theme-based template card if the bytes fail to decode.
    Callers must have already checked ultra_pack_unlocked before passing
    this; this function just renders whatever it's given, same as theme.
    """
    if use_template:
        templated = None
        if custom_background_bytes:
            templated = _draw_custom_bg_card(username, subtitle, avatar_bytes,
                                              custom_background_bytes,
                                              guild_name=guild_name, avatar_shape=avatar_shape,
                                              ultra_options=ultra_options)
        if templated is None:
            templated = _draw_template_card(username, subtitle, avatar_bytes,
                                             guild_name=guild_name, avatar_shape=avatar_shape,
                                             theme=theme)
        if templated is not None:
            out = io.BytesIO()
            templated.convert("RGB").save(out, format="PNG")
            out.seek(0)
            return out.read(), "PNG"
        # Falls through to the flat-color card below if the template
        # background couldn't be loaded.

    base = _draw_base(username, subtitle, avatar_bytes, background_color, accent_color, avatar_shape)

    sticker_frames = []
    durations = []
    if sticker_bytes:
        try:
            src = Image.open(io.BytesIO(sticker_bytes))
            for i, frame in enumerate(ImageSequence.Iterator(src)):
                if i >= MAX_STICKER_FRAMES:
                    break
                sticker_frames.append(_fit_sticker_frame(frame))
                durations.append(frame.info.get("duration", 80) or 80)
        except Exception as e:
            logger.warning(f"[v0] Couldn't decode sticker image, rendering without it: {e}")
            sticker_frames = []

    if animate and len(sticker_frames) > 1:
        frames = []
        for sticker_frame in sticker_frames:
            composed = base.copy()
            composed.paste(sticker_frame, (STICKER_X, STICKER_Y), sticker_frame)
            frames.append(composed.convert("P", palette=Image.ADAPTIVE, colors=255))
        out = io.BytesIO()
        frames[0].save(
            out, format="GIF", save_all=True, append_images=frames[1:],
            duration=durations, loop=0, disposal=2, optimize=False,
        )
        out.seek(0)
        return out.read(), "GIF"

    # Static path: paste a single sticker frame (if we have one) or leave
    # the spot empty, then flatten to PNG.
    final = base
    if sticker_frames:
        final = base.copy()
        final.paste(sticker_frames[0], (STICKER_X, STICKER_Y), sticker_frames[0])
    out = io.BytesIO()
    final.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return out.read(), "PNG"
