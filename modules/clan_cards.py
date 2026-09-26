# path: modules/clan_cards.py

"""
"Clan" flavor cards — separate from the tier-ladder level-up cards in
modules/level_card.py. Every 9 levels gained (see discord_bot/cogs/
leveling.py's on_message), a member gets sent one of these, permanently
locked to whichever clan they were randomly assigned the first time they
hit a multiple of 9 (see database.py's get_or_assign_clan_card).

Per the build spec: these cards carry NO baked-on text (no username, no
level, no "proud of you" message painted into the image) — the flavor
message goes in the Discord message content as a mention instead (see
_send_clan_message in leveling.py). The card is just the artwork with the
member's avatar composited into its circular hole, same green-chroma-key
hand-measured-hole technique as modules/level_card.py's _TIER_IMAGE_HOLES.

Clan names/art are a placeholder pool for now ("clan is random, we'll
build the real clan system later" — not level-gated, not chosen by the
user). Once a real clan system exists, get_or_assign_clan_card's random
pick is the one thing that needs to change; the render/send path stays.
"""

import io
import logging
import os
from functools import lru_cache

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "..")  # repo root, same as level_card.py's tier images

# (filename, label, hole_cx, hole_cy, hole_r) — hole coords are hand-measured
# in each PNG's own original pixel space (flat-green cut-out -> exact
# circle), same method as level_card.py's _TIER_IMAGE_HOLES.
CLAN_CARDS = [
    ("clan_white_knight.png", "WHITE", 228.0, 376.5, 145.75),
    ("clan_white_monster.png", "WHITE", 220.5, 378.0, 140.25),
    ("clan_white_wolves.png", "WHITE", 237.0, 378.0, 141.0),
    ("clan_white_dragon.png", "WHITE", 218.0, 374.5, 131.75),
    ("clan_deviors.png", "DEVIORS", 255.0, 384.0, 149.5),
]

CLAN_CARD_FILENAMES = [c[0] for c in CLAN_CARDS]
_CLAN_BY_FILENAME = {c[0]: c for c in CLAN_CARDS}

CARD_HEIGHT = 300  # matches level_card.py's CARD_WIDTH/CARD_HEIGHT sizing convention


@lru_cache(maxsize=None)
def _load_clan_artwork(filename: str):
    path = os.path.join(_ASSET_DIR, filename)
    return Image.open(path).convert("RGBA")


def render_clan_card(avatar_bytes: bytes, clan_filename: str) -> bytes:
    """Composites the member's avatar into the clan card's circular hole.
    No text is drawn — see module docstring for why. Artwork is scaled to
    CARD_HEIGHT (matching the tier cards' sizing) with width following
    proportionally, since these are wide (~3:1) banners meant to be shown
    in full, not left-cropped to a fixed CARD_WIDTH like the tier cards."""
    filename, label, hole_cx, hole_cy, hole_r = _CLAN_BY_FILENAME[clan_filename]
    art = _load_clan_artwork(filename)
    orig_w, orig_h = art.size
    scale = CARD_HEIGHT / orig_h
    scaled_w = max(1, round(orig_w * scale))
    canvas = art.resize((scaled_w, CARD_HEIGHT), Image.LANCZOS)

    cx, cy, r = hole_cx * scale, hole_cy * scale, hole_r * scale

    avatar_layer = Image.new("RGBA", (scaled_w, CARD_HEIGHT), (0, 0, 0, 0))
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image for clan card, using a blank circle instead: {e}")
        avatar = Image.new("RGBA", (256, 256), (40, 40, 40, 255))
    diameter = int(r * 2)
    avatar = avatar.resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter, diameter), fill=255)
    avatar_layer.paste(avatar, (int(cx - r), int(cy - r)), mask)

    bg = Image.alpha_composite(avatar_layer, canvas)
    flattened = Image.new("RGBA", bg.size, (0, 0, 0, 255))
    bg = Image.alpha_composite(flattened, bg)
    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return out.read()


def get_clan_label(clan_filename: str) -> str:
    entry = _CLAN_BY_FILENAME.get(clan_filename)
    return entry[1] if entry else "WHITE"
