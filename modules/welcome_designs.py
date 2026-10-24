"""Welcome Card Studio Phase 5a: saved designs (pure helpers, no Discord, no database).

A design is the LOOK only: the validated card options plus the avatar shape. Pictures are never part of it.
Every function here never raises; bad data falls back to the defaults of `parse_ultra_options`.
"""
import json
import re
from typing import Optional

from modules.welcome_card import AVATAR_SHAPES, DEFAULT_AVATAR_SHAPE, parse_ultra_options
import logging

logger = logging.getLogger(__name__)

MAX_DESIGNS = 5          # per server (and per clone)
MAX_NAME_LEN = 30


def clean_name(raw) -> Optional[str]:
    """Plain, short, single-line name. Control characters are dropped, spaces collapsed, mentions defanged.
    Returns None when nothing usable is left."""
    try:
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(raw or ""))
        text = text.replace("@", "@\u200b")
        text = re.sub(r"\s+", " ", text).strip()[:MAX_NAME_LEN].strip()
        return text or None
    except Exception:
        logger.debug("swallowed exception in clean_name", exc_info=True)
        return None


def snapshot_look(options_raw, avatar_shape) -> tuple:
    """(options_json, avatar_shape) ready to store: fully validated, so a stored design can never crash a render."""
    shape = avatar_shape if avatar_shape in AVATAR_SHAPES else DEFAULT_AVATAR_SHAPE
    return json.dumps(parse_ultra_options(options_raw), sort_keys=True), shape


def restore_look(options_json, avatar_shape) -> tuple:
    """(options_json, avatar_shape) to write back into the welcome config when a design is made live."""
    return snapshot_look(options_json, avatar_shape)
