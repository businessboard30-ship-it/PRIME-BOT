"""One-tap channel and role creation for the setup panels (memes, movies). Never raises: returns (object, error text)."""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import discord

logger = logging.getLogger(__name__)


async def create_text_channel(guild: discord.Guild, name: str, topic: str, near: Optional[discord.abc.GuildChannel] = None,
                              reason: str = "Created from the bot's setup panel", read_only: bool = False) -> Tuple[Optional[discord.TextChannel], Optional[str]]:
    """New text channel (put under the same category as `near`). Reuses an existing channel of the same name."""
    existing = discord.utils.get(guild.text_channels, name=name)
    if existing is not None:
        return existing, None
    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return None, "I need the **Manage Channels** permission to create a channel. Give me it, or pick an existing channel."
    category = getattr(near, "category", None)
    overwrites = {}
    if read_only:    # members can read and press buttons, only the bot posts
        overwrites = {guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False, create_public_threads=False),
                      me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)}
    try:
        ch = await guild.create_text_channel(name, topic=topic[:1000], category=category, overwrites=overwrites, reason=reason)
        return ch, None
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.info("[auto-create] channel failed in %s: %s", guild.id, type(e).__name__)
        return None, "I couldn't create the channel (a channel limit or my permissions). Pick an existing one instead."


async def create_role(guild: discord.Guild, name: str, color: discord.Color,
                      reason: str = "Created from the bot's setup panel") -> Tuple[Optional[discord.Role], Optional[str]]:
    """New mentionable role. Reuses an existing role of the same name."""
    existing = discord.utils.get(guild.roles, name=name)
    if existing is not None:
        return existing, None
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None, "I need the **Manage Roles** permission to create a role. Give me it, or pick an existing role."
    try:
        return await guild.create_role(name=name, color=color, mentionable=True, reason=reason), None
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.info("[auto-create] role failed in %s: %s", guild.id, type(e).__name__)
        return None, "I couldn't create the role (the server's role limit or my permissions). Pick an existing one instead."


async def toggle_member_role(member: discord.Member, role: discord.Role) -> Tuple[Optional[bool], Optional[str]]:
    """Adds the role if the member lacks it, removes it if they have it. Returns (now_has_role, error)."""
    me = member.guild.me
    if me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
        return None, "I can't hand out that role. An admin needs to move my role above it and give me **Manage Roles**."
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Self-service toggle")
            return False, None
        await member.add_roles(role, reason="Self-service toggle")
        return True, None
    except (discord.Forbidden, discord.HTTPException):
        return None, "I couldn't change your roles just now."


async def create_category(guild: discord.Guild, name: str,
                          reason: str = "Created from the bot's setup panel") -> Tuple[Optional[discord.CategoryChannel], Optional[str]]:
    existing = discord.utils.get(guild.categories, name=name)
    if existing is not None:
        return existing, None
    me = guild.me
    if me is None or not me.guild_permissions.manage_channels:
        return None, "I need the **Manage Channels** permission to create a category. Give me it, or pick an existing one."
    try:
        return await guild.create_category(name, reason=reason), None
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.info("[auto-create] category failed in %s: %s", guild.id, type(e).__name__)
        return None, "I couldn't create the category (a channel limit or my permissions). Pick an existing one instead."
