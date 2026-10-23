"""Terms & Privacy wizard: when the bot joins a server it posts a message asking a server admin to accept them.

Acceptance is recorded once per server and bot (main bot or clone). Nothing is blocked while it is pending; the wizard
only asks, and Decline lets the admin remove the bot. Best-effort everywhere: a server with no postable channel is
skipped, and a failure never stops the rest of the join flow."""
from __future__ import annotations

import logging
from typing import Optional

import discord

import config

logger = logging.getLogger(__name__)

TERMS_VERSION = "2026-10-12"          # bump when the Terms or Privacy Policy change in a way people should re-accept
GREEN = discord.Color.from_rgb(46, 204, 113)
BLUE = discord.Color.from_rgb(88, 101, 242)


def terms_url() -> str:
    return f"{config.WEBSITE_URL}/terms/"


def privacy_url() -> str:
    return f"{config.WEBSITE_URL}/privacy/"


def _key(clone_id) -> int:
    return -1 if clone_id is None else int(clone_id)


async def _pool():
    from database import get_pool
    return await get_pool()


async def get_acceptance(guild_id: int, clone_id) -> Optional[dict]:
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT accepted_by, accepted_at, terms_version FROM guild_terms_acceptance "
            "WHERE guild_id = $1 AND clone_key = $2", guild_id, _key(clone_id))
    return dict(row) if row else None


async def accept(guild_id: int, clone_id, user_id: int) -> tuple:
    """(newly_accepted, row). Two admins tapping together: the first wins, the second sees who accepted."""
    pool = await _pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO guild_terms_acceptance (guild_id, clone_id, accepted_by, terms_version) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT DO NOTHING RETURNING accepted_by, accepted_at, terms_version",
            guild_id, clone_id, user_id, TERMS_VERSION)
        if row is not None:
            return True, dict(row)
        row = await conn.fetchrow(
            "SELECT accepted_by, accepted_at, terms_version FROM guild_terms_acceptance WHERE guild_id = $1 AND clone_key = $2",
            guild_id, _key(clone_id))
    return False, dict(row) if row else {}


def can_decide(member) -> bool:
    """Accept or decline for the whole server: the owner, an administrator or anyone with Manage Server."""
    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    guild = getattr(member, "guild", None)
    return bool(perms.manage_guild or perms.administrator or (guild is not None and guild.owner_id == member.id))


def pick_channel(guild: discord.Guild) -> Optional[discord.abc.Messageable]:
    """The system channel if the bot can post there, otherwise the first text channel it can post an embed in."""
    me = guild.me
    channels = ([guild.system_channel] if guild.system_channel else []) + list(guild.text_channels)
    for ch in channels:
        try:
            perms = ch.permissions_for(me)
        except Exception:
            logger.debug("swallowed exception in pick_channel", exc_info=True)
            continue
        if perms.view_channel and perms.send_messages and perms.embed_links:
            return ch
    return None


def build_embed(guild_name: str, accepted: Optional[dict] = None) -> discord.Embed:
    if accepted:
        e = discord.Embed(
            title="✅ Terms and Privacy Policy accepted", color=GREEN,
            description=(f"Accepted for **{guild_name}** by <@{accepted['accepted_by']}> on "
                         f"<t:{int(accepted['accepted_at'].timestamp())}:D>. Thanks, you're all set."))
    else:
        e = discord.Embed(
            title="📜 One quick thing: Terms and Privacy", color=BLUE,
            description=(f"Thanks for adding me to **{guild_name}**! Before you start, a server admin needs to accept my "
                         f"[Terms of Service]({terms_url()}) and [Privacy Policy]({privacy_url()}).\n\n"
                         "**The short version**\n"
                         "• I read member joins and messages only to run features you turn on: welcome, verification, XP, "
                         "auto-moderation.\n"
                         "• I don't sell data, and I don't keep message text beyond what the privacy page says.\n"
                         "• You can ask for your data to be deleted at any time.\n\n"
                         "Only someone with **Manage Server** can accept or decline for this server."))
    e.set_footer(text=f"Terms version {TERMS_VERSION}")
    return e


async def post_on_join(bot, guild: discord.Guild) -> bool:
    """Posts the wizard in the server. True when it was posted. Never raises."""
    from discord_bot.cogs._views_terms import build_view
    try:
        clone_id = getattr(bot, "clone_id", None)
        if await get_acceptance(guild.id, clone_id):
            return False                                   # re-adding the bot to a server that already accepted
        channel = pick_channel(guild)
        if channel is None:
            logger.info("[terms] no postable channel in guild %s", guild.id)
            return False
        await channel.send(embed=build_embed(guild.name), view=build_view(guild.id))
        return True
    except (discord.HTTPException, discord.Forbidden) as e:
        logger.info("[terms] couldn't post the wizard in guild %s: %s", guild.id, e)
    except Exception:
        logger.exception("[terms] posting the wizard failed for guild %s", guild.id)
    return False
