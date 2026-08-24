"""
Shared helper for getting/setting a Discord user's language preference and
translating a UI string in one call. Every Phase 3/4 cog (economy,
automation, automod, premium) should go through `get_lang()` + `tr()`
rather than hardcoding English strings — see i18n.py's module docstring for
how the underlying AI-translation cache works.

Reuses the same `users.language` column / `db.get_user_language()` /
`db.set_user_language()` that the Telegram side (handlers/language_handler.py)
already uses — language preference is per-user, not per-platform, so a
person who set French on the Telegram bot sees French on a Discord clone
too. clone_id is threaded through the same way every other per-clone query
in discord_bot/ is (None -> 0 on the main bot).
"""

import discord

from database import db
from i18n import tr as _tr, tr_sync as _tr_sync  # re-exported below

__all__ = ["get_lang", "tr", "tr_sync"]

tr = _tr
tr_sync = _tr_sync


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None) or 0


async def get_lang(interaction: discord.Interaction) -> str:
    """The interacting user's saved language code (e.g. 'en', 'fr'),
    defaulting to 'en' if they've never set one."""
    return await db.get_user_language(interaction.user.id, clone_id=_clone_id_of(interaction))
