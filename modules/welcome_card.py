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

from PIL import Image, ImageDraw, ImageFont, ImageSequence

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
}
# Themes that require the card pack purchase — everything except the
# original free 'wolf' template.
PREMIUM_THEMES: frozenset[str] = frozenset(k for k in THEME_BACKGROUNDS if k != "wolf")

TEMPLATE_WIDTH = 1536
TEMPLATE_HEIGHT = 1024

# Circular avatar slot (top-left "member card" box in the artwork).
TEMPLATE_AVATAR_BOX = (100, 398, 248, 546)

# Text block cleared and redrawn each render: "MEMBER #N" + display name.
# Per-theme override since the baked-in text sits a bit higher on the 3
# premium artworks than on the wolf template — falls back to 'wolf' the
# same way the header/subtitle boxes above do.
THEME_MEMBER_TEXT_BOX = {
    "wolf": (270, 435, 690, 545),
    "reaper": (270, 408, 690, 545),
    "shadow": (270, 408, 690, 545),
    "sorcerer": (270, 408, 690, 545),
}
TEMPLATE_MEMBER_TEXT_BOX = THEME_MEMBER_TEXT_BOX["wolf"]
TEMPLATE_MEMBER_TEXT_BG = (5, 5, 5)  # sampled from the artwork's near-black panel

# Header label ("BOT ARCHIVES") and the "TO <server>!" subtitle line under
# the WELCOME wordmark — both optional: only redrawn when guild_name is
# passed in, otherwise the artwork's own baked-in text shows through.
# Header label ("BOT ARCHIVES") and "TO <server>!" subtitle clear/redraw
# boxes — tuned per theme, since each background image places its own
# baked-in title text at a slightly different position. Falls back to the
# 'wolf' entry for any theme not listed here (e.g. a future addition) so a
# missing override never crashes a render, just reuses wolf's box.
THEME_HEADER_LABEL_BOX = {
    "wolf": (85, 80, 400, 112),
    "reaper": (85, 68, 420, 110),
    "shadow": (75, 50, 420, 95),
    "sorcerer": (75, 70, 420, 115),
}
THEME_SUBTITLE_BOX = {
    "wolf": (170, 325, 900, 368),
    "reaper": (150, 295, 900, 345),
    "shadow": (120, 290, 900, 345),
    "sorcerer": (80, 280, 900, 340),
}
TEMPLATE_HEADER_LABEL_BOX = THEME_HEADER_LABEL_BOX["wolf"]
TEMPLATE_SUBTITLE_BOX = THEME_SUBTITLE_BOX["wolf"]
TEMPLATE_TEXT_BG = (5, 5, 5)

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
    draw.text((text_x, 95), username, font=_load_font(48), fill=(255, 255, 255))
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

    ax0, ay0, ax1, ay1 = TEMPLATE_AVATAR_BOX
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
    username_font, username_text = _fit_text_to_box(draw, username, member_box_width, max_font_size=56, min_font_size=22)
    draw.text((mx + 10, my + 45), username_text, font=username_font, fill=(255, 255, 255))

    # Server name, if the caller wants it swapped in (otherwise the
    # artwork's own baked-in header/subtitle text is left alone).
    if guild_name:
        header_box = THEME_HEADER_LABEL_BOX.get(theme, THEME_HEADER_LABEL_BOX["wolf"])
        subtitle_box = THEME_SUBTITLE_BOX.get(theme, THEME_SUBTITLE_BOX["wolf"])
        header_box_width = header_box[2] - header_box[0]
        subtitle_box_width = subtitle_box[2] - subtitle_box[0]

        draw.rectangle(header_box, fill=TEMPLATE_TEXT_BG)
        header_font, header_text = _fit_text_to_box(draw, guild_name.upper(), header_box_width, max_font_size=22, min_font_size=12)
        draw.text((header_box[0], header_box[1]), header_text, font=header_font, fill=(255, 255, 255))

        draw.rectangle(subtitle_box, fill=TEMPLATE_TEXT_BG)
        subtitle_text = f"TO {guild_name.upper()}!"
        subtitle_font, subtitle_text = _fit_text_to_box(draw, subtitle_text, subtitle_box_width, max_font_size=34, min_font_size=16)
        draw.text((subtitle_box[0], subtitle_box[1]), subtitle_text, font=subtitle_font, fill=(160, 160, 160))

    return bg


def _draw_custom_bg_card(username: str, subtitle: str, avatar_bytes: bytes,
                          background_bytes: bytes,
                          guild_name: Optional[str] = None,
                          avatar_shape: str = DEFAULT_AVATAR_SHAPE) -> Optional[Image.Image]:
    """Renders the template-card layout over an ADMIN-SUPPLIED background
    (the ultra-pack /welcome custombg feature) instead of one of the fixed
    THEME_BACKGROUNDS artworks. Returns None if background_bytes doesn't
    decode as an image, so the caller can fall back to a stock theme.

    Unlike _draw_template_card, there's no hand-designed "member card" box
    baked into this artwork — we don't know where it's safe to put text.
    So this draws a generic, always-safe layout instead: the image is
    cover-cropped to fill the card, the avatar sits left-of-center, and a
    semi-transparent dark banner runs along the bottom holding the member
    number/username (and server name, if given) — legible over any
    background rather than assuming specific empty space exists.
    """
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
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        bg = bg.crop((left, 0, left + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        bg = bg.crop((0, top, src_w, top + new_h))
    bg = bg.resize((TEMPLATE_WIDTH, TEMPLATE_HEIGHT))

    # Darken slightly overall so white text stays legible on bright
    # uploads, then lay a stronger gradient-free banner across the bottom
    # third for the text itself.
    overlay = Image.new("RGBA", bg.size, (0, 0, 0, 40))
    bg = Image.alpha_composite(bg, overlay)
    draw = ImageDraw.Draw(bg)

    banner_top = int(TEMPLATE_HEIGHT * 0.72)
    draw.rectangle((0, banner_top, TEMPLATE_WIDTH, TEMPLATE_HEIGHT), fill=(0, 0, 0, 165))

    # Avatar, cropped to the configured shape, centered vertically in the
    # banner near the left edge.
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank frame instead: {e}")
        avatar = Image.new("RGBA", (200, 200), (88, 101, 242, 255))

    avatar_dim = TEMPLATE_HEIGHT - banner_top - 40
    avatar = avatar.resize((avatar_dim, avatar_dim))
    mask = Image.new("L", (avatar_dim, avatar_dim), 0)
    shape_fn = AVATAR_SHAPES.get(avatar_shape, _mask_circle)
    shape_fn(ImageDraw.Draw(mask), (0, 0, avatar_dim, avatar_dim), 255)
    avatar_x, avatar_y = 60, banner_top + 20
    bg.paste(avatar, (avatar_x, avatar_y), mask)

    text_x = avatar_x + avatar_dim + 40
    text_box_width = TEMPLATE_WIDTH - text_x - 60

    draw.text((text_x, banner_top + 24), _extract_member_number(subtitle),
               font=_load_font(26), fill=(190, 190, 190))
    username_font, username_text = _fit_text_to_box(
        draw, username, text_box_width, max_font_size=52, min_font_size=22
    )
    draw.text((text_x, banner_top + 62), username_text, font=username_font, fill=(255, 255, 255))

    if guild_name:
        guild_font, guild_text = _fit_text_to_box(
            draw, f"Welcome to {guild_name}!", text_box_width, max_font_size=28, min_font_size=14
        )
        draw.text((text_x, TEMPLATE_HEIGHT - 50), guild_text, font=guild_font, fill=(200, 200, 200))

    return bg


def render_welcome_card(avatar_bytes: bytes, username: str, subtitle: str,
                         background_color: str = "#2b2d31", accent_color: str = "#5865F2",
                         sticker_bytes: Optional[bytes] = None, animate: bool = False,
                         avatar_shape: str = DEFAULT_AVATAR_SHAPE,
                         guild_name: Optional[str] = None,
                         use_template: bool = True,
                         theme: str = "wolf",
                         custom_background_bytes: Optional[bytes] = None) -> tuple[bytes, str]:
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
                                              guild_name=guild_name, avatar_shape=avatar_shape)
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
