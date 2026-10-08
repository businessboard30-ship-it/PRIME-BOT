"""Links into the web dashboard (dashboard/ on Cloudflare Pages).

Sign-in is Discord OAuth, so these URLs carry no secret: anyone can open one, but the
dashboard only shows servers they can manage. The dashboard does not support clone bots
yet, so callers pass clone_id and fall back to the legacy link for those.
"""
from typing import Optional

import config


def dashboard_url(guild_id: Optional[int] = None) -> str:
    """Dashboard home, or straight into one server's overview."""
    base = config.DASH_PAGES_URL
    return f"{base}/#/g/{int(guild_id)}" if guild_id else f"{base}/"


def dashboard_supported(clone_id: Optional[int]) -> bool:
    return clone_id is None


def dashboard_link_button(guild_id: Optional[int] = None, clone_id: Optional[int] = None, row: Optional[int] = None):
    """A 'Dashboard' link button, or None when the dashboard can't serve this bot (clones)."""
    if not dashboard_supported(clone_id):
        return None
    import discord
    kwargs = {"label": "Open dashboard", "emoji": "🖥️", "style": discord.ButtonStyle.link, "url": dashboard_url(guild_id)}
    if row is not None:
        kwargs["row"] = row
    return discord.ui.Button(**kwargs)
