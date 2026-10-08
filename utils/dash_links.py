"""Links into the web dashboard (dashboard/ on Cloudflare Pages).

Sign-in is Discord OAuth, so these URLs carry no secret: anyone can open one, but the
dashboard only shows servers they can manage. The dashboard serves clone bots too: a clone's link is #/c/<clone_id>/g/<guild_id>, and the dashboard then
acts as that clone (its token, its settings, its Premium).
"""
from typing import Optional

import config


def dashboard_url(guild_id: Optional[int] = None, clone_id: Optional[int] = None) -> str:
    """Dashboard home, or straight into one server's overview (as a clone bot when clone_id is set)."""
    base = config.DASH_PAGES_URL
    if not guild_id:
        return f"{base}/"
    if clone_id:
        return f"{base}/#/c/{int(clone_id)}/g/{int(guild_id)}"
    return f"{base}/#/g/{int(guild_id)}"


def dashboard_supported(clone_id: Optional[int] = None) -> bool:
    """The dashboard serves the main bot and every clone bot."""
    return True


def dashboard_link_button(guild_id: Optional[int] = None, clone_id: Optional[int] = None, row: Optional[int] = None):
    """A 'Dashboard' link button."""
    import discord
    kwargs = {"label": "Open dashboard", "emoji": "🖥️", "style": discord.ButtonStyle.link, "url": dashboard_url(guild_id, clone_id)}
    if row is not None:
        kwargs["row"] = row
    return discord.ui.Button(**kwargs)
