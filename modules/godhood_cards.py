# path: modules/godhood_cards.py

"""
Godhood trial cards — the 5-god "ascension" track. Separate from both the
tier-ladder level-up cards (modules/level_card.py) and the random clan
flavor cards (modules/clan_cards.py), though the render technique is
shared with clan_cards.py (green/flat-hole circle -> member avatar
composited in).

Trigger: on hitting level 10 or 11 (see database.py's
get_or_assign_godhood_trial), a member has a chance to be "chosen" by one
of the 5 gods below and enters a 5-trial gauntlet. The final trial ends
in becoming a clan chief (see modules/clan_cards.py's is_chief badge) —
NOT a top-5 leaderboard placement. Top-5-leaderboard entry is the
separate, permanent reward once trial 5 is cleared (see
GODHOOD_TRIALS[4] and database.py's godhood_hall_of_fame table) — i.e.
the point of finishing the gauntlet is a permanent slot on the global
top-5 board, not just the chief badge.

Unlike CLAN_CARDS, this source art is plain opaque RGB (no baked-in
alpha hole), and the in-game god NAME is treated as canonical
independent of whatever text (if any) happens to be painted into the
art's name-plate — e.g. godhood_asafrat.png literally says "VORATH" on
its plate, and godhood_morlai.png / godhood_nemesis.png have no name text
baked in at all. That's fine: exactly like clan cards, the label is a
Python string paired with the filename, never read off the artwork.
"""

import io
import logging
import os
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "..")  # repo root, same as clan_cards.py

# (filename, label, hole_cx, hole_cy, hole_r) — hole coords hand-measured
# in each PNG's own original pixel space, same method as clan_cards.py /
# level_card.py's _TIER_IMAGE_HOLES. Unlike clan art, these sources have
# NO alpha hole baked in (plain opaque RGB) — _load_godhood_artwork
# punches one in at load time before any compositing happens.
GODHOOD_CARDS = [
    ("godhood_vorath.png", "VORATH", 187.0, 364.0, 101.5),
    ("godhood_azrak.png", "AZRAK", 185.0, 349.0, 110.0),
    ("godhood_asafrat.png", "ASAFRAT", 183.0, 356.0, 113.0),
    ("godhood_morlai.png", "MORLAI", 198.0, 348.0, 113.5),
    ("godhood_nemesis.png", "NEMESIS", 181.0, 359.0, 105.0),
]

GODHOOD_CARD_FILENAMES = [c[0] for c in GODHOOD_CARDS]
_GODHOOD_BY_FILENAME = {c[0]: c for c in GODHOOD_CARDS}
_GODHOOD_BY_LABEL = {c[1]: c for c in GODHOOD_CARDS}

CARD_HEIGHT = 300  # matches clan_cards.py / level_card.py sizing convention

# The 5-trial gauntlet. Trial 5 (clan chief) is the "become a clan chief"
# capstone; clearing it grants the permanent top-5 hall-of-fame slot —
# that global top-5 board, not the chief badge itself, is the actual end
# goal the owner asked for. Concrete per-god task text / targets are
# intentionally left for the game-design pass — this is the state-machine
# shape, not the copy.
GODHOOD_TRIALS = [
    {
        "trial_no": 1,
        "title": "Trial of Recognition",
        # Combined activity-volume target: messages sent + XP gained since
        # the trial started. No separate weighting for text vs. voice XP
        # needed — both already feed the same discord_xp.total_xp pool
        # (see modules/leveling.py / discord_bot/cogs/voice_xp.py's
        # module docstring), so "XP gained" already covers both.
        "target_type": "activity_combined",  # messages_sent + xp_gained
        "target_amount": 400,
        "grants": None,
    },
    {
        # Deliberately NOT tied to heist wins or the coin economy — pure
        # XP-grind checkpoints instead, using modules/leveling.py's
        # existing compute_level()/discord_xp.total_xp. Each later trial's
        # target level is set high on purpose: this gauntlet is meant to
        # be genuinely hard to finish, since finishing it is what earns
        # the permanent (capped at 5) hall-of-fame slot.
        "trial_no": 2,
        "title": "Trial of Strength",
        "target_type": "level_reached",
        "target_amount": 25,
        "grants": None,
    },
    {
        "trial_no": 3,
        "title": "Trial of Sacrifice",
        "target_type": "level_reached",
        "target_amount": 40,
        "grants": None,
    },
    {
        "trial_no": 4,
        "title": "Trial of Dominion",
        "target_type": "level_reached",
        "target_amount": 60,
        "grants": None,
    },
    {
        "trial_no": 5,
        "title": "Trial of the Clan Chief",
        "target_type": "level_reached",
        "target_amount": 100,
        "grants": "clan_chief_badge+hall_of_fame_top5",
    },
]

# Flat probability (0-100) that hitting a trigger level actually results
# in being "chosen" by a god, rolled independently each time a member
# hits one of GODHOOD_TRIGGER_LEVELS (and isn't already in an active or
# tier-blocked gauntlet — see database.py's get_or_assign_godhood_trial).
GODHOOD_CHOSEN_CHANCE_PERCENT = 20

# Levels at which a member becomes eligible to be randomly chosen by a
# god (see database.py's get_or_assign_godhood_trial for the roll + the
# "you have been chosen" message). Kept as a set/tuple so the leveling
# cog can just do `if new_level in GODHOOD_TRIGGER_LEVELS`. Grouped in
# pairs 10 levels apart (10/11, 20/21, 30/31) — a member who FAILED a
# trial does not get re-rolled until they hit the next pair up (see
# GODHOOD_TRIGGER_TIERS below); this tuple is only "is this level a
# trigger level at all", not "is this member eligible right now".
GODHOOD_TRIGGER_LEVELS = (10, 11, 20, 21, 30, 31)

# Same levels grouped into tiers, so the leveling cog / database.py can
# find "the next tier up from here" when deciding if a previously-failed
# member is eligible for a re-roll (failing at tier N blocks re-rolls
# until tier N+1's pair of levels).
GODHOOD_TRIGGER_TIERS = ((10, 11), (20, 21), (30, 31))

# Per-trial deadline once a trial actually starts (trial_started_at ->
# trial_deadline_at in discord_godhood_trials). Missing this fails the
# trial (failed_at set) and — per GODHOOD_TRIGGER_TIERS above — blocks
# any re-roll until the member reaches the next trigger tier's levels.
GODHOOD_TRIAL_DEADLINE_DAYS = 3


@lru_cache(maxsize=None)
def _load_godhood_artwork(filename: str):
    """Loads the source art and punches a real transparent circular hole
    into it at that card's hand-measured hole coords. The source PNGs are
    plain opaque RGB (unlike clan_cards.py's art, which already ships
    with the hole baked into its alpha channel), so without this step the
    avatar composite in render_godhood_card would be painted over by
    solid opaque pixels instead of showing through."""
    path = os.path.join(_ASSET_DIR, filename)
    art = Image.open(path).convert("RGBA")
    _, _, hole_cx, hole_cy, hole_r = _GODHOOD_BY_FILENAME[filename]

    # mask: 255 everywhere except 0 inside the hole circle. Taking the
    # darker of (existing alpha, mask) per-pixel gives alpha=0 inside the
    # hole and alpha=255 (unchanged) everywhere else, since source is opaque.
    mask = Image.new("L", art.size, 255)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (hole_cx - hole_r, hole_cy - hole_r, hole_cx + hole_r, hole_cy + hole_r),
        fill=0,
    )
    from PIL import ImageChops
    punched_alpha = ImageChops.darker(art.split()[3], mask)
    art.putalpha(punched_alpha)
    return art


def render_godhood_card(avatar_bytes: bytes, godhood_filename: str, is_chief: bool = False) -> bytes:
    """Composites the member's avatar into the god card's circular hole.
    Same pattern as clan_cards.render_clan_card: art scaled to
    CARD_HEIGHT, avatar pasted at the (scaled) hole coords, canvas
    alpha-composited on top so the hole shows the avatar through.

    is_chief=True reuses clan_cards' placeholder crown badge so trial 5
    (clan chief) doesn't block on new art either."""
    filename, label, hole_cx, hole_cy, hole_r = _GODHOOD_BY_FILENAME[godhood_filename]
    art = _load_godhood_artwork(filename)
    orig_w, orig_h = art.size
    scale = CARD_HEIGHT / orig_h
    scaled_w = max(1, round(orig_w * scale))
    canvas = art.resize((scaled_w, CARD_HEIGHT), Image.LANCZOS)

    cx, cy, r = hole_cx * scale, hole_cy * scale, hole_r * scale

    avatar_layer = Image.new("RGBA", (scaled_w, CARD_HEIGHT), (0, 0, 0, 0))
    try:
        avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning(f"[v0] Couldn't decode avatar image for godhood card, using a blank circle instead: {e}")
        avatar = Image.new("RGBA", (256, 256), (40, 40, 40, 255))
    diameter = int(r * 2)
    avatar = avatar.resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter, diameter), fill=255)
    avatar_layer.paste(avatar, (int(cx - r), int(cy - r)), mask)

    bg = Image.alpha_composite(avatar_layer, canvas)
    if is_chief:
        from modules.clan_cards import _draw_crown_badge
        _draw_crown_badge(bg, cx, cy, r)
    flattened = Image.new("RGBA", bg.size, (0, 0, 0, 255))
    bg = Image.alpha_composite(flattened, bg)
    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    out.seek(0)
    return out.read()


def get_godhood_label(godhood_filename: str) -> str:
    entry = _GODHOOD_BY_FILENAME.get(godhood_filename)
    return entry[1] if entry else "UNKNOWN"


def resolve_godhood_filename(text: str) -> Optional[str]:
    """Best-effort match of a god name mentioned in free text to its
    GODHOOD_CARDS filename, case-insensitive. Mirrors
    clan_cards.resolve_clan_filename."""
    if not text:
        return None
    upper = text.upper()
    for filename, label, *_ in GODHOOD_CARDS:
        if label in upper:
            return filename
    return None


def get_next_trial(current_trial_no: int) -> Optional[dict]:
    """Returns the trial dict (title, target_type, target_amount, grants)
    for current_trial_no + 1, or None if the gauntlet is already complete
    (current_trial_no >= 5)."""
    for trial in GODHOOD_TRIALS:
        if trial["trial_no"] == current_trial_no + 1:
            return trial
    return None


def tier_index_for_level(level: int) -> Optional[int]:
    """Returns the index into GODHOOD_TRIGGER_TIERS for the tier this
    level belongs to (0 for 10/11, 1 for 20/21, 2 for 30/31), or None if
    level isn't a trigger level at all."""
    for idx, pair in enumerate(GODHOOD_TRIGGER_TIERS):
        if level in pair:
            return idx
    return None


def eligible_for_reroll(failed_at_tier: int, new_level: int) -> bool:
    """A member who failed their gauntlet at failed_at_tier is only
    eligible for a fresh roll once new_level belongs to a LATER tier
    (e.g. failed at tier 0 (10/11) -> blocked at level 11 itself, but
    eligible again at level 20). Returns False if new_level isn't a
    trigger level at all."""
    new_tier = tier_index_for_level(new_level)
    if new_tier is None:
        return False
    return new_tier > failed_at_tier
