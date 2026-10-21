"""The /cat menu (a small wizard): what the first card says and which choices it offers. Pure, no Discord or database.

/cat and the Menu button on every spawn open the same card, so a cat that just appeared and /cat are one flow: the card
shows when the next cat comes, links to a cat that is waiting to be adopted, and lists everything the game can do.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

# (key, emoji, label, one short line shown under the label in the menu). The key picks the button in _views_cat_hub.
MENU: List[Tuple[str, str, str, str]] = [
    ("mine", "🏠", "My cats", "Rename, rank up, trade, gift"),
    ("care", "💖", "Daily care", "Keep your streak going"),
    ("life", "🌸", "Cat Life", "Coins, walks, heists, spin"),
    ("arena", "⚔️", "Arena", "Fight players, only if both agree"),
    ("home", "🏡", "Home", "Buy a home, keep a vault"),
    ("quests", "🎯", "Quests", "Today's small goals"),
    ("market", "🏷️", "Market", "Buy and sell cats"),
    ("board", "🏆", "Leaderboard", "Top collectors and richest"),
    ("profile", "📖", "Profile", "Your bio, pins and badges"),
    ("badges", "🏅", "Badges", "What you've earned"),
    ("wishlist", "🔎", "Wishlist", "Cats you're hoping for"),
    ("cosmetics", "✨", "Cosmetics", "Make your cats look great"),
    ("help", "❓", "Help", "Short guide, one topic at a time"),
]

INTRO = "Pick what you want to do from the menu. Everything here is just for fun, play coins only."


def status_lines(in_server: bool, enabled: bool, next_ts: Optional[int], waiting_url: Optional[str]) -> List[str]:
    """The lines about this server's cats: when the next one comes, and a link to one that is waiting."""
    if not in_server:
        return []
    lines: List[str] = []
    if waiting_url:
        lines.append(f"🐾 **A cat is waiting!** [Adopt it here]({waiting_url})")
    if not enabled:
        lines.append("⏰ Cats don't appear here yet. A server manager can turn them on with **/catsetup**.")
    elif next_ts:
        lines.append(f"⏰ **Next cat:** around <t:{int(next_ts)}:t> (<t:{int(next_ts)}:R>)")
    else:
        lines.append("⏰ **Next cat:** coming up soon.")
    return lines


def jump_url(guild_id: int, spawn: Optional[dict]) -> Optional[str]:
    """Discord link to the spawn message, or None when there isn't one."""
    if not spawn or not spawn.get("channel_id") or not spawn.get("message_id"):
        return None
    return f"https://discord.com/channels/{int(guild_id)}/{int(spawn['channel_id'])}/{int(spawn['message_id'])}"


def card_text(lines: List[str]) -> str:
    return INTRO if not lines else "\n".join(lines) + "\n\n" + INTRO
