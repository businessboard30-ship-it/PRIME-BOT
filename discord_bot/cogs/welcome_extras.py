# path: discord_bot/cogs/welcome_extras.py

"""
Welcome extras — the parts of the welcome system that sit around the card:

  * Goodbye message: a customizable post in a chosen channel when a member leaves.
  * Auto-role on join: one role for new members, a different one for bots.
  * Test posts: a REAL welcome card / goodbye message posted for the person who
    pressed the button, so an owner can see exactly what a new member will see
    without an alt account.

Config lives in discord_welcome_extras (db.get_welcome_extras / set_welcome_extras),
scoped by (guild_id, clone_id) like every other config. Settings are edited from
the Server Owners Panel (Welcome -> Goodbye & roles); there is no slash command,
so this costs no global command slot.

The welcome CARD itself is still posted by WelcomeCog.on_member_join. This cog only
adds listeners, so it can never double-post a card or change how cards render.
"""

import asyncio
import io
import logging
import time

import aiohttp
import discord
from discord.ext import commands

from database import db
from discord_bot import perm_check
from modules.welcome_card import render_welcome_card

logger = logging.getLogger(__name__)

DEFAULT_GOODBYE = "{name} has left {guild}. We are now {count} members."
GOODBYE_MAX = 300
TEST_COOLDOWN = 20.0                      # seconds between test posts per server
_last_test: dict = {}                     # (guild_id, clone_id) -> monotonic time


def _clone_of(client):
    return getattr(client, "clone_id", None)


def apply_goodbye(template: str, member: discord.abc.User, guild: discord.Guild) -> str:
    """{name} = display name, {member} = mention, {guild} = server name, {count} = members."""
    name = discord.utils.escape_markdown(discord.utils.escape_mentions(
        getattr(member, "display_name", None) or getattr(member, "name", "Someone")))
    return (
        (template or DEFAULT_GOODBYE)
        .replace("{name}", name)
        .replace("{member}", getattr(member, "mention", name))
        .replace("{guild}", guild.name)
        .replace("{count}", str(guild.member_count))
    )


def role_issue(guild: discord.Guild, role_id) -> str | None:
    """Plain-language reason a saved auto-role can't be handed out right now, or None."""
    if not role_id:
        return None
    role = guild.get_role(int(role_id))
    if role is None:
        return "that role no longer exists — pick another."
    problem = perm_check.role_problem(guild, role)
    if problem:
        return problem
    return None


def cooldown_left(guild_id: int, clone_id) -> float:
    last = _last_test.get((guild_id, clone_id))
    if last is None:
        return 0.0
    return max(0.0, TEST_COOLDOWN - (time.monotonic() - last))


def _mark_test(guild_id: int, clone_id) -> None:
    _last_test[(guild_id, clone_id)] = time.monotonic()


# ── test posts ───────────────────────────────────────────────────────────

async def send_test_welcome(bot, guild: discord.Guild, member: discord.Member) -> tuple:
    """Post a real welcome card for `member` exactly where a new member's card
    would go (the welcome channel, or the tester's DMs in DM delivery mode).
    Returns (ok, message). Never raises."""
    # Imported here so this module loads even if welcome.py is mid-reload.
    from discord_bot.cogs.welcome import (
        _apply_template, _custom_bg_bytes_for_render, _fetch_sticker_bytes,
    )
    clone_id = _clone_of(bot)
    config = await db.get_welcome_config(guild.id, clone_id=clone_id)
    dm_mode = config.get("delivery_mode") == "dm"
    channel = None
    if not dm_mode:
        if not config.get("channel_id"):
            return False, "Pick a welcome channel first — there's nowhere to post the test."
        channel = guild.get_channel(int(config["channel_id"]))
        if channel is None:
            return False, "The welcome channel no longer exists — pick it again."
        problem = perm_check.channel_problem(channel, guild.me)
        if problem:
            return False, problem

    left = cooldown_left(guild.id, clone_id)
    if left > 0:
        return False, f"Easy — try again in {int(left) + 1}s."
    _mark_test(guild.id, clone_id)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(str(member.display_avatar.replace(size=256).url),
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                avatar_bytes = await resp.read()
            sticker_bytes = await _fetch_sticker_bytes(session, config.get("sticker_url"))
            custom_bg_bytes = await _custom_bg_bytes_for_render(session, config, bot)
        card_bytes, image_format = await asyncio.to_thread(
            render_welcome_card,
            avatar_bytes, member.display_name, f"Member #{guild.member_count}",
            background_color=config["background_color"], accent_color=config["accent_color"],
            sticker_bytes=sticker_bytes, animate=(config.get("card_style") == "gif"),
            guild_name=guild.name, use_template=config.get("use_template", True),
            theme=config.get("card_theme", "wolf"), custom_background_bytes=custom_bg_bytes,
            ultra_options=config.get("ultra_card_json"),
        )
        ext = "gif" if image_format == "GIF" else "png"
        file = discord.File(fp=io.BytesIO(card_bytes), filename=f"welcome.{ext}")
        content = _apply_template(config["message_template"], member)
        content += f"\n-# 🧪 Test post — triggered by {member.display_name}, not a real join."
        if dm_mode:
            await member.send(content=content, file=file)
            return True, "Sent a test card to your DMs (welcome delivery is set to DM)."
        await channel.send(content=content, file=file,
                           allowed_mentions=discord.AllowedMentions(users=[member]))
        return True, f"Posted a test welcome card in {channel.mention}."
    except discord.Forbidden:
        if dm_mode:
            return False, "I couldn't DM you — open your DMs from this server and try again."
        return False, perm_check.channel_problem(channel, guild.me) or \
            f"I can't post in {channel.mention} — check that channel's permissions for me."
    except Exception:
        logger.exception("welcome test post failed for guild %s", guild.id)
        return False, "Couldn't render the test card — check the bot logs."


async def send_test_goodbye(bot, guild: discord.Guild, member: discord.Member) -> tuple:
    """Post a real goodbye message for `member` in the goodbye channel. Never raises."""
    clone_id = _clone_of(bot)
    extras = await db.get_welcome_extras(guild.id, clone_id=clone_id)
    channel = guild.get_channel(int(extras["goodbye_channel_id"])) if extras.get("goodbye_channel_id") else None
    if channel is None:
        return False, "Pick a goodbye channel first."
    problem = perm_check.channel_problem(channel, guild.me, ("view_channel", "send_messages", "embed_links"))
    if problem:
        return False, problem
    left = cooldown_left(guild.id, clone_id)
    if left > 0:
        return False, f"Easy — try again in {int(left) + 1}s."
    _mark_test(guild.id, clone_id)
    try:
        await channel.send(embed=build_goodbye_embed(extras, member, guild, test=True),
                           allowed_mentions=discord.AllowedMentions.none())
        return True, f"Posted a test goodbye in {channel.mention}."
    except discord.Forbidden:
        return False, perm_check.channel_problem(channel, guild.me, ("view_channel", "send_messages", "embed_links")) \
            or f"I can't post in {channel.mention}."
    except discord.HTTPException:
        logger.exception("goodbye test post failed for guild %s", guild.id)
        return False, "Discord rejected the post — try again in a moment."


def build_goodbye_embed(extras: dict, member: discord.abc.User, guild: discord.Guild, test: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title="👋 Goodbye",
        description=apply_goodbye(extras.get("goodbye_message"), member, guild),
        color=discord.Color.dark_grey(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if test:
        embed.set_footer(text="🧪 Test post — nobody actually left.")
    return embed


# ── cog ──────────────────────────────────────────────────────────────────

class WelcomeExtrasCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Auto-role: members get the member role, bots get the bot role."""
        clone_id = _clone_of(self.bot)
        try:
            extras = await db.get_welcome_extras(member.guild.id, clone_id=clone_id)
        except Exception:
            logger.exception("welcome extras lookup failed for guild %s", member.guild.id)
            return
        role_id = extras.get("bot_role_id") if member.bot else extras.get("member_role_id")
        if not role_id:
            return
        guild = member.guild
        role = guild.get_role(int(role_id))
        if role is None:
            perm_check.flag(guild.id, clone_id, "welcome_role",
                            "The auto-role I'm set to give new members was deleted — pick another in "
                            "`/serversetup` → Welcome → Goodbye & roles.")
            return
        problem = perm_check.role_problem(guild, role)
        if problem:
            perm_check.flag(guild.id, clone_id, "welcome_role", f"Auto-role isn't being given. {problem}")
            return
        try:
            await member.add_roles(role, reason="Welcome auto-role")
            perm_check.clear(guild.id, clone_id, "welcome_role")
        except discord.Forbidden:
            perm_check.flag(guild.id, clone_id, "welcome_role",
                            f"Auto-role isn't being given. {perm_check.forbidden_hint(guild, 'manage_roles')}")
        except discord.HTTPException:
            logger.warning("auto-role failed for %s in guild %s", member.id, guild.id, exc_info=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Goodbye message. Bots leaving are skipped."""
        if member.bot:
            return
        guild = member.guild
        clone_id = _clone_of(self.bot)
        try:
            extras = await db.get_welcome_extras(guild.id, clone_id=clone_id)
        except Exception:
            logger.exception("welcome extras lookup failed for guild %s", guild.id)
            return
        if not extras.get("goodbye_enabled") or not extras.get("goodbye_channel_id"):
            return
        channel = guild.get_channel(int(extras["goodbye_channel_id"]))
        if channel is None:
            perm_check.flag(guild.id, clone_id, "goodbye_post",
                            "The goodbye channel was deleted — pick another in "
                            "`/serversetup` → Welcome → Goodbye & roles.")
            return
        try:
            await channel.send(embed=build_goodbye_embed(extras, member, guild),
                               allowed_mentions=discord.AllowedMentions.none())
            perm_check.clear(guild.id, clone_id, "goodbye_post")
        except discord.Forbidden:
            perm_check.flag(guild.id, clone_id, "goodbye_post",
                            "Goodbye messages aren't posting. "
                            + (perm_check.channel_problem(channel, guild.me, ("view_channel", "send_messages", "embed_links"))
                               or f"I can't post in {channel.mention}."))
        except discord.HTTPException:
            logger.warning("goodbye post failed in guild %s", guild.id, exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeExtrasCog(bot))
