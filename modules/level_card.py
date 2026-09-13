# path: modules/level_card.py

"""
Generates a level-up card PNG (avatar + level + XP progress bar) for
discord_bot/cogs/leveling.py. Mirrors modules/welcome_card.py's pattern —
same fallback-font handling, same circular-avatar-with-ring approach.

render_level_card_evolved() (bottom of file) is the free, no-toggle
"evolving" card that automatically replaces render_level_card() once a
member hits level 10 — see its own docstring for the interpolation model.
"""

import io
import logging
import math
import random
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

CARD_WIDTH = 900
CARD_HEIGHT = 300
AVATAR_SIZE = 180
FONT_PATH: Optional[str] = None  # e.g. "assets/fonts/Inter-Bold.ttf"


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


def render_level_card(avatar_bytes: bytes, username: str, new_level: int,
                       current_xp_in_level: int, xp_needed_for_next_level: int,
                       background_color: str = "#2b2d31", accent_color: str = "#57F287") -> bytes:
    """Returns PNG bytes for a level-up card, avatar pulled from the member's Discord profile."""
    bg = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), _hex_to_rgb(background_color))
    draw = ImageDraw.Draw(bg)
    accent_rgb = _hex_to_rgb(accent_color)

    # Accent stripe down the left edge
    draw.rectangle([(0, 0), (14, CARD_HEIGHT)], fill=accent_rgb)

    # Avatar, cropped to a circle, with an accent-colored ring — pulled from
    # the member's live Discord profile avatar (caller fetches the bytes).
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank circle instead: {e}")
        avatar = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), accent_rgb)

    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
    avatar_x, avatar_y = 60, (CARD_HEIGHT - AVATAR_SIZE) // 2
    ring_pad = 6
    draw.ellipse(
        (avatar_x - ring_pad, avatar_y - ring_pad, avatar_x + AVATAR_SIZE + ring_pad, avatar_y + AVATAR_SIZE + ring_pad),
        fill=accent_rgb,
    )
    bg.paste(avatar, (avatar_x, avatar_y), mask)

    text_x = avatar_x + AVATAR_SIZE + 50

    # "LEVEL UP!" tag
    draw.text((text_x, 55), "LEVEL UP!", font=_load_font(26), fill=accent_rgb)

    # Username
    draw.text((text_x, 90), username, font=_load_font(44), fill=(255, 255, 255))

    # New level, large
    draw.text((text_x, 148), f"Level {new_level}", font=_load_font(34), fill=(255, 255, 255))

    # XP progress bar
    bar_x, bar_y = text_x, 205
    bar_w, bar_h = CARD_WIDTH - text_x - 60, 26
    draw.rounded_rectangle(
        (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=bar_h // 2, fill=(60, 63, 68),
    )
    if xp_needed_for_next_level > 0:
        fraction = max(0.0, min(1.0, current_xp_in_level / xp_needed_for_next_level))
    else:
        fraction = 1.0
    fill_w = max(bar_h, int(bar_w * fraction)) if fraction > 0 else 0
    if fill_w > 0:
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), radius=bar_h // 2, fill=accent_rgb,
        )

    xp_label = f"{current_xp_in_level}/{xp_needed_for_next_level} XP"
    draw.text((bar_x, bar_y + bar_h + 8), xp_label, font=_load_font(20), fill=(200, 200, 200))

    out = io.BytesIO()
    bg.save(out, format="PNG")
    out.seek(0)
    return out.read()


# ---------------------------------------------------------------------------
# Evolving level-up card (levels 10-20) — free, automatic, no toggle.
#
# Design language mirrors the "ShadowKing" reference sheet: a dark
# near-black card, a circular avatar with a glowing accent ring, a
# right-hand "stage" badge, and a percentage ring next to a horizontal XP
# bar. All four checkpoints below share that exact layout — only color,
# glow radius, particle density, and the ring's completeness change — so
# render_level_card_evolved() draws ONE parameterized card and interpolates
# between checkpoint values using progress_fraction, rather than branching
# to 4 separate drawing functions.
#
# Checkpoints (frac = (new_level - 10) / 10, clamped to [0, 1]):
#   0.0  -> level 10  "AWAKENED"     cool blue, minimal glow/particles
#   0.3  -> level 13  "CHARGED"      brighter cyan, light particles
#   0.6  -> level 16  "OVERCHARGED"  blue-to-purple gradient, denser particles
#   1.0  -> level 20  "ASCENDED"     blue-purple-gold holographic, max glow
# Anything between two checkpoints linearly blends that pair's color/glow/
# particle values, so e.g. level 18 (frac=0.8) looks like a point 2/3 of
# the way from OVERCHARGED to ASCENDED rather than jumping between them.
# ---------------------------------------------------------------------------

_EVOLVED_CHECKPOINTS = [
    # (frac, ring_rgb, glow_rgb, particle_count, ring_width, label, subtitle)
    (0.0, (74, 158, 255), (60, 140, 255), 6, 5, "AWAKENED", "A NEW JOURNEY BEGINS"),
    (0.3, (56, 209, 255), (40, 200, 255), 14, 6, "CHARGED", "MORE POWER. MORE POSSIBILITIES."),
    (0.6, (150, 110, 255), (120, 90, 255), 26, 7, "OVERCHARGED", "BEYOND LIMITS"),
    (1.0, (255, 205, 90), (255, 170, 60), 42, 9, "ASCENDED", "THE PINNACLE"),
]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rgb(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(_lerp(c1[i], c2[i], t)) for i in range(3))


def _evolved_stage_params(progress_fraction: float):
    """Blends the two nearest checkpoints in _EVOLVED_CHECKPOINTS for the
    given fraction. Returns (ring_rgb, glow_rgb, particle_count, ring_width,
    label, subtitle, stage_t) — stage_t is 0.0 at the lower checkpoint and
    1.0 at the upper one (used to also fade the badge text between the two
    stage names right at a boundary, avoiding an abrupt label swap)."""
    frac = max(0.0, min(1.0, progress_fraction))
    pts = _EVOLVED_CHECKPOINTS
    for i in range(len(pts) - 1):
        f0, ring0, glow0, pc0, rw0, label0, sub0 = pts[i]
        f1, ring1, glow1, pc1, rw1, label1, sub1 = pts[i + 1]
        if f0 <= frac <= f1:
            span = (f1 - f0) or 1.0
            t = (frac - f0) / span
            ring_rgb = _lerp_rgb(ring0, ring1, t)
            glow_rgb = _lerp_rgb(glow0, glow1, t)
            particles = round(_lerp(pc0, pc1, t))
            ring_width = round(_lerp(rw0, rw1, t))
            # Label/subtitle snap to whichever checkpoint we're closer to
            # (blending the *text* would be unreadable) — only the visual
            # parameters actually interpolate continuously.
            label, subtitle = (label1, sub1) if t >= 0.5 else (label0, sub0)
            return ring_rgb, glow_rgb, particles, ring_width, label, subtitle
    # frac >= last checkpoint (level 20 and, defensively, anything beyond).
    _, ring_rgb, glow_rgb, particles, ring_width, label, subtitle = pts[-1]
    return ring_rgb, glow_rgb, particles, ring_width, label, subtitle


def _draw_glow_ellipse(bg: Image.Image, box: tuple, color: tuple, blur: int, alpha: int = 160):
    """Paints a soft glow behind something by drawing a filled ellipse on a
    separate transparent layer, Gaussian-blurring it, then compositing it
    under the rest of the card. Used for both the avatar ring glow and the
    ambient particle glow — cheap to fake in Pillow without a real shader."""
    glow_layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).ellipse(box, fill=(*color, alpha))
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(blur))
    bg.alpha_composite(glow_layer)


def _draw_particles(bg: Image.Image, center: tuple, base_radius: int, count: int, color: tuple, seed: int):
    """Scatters small glowing dots around the avatar ring — density and
    brightness both driven by `count` (already interpolated by the caller),
    so level 11 gets a light dusting and level 20 gets a dense field."""
    if count <= 0:
        return
    rng = random.Random(seed)  # deterministic per-render so it doesn't flicker if re-rendered
    layer = Image.new("RGBA", bg.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = center
    for _ in range(count):
        angle = rng.uniform(0, 2 * math.pi)
        dist = base_radius + rng.uniform(4, base_radius * 0.6)
        x = cx + math.cos(angle) * dist
        y = cy + math.sin(angle) * dist
        r = rng.uniform(1.5, 3.5)
        alpha = rng.randint(120, 220)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*color, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(0.6))
    bg.alpha_composite(layer)


def _draw_percent_ring(draw: ImageDraw.ImageDraw, center: tuple, radius: int, width: int,
                        fraction: float, ring_rgb: tuple, track_rgb: tuple = (35, 38, 46)):
    cx, cy = center
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.arc(box, start=0, end=360, fill=track_rgb, width=width)
    if fraction > 0:
        end_angle = -90 + 360 * max(0.0, min(1.0, fraction))
        draw.arc(box, start=-90, end=end_angle, fill=ring_rgb, width=width)


def render_level_card_evolved(avatar_bytes: bytes, username: str, new_level: int,
                               current_xp_in_level: int, xp_needed_for_next_level: int,
                               background_color: str = "#05070d", accent_color: str = "#3B9AFF",
                               progress_fraction: Optional[float] = None) -> bytes:
    """The free "evolved" level-up card used for level 10+ (see leveling.py's
    _send_level_up_card, which branches new_level >= 10 to this instead of
    render_level_card). Same argument order/shape as render_level_card with
    accent_color/progress_fraction appended, for drop-in compatibility.

    progress_fraction: (new_level - 10) / 10, clamped to [0, 1] by the
    caller or by this function if omitted — drives every interpolated
    visual below (ring/glow color, particle density, ring thickness, and
    which of the 4 stage labels is shown). Callers should pass it
    explicitly (leveling.py does); it's computed here too as a fallback so
    this function is still safe to call directly/in tests.

    accent_color is accepted for signature-compatibility with
    render_level_card but is NOT used for the ring/glow color here — those
    come entirely from the interpolated stage palette (_EVOLVED_CHECKPOINTS)
    so the evolution always reads as blue -> cyan -> purple -> gold
    regardless of a guild's configured accent color. It's still applied to
    the thin accent stripe and the XP-bar track highlight, so a guild's
    branding isn't completely invisible on the card.
    """
    if progress_fraction is None:
        progress_fraction = (new_level - 10) / 10
    frac = max(0.0, min(1.0, progress_fraction))
    ring_rgb, glow_rgb, particle_count, ring_width, stage_label, stage_subtitle = _evolved_stage_params(frac)
    accent_rgb = _hex_to_rgb(accent_color)

    bg = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), _hex_to_rgb(background_color) + (255,))

    # Faint accent stripe along the top edge — same idea as render_level_card's
    # left-edge stripe, just relocated so it doesn't collide with the avatar
    # ring's own glow on this design.
    ImageDraw.Draw(bg).rectangle([(0, 0), (CARD_WIDTH, 4)], fill=accent_rgb)

    avatar_x, avatar_y = 55, (CARD_HEIGHT - AVATAR_SIZE) // 2
    avatar_cx, avatar_cy = avatar_x + AVATAR_SIZE // 2, avatar_y + AVATAR_SIZE // 2

    # Glow behind the avatar ring — radius and alpha both scale with frac so
    # level 10 gets a subtle halo and level 20 gets a strong bloom.
    glow_radius = int(AVATAR_SIZE * 0.62 + 18 * frac)
    glow_blur = int(_lerp(10, 26, frac))
    _draw_glow_ellipse(
        bg,
        (avatar_cx - glow_radius, avatar_cy - glow_radius, avatar_cx + glow_radius, avatar_cy + glow_radius),
        glow_rgb, blur=glow_blur, alpha=int(_lerp(110, 210, frac)),
    )
    _draw_particles(bg, (avatar_cx, avatar_cy), AVATAR_SIZE // 2, particle_count, glow_rgb, seed=new_level)

    draw = ImageDraw.Draw(bg)

    # Avatar itself, cropped to a circle with a solid ring in the
    # interpolated stage color (drawn crisp, on top of the soft glow layer).
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE))
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image, using a blank circle instead: {e}")
        avatar = Image.new("RGBA", (AVATAR_SIZE, AVATAR_SIZE), ring_rgb)

    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
    ring_pad = 5
    draw.ellipse(
        (avatar_x - ring_pad, avatar_y - ring_pad, avatar_x + AVATAR_SIZE + ring_pad, avatar_y + AVATAR_SIZE + ring_pad),
        outline=ring_rgb, width=max(3, ring_width // 2),
    )
    bg.paste(avatar, (avatar_x, avatar_y), mask)

    text_x = avatar_x + AVATAR_SIZE + 55

    # "LEVEL UP!" tag + username, same positions as the free card so the
    # transition from render_level_card -> render_level_card_evolved at
    # level 10 doesn't visually jump around.
    draw.text((text_x, 40), "LEVEL UP!", font=_load_font(24), fill=ring_rgb)
    draw.text((text_x, 72), username, font=_load_font(38), fill=(255, 255, 255))

    # Big "Lv. N" + stage label stacked underneath, matching the reference
    # sheet's "Lv. 10 / AWAKENED" grouping.
    draw.text((text_x, 122), f"Lv. {new_level}", font=_load_font(40), fill=(255, 255, 255))
    draw.text((text_x, 168), stage_label, font=_load_font(18), fill=ring_rgb)

    # XP bar — same track/fill approach as render_level_card, just recolored
    # to the interpolated ring color, with a thin accent-colored cap so the
    # guild's own accent color still shows up somewhere on the card.
    bar_x, bar_y = text_x, 210
    bar_w, bar_h = 330, 20
    draw.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + bar_h), radius=bar_h // 2, fill=(30, 32, 38))
    if xp_needed_for_next_level > 0:
        xp_fraction = max(0.0, min(1.0, current_xp_in_level / xp_needed_for_next_level))
    else:
        xp_fraction = 1.0
    fill_w = max(bar_h, int(bar_w * xp_fraction)) if xp_fraction > 0 else 0
    if fill_w > 0:
        draw.rounded_rectangle((bar_x, bar_y, bar_x + fill_w, bar_y + bar_h), radius=bar_h // 2, fill=ring_rgb)
    draw.text((bar_x, bar_y + bar_h + 6), f"{current_xp_in_level} / {xp_needed_for_next_level} XP",
               font=_load_font(16), fill=(190, 190, 190))

    # Percentage ring to the right of the XP bar (mirrors the reference
    # sheet's circular "41% / 50% / 62% / 100%" indicator).
    ring_cx = bar_x + bar_w + 70
    ring_cy = bar_y + bar_h // 2
    ring_radius = 34
    _draw_percent_ring(draw, (ring_cx, ring_cy), ring_radius, max(4, ring_width - 1), xp_fraction, ring_rgb)
    pct_text = f"{int(round(xp_fraction * 100))}%"
    pct_font = _load_font(16)
    pct_bbox = draw.textbbox((0, 0), pct_text, font=pct_font)
    draw.text(
        (ring_cx - (pct_bbox[2] - pct_bbox[0]) / 2, ring_cy - (pct_bbox[3] - pct_bbox[1]) / 2 - pct_bbox[1]),
        pct_text, font=pct_font, fill=(255, 255, 255),
    )

    # Right-hand stage badge (icon + subtitle), separated by a thin vertical
    # divider — matches the reference sheet's "AWAKENED / A NEW JOURNEY
    # BEGINS" block on the far right. Icon shape escalates from a small
    # diamond at level 10 to a crown-ish double-triangle at level 20 to
    # sell "capstone" without needing bundled icon assets.
    divider_x = CARD_WIDTH - 190
    draw.line((divider_x, 30, divider_x, CARD_HEIGHT - 30), fill=(50, 53, 62), width=2)
    badge_cx = divider_x + (CARD_WIDTH - divider_x) // 2

    icon_y = CARD_HEIGHT // 2 - 55
    icon_size = int(_lerp(10, 20, frac))
    if frac < 0.75:
        # Simple glowing diamond — grows slightly with progress.
        draw.polygon(
            [(badge_cx, icon_y - icon_size), (badge_cx + icon_size, icon_y),
             (badge_cx, icon_y + icon_size), (badge_cx - icon_size, icon_y)],
            fill=ring_rgb,
        )
    else:
        # Crown-ish silhouette for the top of the OVERCHARGED->ASCENDED
        # range, reinforcing "max level" without a bundled icon asset.
        base_y = icon_y + icon_size
        draw.polygon(
            [(badge_cx - icon_size, base_y), (badge_cx - icon_size, icon_y - 2),
             (badge_cx - icon_size // 2, icon_y + icon_size // 2), (badge_cx, icon_y - icon_size),
             (badge_cx + icon_size // 2, icon_y + icon_size // 2), (badge_cx + icon_size, icon_y - 2),
             (badge_cx + icon_size, base_y)],
            fill=ring_rgb,
        )

    label_font = _load_font(17)
    label_bbox = draw.textbbox((0, 0), stage_label, font=label_font)
    draw.text((badge_cx - (label_bbox[2] - label_bbox[0]) / 2, CARD_HEIGHT // 2 - 5), stage_label,
               font=label_font, fill=ring_rgb)

    sub_font = _load_font(11)
    # Wrap the subtitle across up to two lines so longer stage subtitles
    # (e.g. "MORE POWER. MORE POSSIBILITIES.") don't overflow the badge
    # column's width.
    words = stage_subtitle.split(" ")
    sub_lines, current_line = [], ""
    for word in words:
        trial = f"{current_line} {word}".strip()
        if draw.textlength(trial, font=sub_font) <= (CARD_WIDTH - divider_x - 20):
            current_line = trial
        else:
            if current_line:
                sub_lines.append(current_line)
            current_line = word
    if current_line:
        sub_lines.append(current_line)
    for i, line in enumerate(sub_lines[:2]):
        line_bbox = draw.textbbox((0, 0), line, font=sub_font)
        draw.text((badge_cx - (line_bbox[2] - line_bbox[0]) / 2, CARD_HEIGHT // 2 + 22 + i * 16), line,
                   font=sub_font, fill=(170, 170, 175))

    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return out.read()


# ---------------------------------------------------------------------------
# Leaderboard card — replaces the plain text-embed /leaderboard with a
# rendered image (avatar + display name + level/XP + top role per row).
# Same dark-card / PIL-composite approach as the rest of this module; no
# new dependencies. See discord_bot/cogs/leveling.py's `leaderboard`
# command for how entries are assembled (member/role lookups happen there
# — this function only draws whatever dicts it's handed).
# ---------------------------------------------------------------------------

LEADERBOARD_WIDTH = 900
_LB_HEADER_HEIGHT = 80
_LB_ROW_HEIGHT = 76
_LB_ROW_PAD = 10
_LB_AVATAR_SIZE = 52

# Rank-specific accent colors for the top 3 rows (gold/silver/bronze) — every
# row past #3 uses the guild's accent_color instead, same idea as a lot of
# game leaderboards visually calling out only the podium.
_LB_RANK_COLORS = {1: (255, 205, 90), 2: (200, 205, 215), 3: (205, 140, 90)}


def _fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, font_size: int, min_size: int = 12) -> tuple:
    """Shrinks font size until text fits max_width, same idea as
    welcome_card.py's _fit_text_to_box but returning (font, text) for a
    single line rather than wrapping — leaderboard rows are one line each,
    so a long display name should shrink, not wrap."""
    size = font_size
    while size > min_size:
        font = _load_font(size)
        if draw.textlength(text, font=font) <= max_width:
            return font, text
        size -= 2
    return _load_font(min_size), text


def render_leaderboard_card(entries: list, guild_name: str = "",
                             background_color: str = "#0d0e12", accent_color: str = "#57F287") -> bytes:
    """entries: list of dicts, ranked order (entries[0] is #1), each with
    keys: avatar_bytes (bytes or None), display_name (str), level (int),
    total_xp (int), role_name (str or None — None/"@everyone" is skipped),
    role_color (str hex or None). Caller (leveling.py's `leaderboard`
    command) fetches avatars concurrently and resolves top_role BEFORE
    calling this — this function does no I/O of its own, matching how
    render_level_card/_evolved never fetch anything either.

    Row count is whatever len(entries) is (leveling.py caps it at 10 via
    get_xp_leaderboard's limit) — height grows to fit rather than this
    function enforcing its own cap.
    """
    accent_rgb = _hex_to_rgb(accent_color)
    height = _LB_HEADER_HEIGHT + len(entries) * (_LB_ROW_HEIGHT + _LB_ROW_PAD) + _LB_ROW_PAD
    bg = Image.new("RGB", (LEADERBOARD_WIDTH, height), _hex_to_rgb(background_color))
    draw = ImageDraw.Draw(bg)

    # Header — no emoji baked into the image (Pillow's fallback font can't
    # render them, unlike Discord's own client text elsewhere on this
    # message) — plain "XP LEADERBOARD" label instead.
    draw.rectangle([(0, 0), (LEADERBOARD_WIDTH, 4)], fill=accent_rgb)
    title = f"{guild_name} - XP LEADERBOARD" if guild_name else "XP LEADERBOARD"
    draw.text((30, 26), title, font=_load_font(28), fill=(255, 255, 255))

    y = _LB_HEADER_HEIGHT
    for i, entry in enumerate(entries, start=1):
        row_color = (22, 24, 29) if i % 2 == 0 else (17, 19, 23)
        draw.rounded_rectangle(
            (14, y, LEADERBOARD_WIDTH - 14, y + _LB_ROW_HEIGHT), radius=10, fill=row_color,
        )
        rank_color = _LB_RANK_COLORS.get(i, accent_rgb)

        # Rank number
        rank_text = f"#{i}"
        rank_font = _load_font(22)
        draw.text((34, y + _LB_ROW_HEIGHT // 2 - 14), rank_text, font=rank_font, fill=rank_color)

        # Avatar, circular, ring in rank_color
        avatar_x = 100
        avatar_y = y + (_LB_ROW_HEIGHT - _LB_AVATAR_SIZE) // 2
        try:
            avatar_bytes = entry.get("avatar_bytes")
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((_LB_AVATAR_SIZE, _LB_AVATAR_SIZE))
        except Exception as e:
            logger.warning(f"[v0] Couldn't decode leaderboard avatar for row {i}, using a blank circle instead: {e}")
            avatar = Image.new("RGBA", (_LB_AVATAR_SIZE, _LB_AVATAR_SIZE), rank_color)
        mask = Image.new("L", (_LB_AVATAR_SIZE, _LB_AVATAR_SIZE), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, _LB_AVATAR_SIZE, _LB_AVATAR_SIZE), fill=255)
        ring_pad = 3
        draw.ellipse(
            (avatar_x - ring_pad, avatar_y - ring_pad,
             avatar_x + _LB_AVATAR_SIZE + ring_pad, avatar_y + _LB_AVATAR_SIZE + ring_pad),
            outline=rank_color, width=3,
        )
        bg.paste(avatar, (avatar_x, avatar_y), mask)

        # Display name
        name_x = avatar_x + _LB_AVATAR_SIZE + 26
        name_max_width = 300
        name_font, name_text = _fit_text(draw, entry.get("display_name", "Unknown"), name_max_width, 24)
        draw.text((name_x, y + 14), name_text, font=name_font, fill=(255, 255, 255))

        # Level / XP, under the name
        level = entry.get("level", 0)
        total_xp = entry.get("total_xp", 0)
        draw.text((name_x, y + 44), f"Level {level} - {total_xp} XP", font=_load_font(16), fill=(170, 170, 175))

        # Top role badge, right-aligned — colored pill (role_color) with the
        # role name, or nothing at all if the member has no displayed role
        # (@everyone-only) or couldn't be resolved (left the guild).
        role_name = entry.get("role_name")
        if role_name:
            role_color_hex = entry.get("role_color") or "#99AAB5"
            role_rgb = _hex_to_rgb(role_color_hex) if role_color_hex != "#000000" else (153, 170, 181)
            badge_font, badge_text = _fit_text(draw, role_name, 220, 16, min_size=12)
            text_w = draw.textlength(badge_text, font=badge_font)
            pad_x = 14
            badge_w = int(text_w + pad_x * 2)
            badge_h = 30
            badge_x2 = LEADERBOARD_WIDTH - 30
            badge_x1 = badge_x2 - badge_w
            badge_y1 = y + (_LB_ROW_HEIGHT - badge_h) // 2
            draw.rounded_rectangle(
                (badge_x1, badge_y1, badge_x2, badge_y1 + badge_h), radius=badge_h // 2,
                outline=role_rgb, width=2,
            )
            draw.text((badge_x1 + pad_x, badge_y1 + (badge_h - 16) // 2 - 1), badge_text,
                       font=badge_font, fill=role_rgb)

        y += _LB_ROW_HEIGHT + _LB_ROW_PAD

    out = io.BytesIO()
    bg.save(out, format="PNG")
    out.seek(0)
    return out.read()
