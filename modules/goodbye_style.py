"""Premium goodbye styling: what a server can change about its goodbye post, and how it is applied. Pure: no Discord, no database.

Free servers keep the plain goodbye (text, channel, on/off, test) exactly as before. Premium servers can also set a
title, an embed colour, a banner image, a footer, and whether the leaver's avatar is shown.

Saved choices are KEPT when Premium lapses but are only USED while the server is Premium (effective()), the same rule
the anti-raid and honeypot extras follow, so renewing brings the design straight back.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import urlparse

TITLE_MAX = 80
FOOTER_MAX = 100
URL_MAX = 500

DEFAULT_TITLE = "👋 Goodbye"
DEFAULT_COLOR = 0x607D8B          # discord.Color.dark_grey(), the look every server has today

# (label, emoji, colour). Shown in the editor's colour menu; 'Custom' (a hex code) is added by the view.
PRESETS: Tuple[Tuple[str, str, int], ...] = (
    ("Slate", "🩶", 0x607D8B),
    ("Ruby", "❤️", 0xE5484D),
    ("Sunset", "🧡", 0xF76808),
    ("Gold", "💛", 0xFFC53D),
    ("Mint", "💚", 0x30A46C),
    ("Sky", "💙", 0x3E9BFF),
    ("Violet", "💜", 0x8E4EC6),
    ("Rose", "🌸", 0xE93D82),
    ("Midnight", "🖤", 0x1F2937),
)

_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def parse_color(text: Optional[str]) -> Optional[int]:
    """'#ff8800', 'ff8800' or '#f80' -> int; anything else -> None."""
    m = _HEX.match((text or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h, 16)


def color_hex(value: Optional[int]) -> str:
    return "#%06X" % int(value if value is not None else DEFAULT_COLOR)


def color_name(value: Optional[int]) -> str:
    for label, _emoji, colour in PRESETS:
        if colour == (value if value is not None else DEFAULT_COLOR):
            return label
    return color_hex(value)


def check_image_url(text: Optional[str]) -> Tuple[bool, Optional[str]]:
    """(ok, url). Blank is fine and means 'no banner' -> (True, None). Must be an https link to a png/jpg/gif/webp."""
    raw = (text or "").strip()
    if not raw:
        return True, None
    if len(raw) > URL_MAX or any(c.isspace() for c in raw):
        return False, None
    try:
        u = urlparse(raw)
    except ValueError:
        return False, None
    if u.scheme != "https" or not u.netloc or "@" in u.netloc:
        return False, None
    if not u.path.lower().endswith(_IMAGE_EXT):
        return False, None
    return True, raw


def effective(extras: dict, premium: bool) -> dict:
    """The look to actually post: the saved style for Premium servers, the plain default for everyone else."""
    extras = extras or {}
    if not premium:
        return {"title": None, "color": DEFAULT_COLOR, "image_url": None, "footer": None, "show_avatar": True}
    color = extras.get("goodbye_color")
    show = extras.get("goodbye_show_avatar")
    return {
        "title": (extras.get("goodbye_title") or "").strip()[:TITLE_MAX] or None,
        "color": int(color) if color is not None else DEFAULT_COLOR,
        "image_url": extras.get("goodbye_image_url") or None,
        "footer": (extras.get("goodbye_footer") or "").strip()[:FOOTER_MAX] or None,
        "show_avatar": True if show is None else bool(show),
    }


def has_custom(extras: dict) -> bool:
    """True when any style choice differs from the plain default (used for the 'customised' tick on the button)."""
    e = effective(extras, True)
    return bool(e["title"] or e["image_url"] or e["footer"] or e["color"] != DEFAULT_COLOR or not e["show_avatar"])


def summary_lines(extras: dict) -> list:
    """The current choices, one short line each, for the editor card."""
    e = effective(extras, True)
    return [
        f"🏷️ **Title:** {e['title'] or '*default*'}",
        f"🎨 **Colour:** {color_name(e['color'])}",
        f"🖼️ **Banner:** {'set' if e['image_url'] else '*none*'}",
        f"📝 **Footer:** {e['footer'] or '*none*'}",
        f"🙂 **Leaver's avatar:** {'shown' if e['show_avatar'] else 'hidden'}",
    ]
