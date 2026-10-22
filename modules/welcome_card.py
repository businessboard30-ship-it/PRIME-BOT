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

from PIL import Image, ImageDraw, ImageEnhance, ImageChops, ImageFilter, ImageFont, ImageOps, ImageSequence

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
    "avatar_glow": False,      # soft halo behind the avatar in the text color (studio 8a)
    "soft_edge": False,        # banner fades into the image instead of a hard edge
    "shadow": False,           # soft shadow under the text
    "big_name": False,         # larger username
    "glass": False,            # banner blurs the image behind it (frosted glass)
    "auto_contrast": False,    # bright image behind text -> extra outline + darker tint
    # Layout / font / crop (wizard selects). Defaults = classic look.
    "layout": "banner",        # banner | centered
    "font": "classic",         # key of ULTRA_FONTS
    "focus": "center",         # crop anchor: center | top | bottom | left | right
    "bracket": "none",         # key of ULTRA_BRACKETS, wrapped around the username
    "ratio": "3:2",            # key of ULTRA_RATIOS: the card's shape (width is always 1536 px)
    # Welcome Card Studio phase 3a (Premium/Plus only at save and join; defaults = the classic crop and text size).
    "zoom": 100,               # percent, one of ULTRA_ZOOMS: how far in the crop window moves
    "pan_x": 0,                # -100 (left end of the picture) .. 100 (right end), 0 = where the crop focus put it
    "pan_y": 0,                # -100 (top) .. 100 (bottom)
    "text_size": "medium",     # key of ULTRA_TEXT_SIZES
    "frame": "none",           # key of ULTRA_FRAMES: picture frame around the whole card (studio 3b)
    "frame_color": "white",    # key of ULTRA_TEXT_COLORS
    # Welcome Card Studio phase 3e (Premium/Plus only at save and join): move the avatar to one of nine spots.
    "heading_font": "same",    # "same" (as the name) or a key of ULTRA_FONTS: the font of the heading line (studio 4b)
    "text_outline": "auto",    # "auto" (the old rule) or a key of ULTRA_OUTLINES: dark edge around the text (studio 4c)
    "brightness": "normal",    # key of ULTRA_BRIGHTNESS: how light the background picture is (studio 4d)
    "effect": "none",          # key of ULTRA_EFFECTS: one look applied to the background picture (studio 4a)
    "avatar_pos": "auto",      # key of ULTRA_AVATAR_POS: "auto" = the layout decides; else one of nine cells
    # Welcome Card Studio phase 3c (Premium/Plus only at save and join): nudge the text block, in pixels at card width 1536.
    "text_dx": 0,              # -300 (left) .. 300 (right); the safe-area guard keeps the text on the card and off the avatar
    "text_dy": 0,              # -300 (up) .. 300 (down)
    # Welcome Card Studio phase 3d (Premium/Plus only): which cell of a 3x3 grid the text block sits in; the nudge adds on top.
    "text_pos": "auto",        # key of ULTRA_TEXT_POS; "auto" = wherever the layout puts it (the classic look)
}
ULTRA_BOOL_KEYS = ("ring", "soft_edge", "shadow", "big_name", "glass", "auto_contrast", "avatar_glow")
ULTRA_LAYOUTS = ("banner", "centered")
ULTRA_ZOOMS = (100, 125, 150, 200)
ULTRA_PAN_LIMIT = 100
ULTRA_AVATAR_POS = ("auto", "top-left", "top", "top-right", "left", "center", "right",
                    "bottom-left", "bottom", "bottom-right")   # studio 3e: where the avatar goes ("auto" = layout decides)
ULTRA_OUTLINES = {"none": 0, "thin": 2, "medium": 4, "thick": 6, "glow": 0}   # px of dark stroke; "auto" keeps the old rule
ULTRA_BRIGHTNESS = {"darker": 0.65, "dark": 0.82, "normal": 1.0, "light": 1.18, "lighter": 1.35}   # multiplier on the picture only
ULTRA_EFFECTS = ("none", "blur", "vignette", "bw", "warm", "cool", "fade", "grain", "sparkle")   # all cheap; glow/sparkle need the owner's OK
ULTRA_AVATAR_EDGE = 60       # a chosen avatar spot sits this far from the card edge (ring included)
ULTRA_NUDGE_LIMIT = 300
ULTRA_TEXT_POS = {            # grid cell -> (row, column)
    "top-left": ("top", "left"), "top": ("top", "center"), "top-right": ("top", "right"),
    "left": ("middle", "left"), "center": ("middle", "center"), "right": ("middle", "right"),
    "bottom-left": ("bottom", "left"), "bottom": ("bottom", "center"), "bottom-right": ("bottom", "right"),
}
ULTRA_TEXT_POS_PAD = 60      # how far from the card edge a grid cell puts the text block
ULTRA_NUDGE_MARGIN = 24      # the text block never gets closer than this to the card edge
ULTRA_TEXT_SIZES = {"small": 0.8, "medium": 1.0, "large": 1.2, "xl": 1.4}   # multiplies the name and heading size
ULTRA_FRAMES = ("none", "thin", "medium", "thick", "double", "rounded", "glow", "polaroid", "film")   # soft glow added in 4f with the owner's OK; polaroid / film strip: later
ULTRA_FOCUS = ("center", "top", "bottom", "left", "right", "auto")   # "auto" = smart crop (keeps the busiest part)
# Bundled OFL fonts (assets/fonts). "classic" = the card's normal font.
ULTRA_FONTS = {
    "classic": None,
    "clean": "Poppins-Bold.ttf",
    "tall": "BebasNeue-Regular.ttf",
    "script": "Pacifico-Regular.ttf",
    "tech": "Orbitron-Variable.ttf",
    "elegant": "Cinzel-Variable.ttf",
    "pixel": "PressStart2P-Regular.ttf",
    "gothic": "PirataOne-Regular.ttf",
    "comic": "Bangers-Regular.ttf",
    "horror": "Creepster-Regular.ttf",
    "round": "Righteous-Regular.ttf",
    "cursive": "Lobster-Regular.ttf",       # studio 4f: four more open-license (SIL OFL) fonts, license files in assets/fonts
    "poster": "Anton-Regular.ttf",
    "wide": "Audiowide-Regular.ttf",
    "marquee": "Monoton-Regular.ttf",
}
# Fonts that run large/small for their point size: multiplier on the username's max size.
ULTRA_FONT_SCALE = {"tall": 1.25, "pixel": 0.6, "tech": 0.9, "elegant": 0.95, "gothic": 1.2, "comic": 1.1, "horror": 1.1,
                    "cursive": 1.0, "poster": 1.15, "wide": 0.9, "marquee": 0.8}
# Optional characters wrapped around the username (Latin-1 only so every card font can draw them).
ULTRA_BRACKETS = {
    "none": ("", ""),
    "square": ("[", "]"),
    "round": ("(", ")"),
    "curly": ("{", "}"),
    "angle": ("<", ">"),
    "guillemet": ("\u00ab", "\u00bb"),
    "slashes": ("//", "//"),
    "stars": ("*", "*"),
}
# Card shape (width : height). "3:2" is the classic card (1536 x 1024) and is the default, so an untouched server
# renders exactly as before. The width never changes; the height follows the ratio.
ULTRA_RATIOS = {"3:2": (3, 2), "16:9": (16, 9), "2:1": (2, 1), "3:1": (3, 1), "4:3": (4, 3), "1:1": (1, 1)}
ULTRA_DEFAULT_RATIO = "3:2"
ULTRA_RATIO_AUTO = "auto"            # pick the card shape from the uploaded image itself


def nearest_ratio(width: int, height: int) -> str:
    """The supported card shape whose proportions are closest to an image of this size (so the least is cropped).
    Wider than 3:1 gives 3:1, portrait gives 1:1: those are the widest and tallest cards we offer."""
    import math
    if width <= 0 or height <= 0:
        return ULTRA_DEFAULT_RATIO
    r = width / height
    return min(ULTRA_RATIOS, key=lambda k: abs(math.log(r / (ULTRA_RATIOS[k][0] / ULTRA_RATIOS[k][1]))))


def _smart_crop_start(img: Image.Image, axis: int, window: int) -> int:
    """Where to start a `window`-pixel-wide crop along `axis` (0 = x, 1 = y) so it keeps the most detail (edges), with a
    small pull toward the middle so a flat sky or empty edge never wins by accident. Cheap: works on a 96px thumbnail."""
    total = img.size[axis]
    if window >= total:
        return 0
    small_len = 96                       # positions are scored on 96 slots...
    work = 480                           # ...but edges are found on a 480px copy so fine texture isn't averaged away
    wscale = work / total
    thumb = img.convert("L").resize((work, max(1, round(img.size[1 - axis] * wscale))) if axis == 0
                                    else (max(1, round(img.size[0] * wscale)), work))
    edges = thumb.filter(ImageFilter.FIND_EDGES)
    strip = edges.resize((small_len, 1), Image.BOX) if axis == 0 else edges.resize((1, small_len), Image.BOX)
    scale = small_len / total
    vals = list(strip.tobytes())                     # mode L: one byte per slot
    if len(vals) > 2:
        vals[0], vals[-1] = vals[1], vals[-2]          # the edge filter lights up the picture's own border: ignore that
    w = max(1, round(window * scale))
    if w >= small_len:
        return max(0, (total - window) // 2)
    span = small_len - w
    centre = span / 2
    csum = [0]
    for v in vals:
        csum.append(csum[-1] + v)
    best, best_score = round(centre), None
    for start in range(0, span + 1):
        score = (csum[start + w] - csum[start]) * (1.0 - 0.15 * abs(start - centre) / (centre or 1))
        # strictly better wins; a tie (a flat image) goes to the position nearest the middle
        if best_score is None or score > best_score + 1e-9 or (abs(score - best_score) <= 1e-9 and abs(start - centre) < abs(best - centre)):
            best, best_score = start, score
    return min(total - window, max(0, round(best / scale)))


def ratio_height(key) -> int:
    """Card height in pixels for a ratio key (width is TEMPLATE_WIDTH). Unknown keys give the classic height."""
    a, b = ULTRA_RATIOS.get(key, ULTRA_RATIOS[ULTRA_DEFAULT_RATIO])
    return TEMPLATE_HEIGHT if key not in ULTRA_RATIOS or key == ULTRA_DEFAULT_RATIO else round(TEMPLATE_WIDTH * b / a)


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
    if raw.get("bracket") in ULTRA_BRACKETS:
        opts["bracket"] = raw["bracket"]
    if isinstance(raw.get("ratio"), str) and (raw["ratio"] in ULTRA_RATIOS or raw["ratio"] == ULTRA_RATIO_AUTO):
        opts["ratio"] = raw["ratio"]
    if isinstance(raw.get("heading"), str):
        opts["heading"] = raw["heading"].strip()[:ULTRA_HEADING_MAX]
    if isinstance(raw.get("show_number"), bool):
        opts["show_number"] = raw["show_number"]
    z = raw.get("zoom")
    if isinstance(z, int) and not isinstance(z, bool) and z in ULTRA_ZOOMS:
        opts["zoom"] = z
    for k in ("pan_x", "pan_y"):
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            opts[k] = max(-ULTRA_PAN_LIMIT, min(ULTRA_PAN_LIMIT, v))
    if raw.get("text_size") in ULTRA_TEXT_SIZES:
        opts["text_size"] = raw["text_size"]
    if raw.get("frame") in ULTRA_FRAMES:
        opts["frame"] = raw["frame"]
    if raw.get("heading_font") == "same" or raw.get("heading_font") in ULTRA_FONTS:
        opts["heading_font"] = raw["heading_font"]
    if raw.get("text_outline") == "auto" or raw.get("text_outline") in ULTRA_OUTLINES:
        opts["text_outline"] = raw["text_outline"]
    if raw.get("brightness") in ULTRA_BRIGHTNESS:
        opts["brightness"] = raw["brightness"]
    if raw.get("effect") in ULTRA_EFFECTS:
        opts["effect"] = raw["effect"]
    if raw.get("avatar_pos") in ULTRA_AVATAR_POS:
        opts["avatar_pos"] = raw["avatar_pos"]
    if raw.get("frame_color") in ULTRA_TEXT_COLORS:
        opts["frame_color"] = raw["frame_color"]
    for k in ("text_dx", "text_dy"):
        v = raw.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            opts[k] = max(-ULTRA_NUDGE_LIMIT, min(ULTRA_NUDGE_LIMIT, v))
    if raw.get("text_pos") in ULTRA_TEXT_POS:
        opts["text_pos"] = raw["text_pos"]
    return opts


def _apply_brightness(img, level):
    """Studio 4d: lighten or darken the background picture only. Keeps alpha and size; never raises."""
    try:
        f = ULTRA_BRIGHTNESS.get(level, 1.0)
        if f == 1.0:
            return img
        img = img.convert("RGBA")
        a = img.getchannel("A")
        rgb = ImageEnhance.Brightness(img.convert("RGB")).enhance(f)
        out = rgb.convert("RGBA")
        out.putalpha(a)
        return out
    except Exception:
        return img


def _apply_effect(img, effect):
    """Studio 4a: one look on the background picture only (never the text or avatar). Returns an RGBA image of the
    same size; any unknown effect or error returns the picture unchanged. Each one is a few cheap whole-image ops."""
    try:
        img = img.convert("RGBA")
        a = img.getchannel("A")
        rgb = img.convert("RGB")
        if effect == "blur":
            rgb = rgb.filter(ImageFilter.GaussianBlur(7))
        elif effect == "bw":
            rgb = ImageOps.grayscale(rgb).convert("RGB")
        elif effect == "fade":
            rgb = Image.blend(rgb, Image.new("RGB", rgb.size, (128, 128, 128)), 0.28)
        elif effect in ("warm", "cool"):
            r, g, b = rgb.split()
            k = 1.12 if effect == "warm" else 0.88
            r = r.point(lambda v: min(255, int(v * k)))
            b = b.point(lambda v: min(255, int(v / k)))
            rgb = Image.merge("RGB", (r, g, b))
        elif effect == "grain":   # studio 4e: fixed-seed noise tile, so the same card always renders the same
            import random
            tile = Image.frombytes("L", (256, 256), random.Random(7).randbytes(256 * 256))
            tile = tile.point(lambda v: 128 + (v - 128) // 6)
            noise = Image.new("L", rgb.size)
            for ty in range(0, rgb.height, 256):
                for tx in range(0, rgb.width, 256):
                    noise.paste(tile, (tx, ty))
            rgb = ImageChops.add(rgb, Image.merge("RGB", (noise, noise, noise)), 1.0, -128)
        elif effect == "sparkle":   # studio 4f (owner approved): a few soft four-point stars, fixed seed, low opacity
            import random
            rng = random.Random(11)
            layer = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
            ld = ImageDraw.Draw(layer)
            for _ in range(max(6, rgb.width * rgb.height // 45000)):
                x, y, r = rng.randrange(rgb.width), rng.randrange(rgb.height), rng.randrange(6, 16)
                ld.line((x - r, y, x + r, y), fill=(255, 255, 255, 150), width=2)
                ld.line((x, y - r, x, y + r), fill=(255, 255, 255, 150), width=2)
                ld.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(255, 255, 255, 190))
            rgb = Image.alpha_composite(rgb.convert("RGBA"), layer.filter(ImageFilter.GaussianBlur(0.8))).convert("RGB")
        elif effect == "vignette":
            mask = Image.radial_gradient("L").resize(rgb.size)      # 0 in the middle, 255 at the corners
            mask = mask.point(lambda v: max(0, v - 110) * 255 // 145 * 150 // 255)
            rgb = Image.composite(Image.new("RGB", rgb.size, (0, 0, 0)), rgb, mask)
        else:
            return img
        out = rgb.convert("RGBA")
        out.putalpha(a)
        return out
    except Exception:
        return img


def _clamp_avatar_nudge(rect, dx, dy, W, H, text_boxes, margin=ULTRA_NUDGE_MARGIN, gap=24):
    """Safe-area guard for the avatar nudge. `rect` is (x, y, size) of the avatar with its ring, as it sits now;
    `text_boxes` are (x, y, w, h) boxes of the text lines. Returns (dx, dy) cut down so the avatar stays `margin` px
    inside the card and never gets within `gap` px of the text. The avatar slides from where it is toward the
    requested spot and stops at the first thing in its way (it cannot jump over the text). (0, 0) is always allowed.
    Pure function, never raises."""
    try:
        x, y, size = rect
        # 1. on the card (if the avatar already sits closer to an edge than the margin, that edge does not move it)
        lo_x, hi_x = min(margin, x), max(W - margin - size, x)
        lo_y, hi_y = min(margin, y), max(H - margin - size, y)
        dx = max(lo_x - x, min(hi_x - x, dx))
        dy = max(lo_y - y, min(hi_y - y, dy))
        if not dx and not dy:
            return 0, 0

        def hits(ox, oy):
            ax0, ay0, ax1, ay1 = x + ox - gap, y + oy - gap, x + ox + size + gap, y + oy + size + gap
            for (bx, by, bw, bh) in text_boxes:
                if ax0 < bx + bw and ax1 > bx and ay0 < by + bh and ay1 > by:
                    return True
            return False

        # 2. off the text: walk three paths (straight, sideways then up/down, up/down then sideways) in small steps and
        # stop each before its first hit, so the avatar can never jump over the text. The path that gets furthest wins.
        steps = 48

        def walk(segments):
            """segments: list of (dx, dy) legs, each walked from where the last one stopped. Returns the end offset."""
            cx = cy = 0
            for (sx, sy) in segments:
                ex, ey = cx + sx, cy + sy
                best = (cx, cy)
                for i in range(1, steps + 1):
                    ox, oy = round(cx + sx * i / steps), round(cy + sy * i / steps)
                    if hits(ox, oy):
                        return best
                    best = (ox, oy)
                cx, cy = ex, ey
            return cx, cy

        paths = ([(dx, dy)], [(dx, 0), (0, dy)], [(0, dy), (dx, 0)])
        ends = [walk(seg) for seg in paths]
        return max(ends, key=lambda e: abs(e[0]) + abs(e[1]))
    except Exception:
        return 0, 0


def _clamp_text_nudge(lines, dx, dy, W, H, avoid_left=None, avoid_right=None, avoid_above=None,
                      margin=ULTRA_NUDGE_MARGIN):
    """Safe-area guard for the text nudge. `lines` are (x, y, w, h) boxes of the text lines as they sit now.
    Returns (dx, dy) cut down so the whole block stays `margin` px inside the card and clear of the avatar:
    avoid_left = x the block must stay right of, avoid_right = x it must stay left of, avoid_above = y it must stay
    below. The no-move position is always allowed, so the guard can never push a default card anywhere."""
    if not lines:
        return 0, 0
    left = min(x for x, y, w, h in lines)
    right = max(x + w for x, y, w, h in lines)
    top = min(y for x, y, w, h in lines)
    bottom = max(y + h for x, y, w, h in lines)
    dx_lo, dx_hi = margin - left, W - margin - right
    dy_lo, dy_hi = margin - top, H - margin - bottom
    if avoid_left is not None:
        dx_lo = max(dx_lo, avoid_left - left)
    if avoid_right is not None:
        dx_hi = min(dx_hi, avoid_right - right)
    if avoid_above is not None:
        dy_lo = max(dy_lo, avoid_above - top)
    dx_lo, dx_hi, dy_lo, dy_hi = min(dx_lo, 0), max(dx_hi, 0), min(dy_lo, 0), max(dy_hi, 0)
    return int(max(dx_lo, min(dx_hi, dx))), int(max(dy_lo, min(dy_hi, dy)))


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


def _draw_frame(img: Image.Image, frame: str, color) -> Image.Image:
    """Studio 3b: a border around the whole card. Sizes are for the 1536 px wide card. Never changes the card size."""
    if frame == "none" or frame not in ULTRA_FRAMES:
        return img
    W, H = img.size
    col = tuple(color) + (255,)
    d = ImageDraw.Draw(img)
    if frame == "thin":
        d.rectangle((0, 0, W - 1, H - 1), outline=col, width=6)
    elif frame == "medium":   # studio 4e
        d.rectangle((0, 0, W - 1, H - 1), outline=col, width=13)
    elif frame == "thick":
        d.rectangle((0, 0, W - 1, H - 1), outline=col, width=22)
    elif frame == "double":
        d.rectangle((0, 0, W - 1, H - 1), outline=col, width=8)
        d.rectangle((18, 18, W - 19, H - 19), outline=col, width=8)
    elif frame == "glow":   # studio 4f (owner approved): soft, muted glow along the edge plus a thin line
        halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(halo).rectangle((0, 0, W - 1, H - 1), outline=tuple(color) + (170,), width=26)
        img = Image.alpha_composite(img, halo.filter(ImageFilter.GaussianBlur(16)))
        ImageDraw.Draw(img).rectangle((0, 0, W - 1, H - 1), outline=col, width=4)
    elif frame == "rounded":
        border, radius = 16, 48
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle((border, border, W - 1 - border, H - 1 - border), radius=radius, fill=255)
        img = Image.composite(img, Image.new("RGBA", (W, H), col), mask)
    elif frame == "polaroid":   # studio 8a: the finished card shrinks into a photo-print border, size unchanged
        return _shrink_into_border(img, color, side=36, top=36, bottom=110, line=True)
    elif frame == "film":   # studio 8a: dark film strip with sprocket holes left and right (holes use the frame color)
        return _film_strip(img, color)
    return img


def _shrink_into_border(img: Image.Image, color, side: int, top: int, bottom: int, line: bool = False) -> Image.Image:
    """Studio 8a: scale the whole card down (same proportions, so nothing is cut or covered) and paste it inside a
    border of `color`. Output has exactly the input size. Never raises: on any error the card is returned as is."""
    try:
        W, H = img.size
        aw, ah = max(1, W - 2 * side), max(1, H - top - bottom)
        s = min(aw / W, ah / H)
        nw, nh = max(1, int(W * s)), max(1, int(H * s))
        inner = img.convert("RGBA").resize((nw, nh), Image.LANCZOS)
        out = Image.new("RGBA", (W, H), tuple(color) + (255,))
        x, y = side + (aw - nw) // 2, top
        out.paste(inner, (x, y))
        if line:
            ImageDraw.Draw(out).rectangle((x - 1, y - 1, x + nw, y + nh), outline=(0, 0, 0, 60), width=2)
        return out
    except Exception:
        logger.exception("[welcome-studio] border frame failed")
        return img


def _film_strip(img: Image.Image, color) -> Image.Image:
    """Studio 8a: film look. Dark strip with sprocket holes on both sides; the card shrinks to fit between them."""
    try:
        W, H = img.size
        strip, vpad = 72, 18
        out = _shrink_into_border(img, (24, 24, 24), side=strip, top=vpad, bottom=vpad)
        d = ImageDraw.Draw(out)
        hole = tuple(color) + (255,)
        y = 22
        while y + 34 < H:
            for hx in (strip // 2 - 13, W - strip // 2 - 13):
                d.rounded_rectangle((hx, y, hx + 26, y + 34), radius=7, fill=hole)
            y += 70
        return out
    except Exception:
        logger.exception("[welcome-studio] film frame failed")
        return img


def _zoom_pan_box(box, src_w: int, src_h: int, zoom: int, pan_x: int, pan_y: int):
    """Studio 3a: shrink the cover-crop window by `zoom` percent around its middle, then slide it toward an end of the
    picture by pan (-100 = one end, 100 = the other). The window keeps the card's shape and never leaves the picture."""
    l, t, r, b = box
    w, h = r - l, b - t
    nw, nh = max(1, int(w * 100 / zoom)), max(1, int(h * 100 / zoom))

    def place(start, size, new, total, pan):
        c0 = start + (size - new) // 2                    # window centered on the original crop
        if pan > 0:
            c0 += int((total - new - c0) * pan / ULTRA_PAN_LIMIT)
        elif pan < 0:
            c0 += int(c0 * pan / ULTRA_PAN_LIMIT)
        return max(0, min(total - new, c0))

    nl, nt = place(l, w, nw, src_w, pan_x), place(t, h, nh, src_h, pan_y)
    return (nl, nt, nl + nw, nt + nh)


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

    # Cover-crop to W x H (W is always TEMPLATE_WIDTH; H follows the admin's chosen ratio) so an arbitrary
    # aspect-ratio upload never letterboxes or stretches oddly.
    ratio_key = nearest_ratio(src_w, src_h) if opts["ratio"] == ULTRA_RATIO_AUTO else opts["ratio"]
    W, H = TEMPLATE_WIDTH, ratio_height(ratio_key)
    classic = H == TEMPLATE_HEIGHT
    compact = H < 700                      # only the 3:1 strip: the centered layout needs tighter spacing to fit
    target_ratio = W / H
    src_ratio = src_w / src_h
    focus = opts["focus"]
    box = (0, 0, src_w, src_h)
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        if focus == "auto":
            left = _smart_crop_start(bg, 0, new_w)
        else:
            left = 0 if focus == "left" else (src_w - new_w if focus == "right" else (src_w - new_w) // 2)
        box = (left, 0, left + new_w, src_h)
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        if focus == "auto":
            top = _smart_crop_start(bg, 1, new_h)
        else:
            top = 0 if focus == "top" else (src_h - new_h if focus == "bottom" else (src_h - new_h) // 2)
        box = (0, top, src_w, top + new_h)
    if opts["zoom"] != 100 or opts["pan_x"] or opts["pan_y"]:
        box = _zoom_pan_box(box, src_w, src_h, opts["zoom"], opts["pan_x"], opts["pan_y"])
    if box != (0, 0, src_w, src_h):
        bg = bg.crop(box)
    bg = bg.resize((W, H))

    # Slight overall darkening so white text stays legible on bright uploads.
    bg = Image.alpha_composite(bg, Image.new("RGBA", bg.size, (0, 0, 0, 40)))
    if opts["effect"] != "none":
        bg = _apply_effect(bg, opts["effect"])      # studio 4a: default "none" skips this, pixel-identical
    if opts["brightness"] != "normal":
        bg = _apply_brightness(bg, opts["brightness"])   # studio 4d: default "normal" skips this, pixel-identical

    # The band (banner) keeps the classic card's size on every ratio: short cards never get a band under 220 px
    # (the text needs that much room), tall cards never get a band over the classic height.
    if classic:
        top_band_h, bottom_band_top = int(H * 0.28), int(H * 0.72)
    else:
        top_band_h = min(int(TEMPLATE_HEIGHT * 0.28), max(220, int(H * 0.28)))
        bottom_band_top = H - top_band_h
    if centered:
        band_top, band_bottom = (int(H * 0.42) if classic else (int(H * 0.40) if compact else max(int(H * 0.42), H - 600))), H
    elif banner_pos == "top":
        band_top, band_bottom = 0, top_band_h
    else:  # "bottom", and "none" (no banner drawn, but same text placement)
        band_top, band_bottom = bottom_band_top, H
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
                if 0 <= y < H:
                    bd.line((0, y, W, y), fill=(0, 0, 0, a))
        if opts["glass"]:
            # Frosted glass: blur what is behind the band, lighter tint.
            region = bg.crop((0, band_top, W, band_bottom)).filter(ImageFilter.GaussianBlur(22))
            bg.paste(region, (0, band_top))
            banner_alpha = int(banner_alpha * 0.55)
        bd.rectangle((0, band_top, W, band_bottom), fill=(0, 0, 0, banner_alpha))
        bg = Image.alpha_composite(bg, banner)
    draw = ImageDraw.Draw(bg)

    # Without a solid banner (or with a light one) add a thin dark outline
    # so text stays readable on busy images.
    stroke = 2 if (banner_pos == "none" or opts["dim"] == "light") else 0
    if opts["auto_contrast"] and _region_brightness(bg, (0, band_top, W, band_bottom)) > 130:
        # Text sits on a bright area: outline it and darken that strip a bit.
        stroke = max(stroke, 3)
        bg = Image.alpha_composite(bg, Image.new("RGBA", bg.size, (0, 0, 0, 0)))
        shade = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        ImageDraw.Draw(shade).rectangle((0, band_top, W, band_bottom), fill=(0, 0, 0, 70))
        bg = Image.alpha_composite(bg, shade)
        draw = ImageDraw.Draw(bg)

    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank frame instead: {e}")
        avatar = Image.new("RGBA", (200, 200), (88, 101, 242, 255))

    avatar_dim = ((int(H * 0.22) if compact else int(H * 0.29)) if classic or compact else min(int(TEMPLATE_HEIGHT * 0.29), int(H * 0.29))) if centered else band_h - 40
    avatar = avatar.resize((avatar_dim, avatar_dim))
    mask = Image.new("L", (avatar_dim, avatar_dim), 0)
    shape_fn = AVATAR_SHAPES.get(avatar_shape, _mask_circle)
    shape_fn(ImageDraw.Draw(mask), (0, 0, avatar_dim, avatar_dim), 255)
    if centered:
        avatar_x = (W - avatar_dim) // 2
        avatar_y = band_top - avatar_dim // 2
        text_x = 60
        text_box_width = W - 120
    else:
        avatar_y = band_top + 20
        if opts["avatar_side"] == "right":
            avatar_x = W - 60 - avatar_dim
            text_x = 60
            text_box_width = avatar_x - 40 - text_x
        else:
            avatar_x = 60
            text_x = avatar_x + avatar_dim + 40
            text_box_width = W - text_x - 60
    if opts["text_color"] == ULTRA_AVATAR_COLOR:
        color = _avatar_dominant_color(avatar)
    else:
        color = ULTRA_TEXT_COLORS[opts["text_color"]]

    def _paste_avatar(canvas, ax, ay):
        if opts["avatar_glow"]:   # studio 8a: a soft halo in the text color, drawn under the avatar
            try:
                pad = 44
                big = avatar_dim + pad * 2
                halo = Image.new("L", (big, big), 0)
                shape_fn(ImageDraw.Draw(halo), (pad - 6, pad - 6, pad + avatar_dim + 6, pad + avatar_dim + 6), 190)
                halo = halo.filter(ImageFilter.GaussianBlur(16))
                canvas.paste(Image.new("RGBA", (big, big), tuple(color) + (255,)), (ax - pad, ay - pad), halo)
            except Exception:
                logger.exception("[welcome-studio] avatar glow failed")
        if opts["ring"]:
            ring_w = 8
            big = avatar_dim + ring_w * 2
            ring_mask = Image.new("L", (big, big), 0)
            shape_fn(ImageDraw.Draw(ring_mask), (0, 0, big, big), 255)
            canvas.paste(Image.new("RGBA", (big, big), color + (255,)), (ax - ring_w, ay - ring_w), ring_mask)
        canvas.paste(avatar, (ax, ay), mask)

    # Studio 3e: a nudged avatar is drawn after the text is placed (so the guard can keep it off the text);
    # an un-nudged avatar is drawn right here, exactly as before.
    avatar_nudged = opts["avatar_pos"] != "auto"
    if not avatar_nudged:
        _paste_avatar(bg, avatar_x, avatar_y)

    heading = opts["heading"]
    if heading:
        heading = heading.replace("{guild}", guild_name or "").replace("{member}", username)
    elif guild_name:
        heading = f"Welcome to {guild_name}!"

    font_key = opts["font"]
    head_font = font_key if opts["heading_font"] == "same" else opts["heading_font"]   # studio 4b
    name_max = (84 if centered else 72) if opts["big_name"] else (64 if centered else 52)
    name_max = int(name_max * ULTRA_FONT_SCALE.get(font_key, 1.0))   # some fonts run big/small for their size
    size_k = ULTRA_TEXT_SIZES[opts["text_size"]]
    if centered:
        size_k = min(size_k, 1.2)     # safe-area guard: the centered stack must stay on the card
    if compact:
        size_k = min(size_k, 1.0)
    name_max = int(name_max * size_k)
    if compact and centered:
        name_max = int(name_max * 0.7)
    b_l, b_r = ULTRA_BRACKETS[opts["bracket"]]
    if b_l:
        username = f"{b_l}{username}{b_r}"
    name_fit = _fit_styled(draw, username, text_box_width, name_max, 22, font_key)
    if name_fit is None:
        name_fit = _fit_text_fallback(draw, username, text_box_width, max_font_size=name_max, min_font_size=22)
        name_styled = False
    else:
        name_styled = True
    username_size, username_text = name_fit
    heading_styled = False
    if heading:
        head_max = int(((24 if compact else 34) if centered else 28) * size_k)
        head_fit = _fit_styled(draw, heading, text_box_width, head_max, 14, head_font)
        if head_fit is None:
            head_fit = _fit_text_fallback(draw, heading, text_box_width, max_font_size=head_max, min_font_size=14)
        else:
            heading_styled = True
        guild_size, guild_text = head_fit

    number_text = _extract_member_number(subtitle) if opts["show_number"] else ""
    if centered:
        g1, g2, g3, k1, k2 = (16, 6, 6, 1.3, 1.4) if compact else (34, 14, 18, 1.4, 1.5)
        name_y = avatar_y + avatar_dim + g1
        head_y = name_y + int(username_size * k1) + g2
        num_y = (head_y + int(guild_size * k2) + g3) if heading else (name_y + int(username_size * k1) + g3)
    else:
        name_y, head_y, num_y = band_top + 62, band_bottom - 50, band_top + 24

    if opts["text_outline"] != "auto":   # studio 4c: the admin picked the edge; "auto" keeps the rule above
        stroke = ULTRA_OUTLINES[opts["text_outline"]]
    from modules.level_card import _textlength_fallback as _tlf

    def _x_for(d, text, size, styled, fk=None):
        """Left x of a line: text_x in banner layout, centered otherwise."""
        if not centered:
            return text_x
        if styled:
            w = d.textlength(text, font=_style_font(fk or font_key, size))
        else:
            w = _tlf(d, text, size)
        return int((W - w) / 2)

    def _put(d, x, y, text, size, styled, col, st, fk=None):
        if styled:
            d.text((x, y), text, font=_style_font(fk or font_key, size), fill=col,
                   stroke_width=st, stroke_fill=(0, 0, 0))
        else:
            _draw_text_fb(d, (x, y), text, size, col, stroke_width=st, stroke_fill=(0, 0, 0))

    tdx = tdy = 0
    boxes = []
    if opts["text_dx"] or opts["text_dy"] or opts["text_pos"] != "auto" or avatar_nudged:
        # Studio 3c/3d/3e: anchor the text block to a grid cell, nudge it, then cut it down by the safe-area guard
        # (on the card, off the avatar). With only an avatar move the boxes are still measured (the avatar guard
        # needs them) but the text itself stays where it was.
        def _w_of(text, size, styled, fk=None):
            return draw.textlength(text, font=_style_font(fk or font_key, size)) if styled else _tlf(draw, text, size)
        if number_text:
            boxes.append((_x_for(draw, number_text, 26, False), num_y, _w_of(number_text, 26, False), 34))
        boxes.append((_x_for(draw, username_text, username_size, name_styled), name_y,
                      _w_of(username_text, username_size, name_styled), int(username_size * 1.3)))
        if heading:
            boxes.append((_x_for(draw, guild_text, guild_size, heading_styled, head_font), head_y,
                          _w_of(guild_text, guild_size, heading_styled, head_font), int(guild_size * 1.3)))
        ring_pad = 8 if opts["ring"] else 0
        if centered:
            guard = {"avoid_above": avatar_y + avatar_dim + ring_pad + 8}
        elif opts["avatar_side"] == "right":
            guard = {"avoid_right": avatar_x - ring_pad - 24}
        else:
            guard = {"avoid_left": avatar_x + avatar_dim + ring_pad + 24}
        if opts["text_dx"] or opts["text_dy"] or opts["text_pos"] != "auto":
            want_dx, want_dy = opts["text_dx"], opts["text_dy"]
            if opts["text_pos"] != "auto":
                row, col = ULTRA_TEXT_POS[opts["text_pos"]]
                b_left, b_right = min(b[0] for b in boxes), max(b[0] + b[2] for b in boxes)
                b_top, b_bottom = min(b[1] for b in boxes), max(b[1] + b[3] for b in boxes)
                pad = ULTRA_TEXT_POS_PAD
                goal_x = {"left": pad, "center": (W - (b_right - b_left)) / 2, "right": W - pad - (b_right - b_left)}[col]
                goal_y = {"top": pad, "middle": (H - (b_bottom - b_top)) / 2, "bottom": H - pad - (b_bottom - b_top)}[row]
                want_dx += int(goal_x - b_left)
                want_dy += int(goal_y - b_top)
            tdx, tdy = _clamp_text_nudge(boxes, want_dx, want_dy, W, H, **guard)

    def _draw_texts(d, col, scol, off=(0, 0), st=0):
        ox, oy = off[0] + tdx, off[1] + tdy
        if number_text:
            nx = _x_for(d, number_text, 26, False)
            d.text((nx + ox, num_y + oy), number_text, font=_load_font(26),
                   fill=col if scol else (190, 190, 190), stroke_width=st, stroke_fill=(0, 0, 0))
        _put(d, _x_for(d, username_text, username_size, name_styled) + ox, name_y + oy,
             username_text, username_size, name_styled, col, st)
        if heading:
            _put(d, _x_for(d, guild_text, guild_size, heading_styled, head_font) + ox, head_y + oy,
                 guild_text, guild_size, heading_styled, col, st, head_font)

    if opts["shadow"]:
        layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        _draw_texts(ImageDraw.Draw(layer), (0, 0, 0, 235), True, off=(3, 4))
        layer = layer.filter(ImageFilter.GaussianBlur(4))
        bg = Image.alpha_composite(bg, layer)
        draw = ImageDraw.Draw(bg)
    if opts["text_outline"] == "glow":   # studio 4f (owner approved): soft glow in the text color behind the text
        glow = Image.new("RGBA", bg.size, (0, 0, 0, 0))
        _draw_texts(ImageDraw.Draw(glow), tuple(color[:3]) + (200,), True, st=3)
        bg = Image.alpha_composite(bg, glow.filter(ImageFilter.GaussianBlur(9)))
        draw = ImageDraw.Draw(bg)
    _draw_texts(draw, color, False, st=stroke)

    if avatar_nudged:
        # Studio 3e: the guard sees the text where it really is drawn (after the 3c nudge) and keeps the avatar clear.
        ring_pad = 8 if opts["ring"] else 0
        placed = [(bx + tdx, by + tdy, bw, bh) for (bx, by, bw, bh) in boxes]
        row, _, col = opts["avatar_pos"].partition("-") if "-" in opts["avatar_pos"] else (opts["avatar_pos"], "", "")
        # cell -> wanted top-left of the avatar: "top-left", "top", "left", "center" ... (a lone word is a row or a column)
        v = {"top": "top", "bottom": "bottom", "left": "mid", "right": "mid", "center": "mid"}.get(row, "mid")
        h = col or ({"left": "left", "right": "right"}.get(row, "center"))
        size = avatar_dim + 2 * ring_pad
        edge = ULTRA_AVATAR_EDGE
        want_x = {"left": edge, "center": (W - size) // 2, "right": W - edge - size}[h] + ring_pad
        want_y = {"top": edge, "mid": (H - size) // 2, "bottom": H - edge - size}[v] + ring_pad
        adx, ady = _clamp_avatar_nudge((avatar_x - ring_pad, avatar_y - ring_pad, size),
                                       want_x - avatar_x, want_y - avatar_y, W, H, placed)
        bg = bg.convert("RGBA")
        _paste_avatar(bg, avatar_x + adx, avatar_y + ady)

    if opts["frame"] != "none":
        bg = _draw_frame(bg.convert("RGBA"), opts["frame"], ULTRA_TEXT_COLORS[opts["frame_color"]])
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
