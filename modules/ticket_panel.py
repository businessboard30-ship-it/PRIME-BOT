# path: modules/ticket_panel.py

"""
Ticket panel rules (pure: no Discord, no database), shared by the panel, the ticket channel and the setup wizard.

Free:     a V2 panel with the server's icon and one Open Ticket button, ticket channel with Claim / Close.
Premium:  a "Choose how we can help" dropdown of up to MAX_CATEGORIES categories (with descriptions), a question the member
          answers before the ticket opens, a custom panel title / text / colour, custom link buttons, auto-close on
          inactivity, a transcript log channel and a custom close message.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

MAX_CATEGORIES = 10
MAX_CUSTOM_BUTTONS = 3
DEFAULT_TITLE = "Support desk"
DEFAULT_TEXT = "Need a hand? Open a private ticket below and our team will be with you."
DEFAULT_COLOR = 0x5865F2
PLACEHOLDER = "Choose how we can help"
QUESTION_LABEL = "What do you need help with?"

PRESET_CATEGORIES: List[Dict[str, str]] = [
    {"emoji": "💬", "label": "General support", "description": "Questions or help using the server"},
    {"emoji": "🚨", "label": "Member report", "description": "Privately report a member or incident"},
    {"emoji": "⚖️", "label": "Staff appeal", "description": "Appeal a moderation decision"},
    {"emoji": "🛠️", "label": "Technical help", "description": "Report a technical problem"},
    {"emoji": "🤝", "label": "Partnership request", "description": "Ask about a community partnership"},
    {"emoji": "📝", "label": "Staff application", "description": "Apply to join the server team"},
]


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return s[:40] or "ticket"


def parse_categories(raw: Optional[str]) -> List[Dict[str, str]]:
    """The saved categories, each with a stable `key`. Bad or empty data gives []."""
    try:
        rows = json.loads(raw) if raw else []
    except Exception:
        return []
    out, seen = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not str(r.get("label") or "").strip():
            continue
        key = slug(r["label"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "emoji": str(r.get("emoji") or "🎫")[:8], "label": str(r["label"]).strip()[:80],
                    "description": str(r.get("description") or "")[:100]})
        if len(out) >= MAX_CATEGORIES:
            break
    return out


def find_category(raw: Optional[str], key: Optional[str]) -> Optional[Dict[str, str]]:
    return next((c for c in parse_categories(raw) if c["key"] == key), None)


def parse_color(text: Optional[str]) -> Optional[int]:
    """'#5865F2' / '5865f2' -> int; None when it isn't a colour."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(text or "").strip())
    return int(m.group(1), 16) if m else None


def panel_look(cfg: dict, premium: bool) -> Dict[str, object]:
    """Title / text / colour of the panel. Custom values only apply while the server has Premium."""
    if premium:
        return {"title": (cfg.get("panel_title") or DEFAULT_TITLE)[:100],
                "text": (cfg.get("panel_text") or DEFAULT_TEXT)[:600],
                "color": parse_color(cfg.get("panel_color")) or DEFAULT_COLOR}
    return {"title": DEFAULT_TITLE, "text": DEFAULT_TEXT, "color": DEFAULT_COLOR}


def categories_line(cats: List[Dict[str, str]]) -> str:
    return " · ".join(c["label"] for c in cats)


def ticket_channel_name(user_name: str, category: Optional[Dict[str, str]] = None) -> str:
    base = f"{category['key']}-{slug(user_name)}" if category else f"ticket-{slug(user_name)}"
    return base[:90]


def is_inactive(last_activity_ts: float, now_ts: float, hours: Optional[int]) -> bool:
    return bool(hours) and (now_ts - last_activity_ts) >= int(hours) * 3600
