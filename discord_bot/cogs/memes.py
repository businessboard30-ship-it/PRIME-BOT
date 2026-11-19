# path: discord_bot/cogs/memes.py

"""
Meme feed. One slash command, /meme, opens the setup panel (Manage Server); the same panel opens from the join DM's
"More features" screen. Everything runs from one persistent Components V2 panel: channel, how often, which meme sources
and an optional ping role.

Anyone with Manage Server can set it up and preview a meme for free. Turning on the automatic posting (and "Post now")
needs the server to have Premium. Settings are kept if Premium lapses; posting simply pauses.

Cost: one cheap query every 5 minutes (none if no server turned it on); one meme-api request per post.
Rules and the fetch live in modules/memes.py, storage in modules/meme_store.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from discord_bot.auto_create import create_role, create_text_channel, toggle_member_role
from discord_bot.cogs.birthdays import _Fixed, _admin, _clone, _is_premium, _premium_gate
from modules import meme_store as store
from modules import memes as mm

logger = logging.getLogger(__name__)

TICK_MINUTES = 5


def _cog(interaction: discord.Interaction):
    return interaction.client.get_cog("MemesCog")


def _can_post(channel, guild: discord.Guild) -> bool:
    me = guild.me
    if not channel or not me:
        return False
    p = channel.permissions_for(me)
    return bool(p.view_channel and p.send_messages and p.embed_links)


def meme_embed(m: dict) -> discord.Embed:
    e = discord.Embed(title=m["title"][:256], url=m["post"] or None, color=discord.Color.orange())
    e.set_image(url=m["url"])
    if m.get("sub"):
        e.set_footer(text=f"r/{m['sub']} · 👍 {m.get('ups', 0)}")
    return e


def _config_text(guild: discord.Guild, cfg: dict, premium: bool) -> str:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    role = guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
    subs = ", ".join(mm.PRESETS[s] for s in mm.subs_from_text(cfg.get("subs")))
    on = bool(cfg.get("enabled") and ch and premium)
    if cfg.get("enabled") and ch and not premium:
        status = "⏸️ Paused (the server needs 💎 Premium to post automatically)"
    else:
        status = "✅ On" if on else "⏸️ Off (pick a channel, then turn on)"
    return "\n".join([
        "## 😂 Meme setup",
        "Fresh memes posted in a channel on a schedule you pick. Setting up is free for everyone with Manage Server; "
        "💎 Premium turns on the automatic posting.",
        "",
        f"**Status:** {status}",
        f"**Channel:** {ch.mention if ch else 'not set'}",
        f"**Every:** {mm.valid_interval(cfg.get('interval_hours'))} hour(s)",
        f"**Sources:** {subs}",
        f"**Ping role:** {role.mention if role else 'none'}",
    ])


def build_setup_view(guild: discord.Guild, cfg: dict, premium: bool, note: Optional[str] = None) -> discord.ui.LayoutView:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    on = bool(cfg.get("enabled") and ch and premium)
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=discord.Color.orange() if on else discord.Color.dark_grey())
    box.add_item(discord.ui.TextDisplay(_config_text(guild, cfg, premium) + (f"\n-# {note}" if note else "")))
    box.add_item(discord.ui.Separator())
    box.add_item(discord.ui.ActionRow(PickChannelSelect()))
    box.add_item(discord.ui.ActionRow(IntervalSelect(mm.valid_interval(cfg.get("interval_hours")))))
    box.add_item(discord.ui.ActionRow(SourcesSelect(mm.subs_from_text(cfg.get("subs")))))
    box.add_item(discord.ui.ActionRow(PingRoleSelect()))
    box.add_item(discord.ui.ActionRow(ToggleButton(bool(cfg.get("enabled")), premium), PreviewButton(), PostNowButton(premium)))
    box.add_item(discord.ui.ActionRow(MakeChannelButton(), MakeRoleButton()))
    view.add_item(box)
    return view


async def _state(interaction: discord.Interaction):
    cfg = await store.get_config(interaction.guild.id, _clone(interaction))
    return cfg, await _is_premium(interaction.guild.id, _clone(interaction))


async def _save(interaction: discord.Interaction, note: Optional[str] = None, **fields):
    guild, clone_id = interaction.guild, _clone(interaction)
    cfg = await store.set_config(guild.id, clone_id, **fields)
    premium = await _is_premium(guild.id, clone_id)
    await interaction.response.edit_message(view=build_setup_view(guild, cfg, premium, note=note))


# ── persistent items (fixed custom_ids) ──────────────────────────────────

class PickChannelSelect(_Fixed, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=r"^meme:channel$"):
    def __init__(self):
        super().__init__(discord.ui.ChannelSelect(
            custom_id="meme:channel", channel_types=[discord.ChannelType.text], placeholder="Channel for memes"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        channel = interaction.guild.get_channel(self.item.values[0].id)
        if not _can_post(channel, interaction.guild):
            return await interaction.response.send_message(
                f"I need **View Channel, Send Messages** and **Embed Links** in "
                f"{channel.mention if channel else 'that channel'}. Fix its permissions or pick another one.", ephemeral=True)
        await _save(interaction, channel_id=channel.id)


class IntervalSelect(_Fixed, discord.ui.DynamicItem[discord.ui.Select], template=r"^meme:interval$"):
    def __init__(self, current: int = mm.DEFAULT_INTERVAL):
        super().__init__(discord.ui.Select(
            custom_id="meme:interval", placeholder="How often",
            options=[discord.SelectOption(label=f"Every {h} hour" + ("s" if h != 1 else ""), value=str(h),
                                          default=(h == current)) for h in mm.INTERVALS]))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        await _save(interaction, interval_hours=mm.valid_interval(self.item.values[0]))


class SourcesSelect(_Fixed, discord.ui.DynamicItem[discord.ui.Select], template=r"^meme:subs$"):
    def __init__(self, current=None):
        current = set(current or mm.DEFAULT_SUBS)
        super().__init__(discord.ui.Select(
            custom_id="meme:subs", placeholder="Meme sources", min_values=1, max_values=len(mm.PRESETS),
            options=[discord.SelectOption(label=label, value=key, default=(key in current)) for key, label in mm.PRESETS.items()]))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        await _save(interaction, subs=",".join(mm.clean_subs(self.item.values)))


class PingRoleSelect(_Fixed, discord.ui.DynamicItem[discord.ui.RoleSelect], template=r"^meme:role$"):
    def __init__(self):
        super().__init__(discord.ui.RoleSelect(
            custom_id="meme:role", placeholder="Role to ping on each meme (optional)", min_values=0, max_values=1))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        role = self.item.values[0] if self.item.values else None
        await _save(interaction, ping_role_id=role.id if role else None)


class ToggleButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^meme:toggle$"):
    def __init__(self, enabled: bool = False, premium: bool = True):
        super().__init__(discord.ui.Button(
            label="Turn off" if enabled else ("Turn on" if premium else "Turn on 💎"),
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success, custom_id="meme:toggle"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        cfg, _ = await _state(interaction)               # the live value, not whatever the label said
        if cfg.get("enabled"):
            return await _save(interaction, enabled=False)   # turning off is always allowed
        if not cfg.get("channel_id"):
            return await interaction.response.send_message("Pick a channel first.", ephemeral=True)
        ch = interaction.guild.get_channel(cfg["channel_id"])
        if not _can_post(ch, interaction.guild):
            return await interaction.response.send_message(
                "I can't post in that channel any more. Pick one where I have **Send Messages** and **Embed Links**.",
                ephemeral=True)
        if not await _premium_gate(interaction, interaction.guild, _clone(interaction)):
            return                                        # activation is Premium; setup stays free
        await _save(interaction, note="Memes are on. The first one is posted within a few minutes.", enabled=True)
        cog = _cog(interaction)
        if cog is not None:
            asyncio.create_task(cog.post_now(interaction.guild.id))


class PreviewButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^meme:preview$"):
    """Free: shows one meme only to the person who pressed it."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Preview", emoji="👀", style=discord.ButtonStyle.secondary,
                                           custom_id="meme:preview"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        cfg, _ = await _state(interaction)
        m = await mm.fetch_meme(mm.subs_from_text(cfg.get("subs")))
        if not m:
            return await interaction.followup.send("I couldn't fetch a meme just now. Try again in a minute.", ephemeral=True)
        await interaction.followup.send(embed=meme_embed(m), ephemeral=True)


class PostNowButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^meme:postnow$"):
    def __init__(self, premium: bool = True):
        super().__init__(discord.ui.Button(label="Post now" + ("" if premium else " 💎"), emoji="🎲",
                                           style=discord.ButtonStyle.primary, custom_id="meme:postnow"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        cfg, _ = await _state(interaction)
        if not cfg.get("channel_id"):
            return await interaction.response.send_message("Pick a channel first.", ephemeral=True)
        if not await _premium_gate(interaction, interaction.guild, _clone(interaction)):
            return
        await interaction.response.defer(ephemeral=True)
        cog = _cog(interaction)
        ok = await cog.post_now(interaction.guild.id, force=True) if cog is not None else False
        await interaction.followup.send("🎲 Posted." if ok else "I couldn't post a meme just now. Check my channel "
                                        "permissions and try again.", ephemeral=True)


class MakeChannelButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^meme:mkchannel$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Create channel", emoji="➕", style=discord.ButtonStyle.secondary,
                                           custom_id="meme:mkchannel"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        ch, err = await create_text_channel(interaction.guild, "memes", "Fresh memes, posted automatically. 😂", near=interaction.channel)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await _save(interaction, note=f"Channel {ch.mention} is ready.", channel_id=ch.id)


class MakeRoleButton(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^meme:mkrole$"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Create role", emoji="➕", style=discord.ButtonStyle.secondary,
                                           custom_id="meme:mkrole"))

    async def callback(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        role, err = await create_role(interaction.guild, "Meme Fans", discord.Color.orange())
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await _save(interaction, note=f"Role {role.mention} is ready. Members can opt in from the button under each meme.",
                    ping_role_id=role.id)


class MemeRoleToggle(_Fixed, discord.ui.DynamicItem[discord.ui.Button], template=r"^meme:rolebtn$"):
    """Under each meme when a ping role is set: members opt in or out of the pings themselves."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Meme pings", emoji="🔔", style=discord.ButtonStyle.secondary,
                                           custom_id="meme:rolebtn"))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        cfg = await store.get_config(interaction.guild.id, _clone(interaction))
        role = interaction.guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
        if role is None:
            return await interaction.response.send_message("The meme role no longer exists. An admin can set it up again.", ephemeral=True)
        has, err = await toggle_member_role(interaction.user, role)
        await interaction.response.send_message(err or (f"✅ You'll get pinged for memes ({role.mention})." if has else "You won't be pinged any more."),
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


DYNAMIC_ITEMS = (PickChannelSelect, IntervalSelect, SourcesSelect, PingRoleSelect, ToggleButton, PreviewButton, PostNowButton,
                 MakeChannelButton, MakeRoleButton, MemeRoleToggle)


# ── the cog ─────────────────────────────────────────────────────────────

class MemesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clone_id: Optional[int] = getattr(bot, "clone_id", None)
        self._seen: Dict[int, deque] = {}                # guild id -> recent meme urls (no repeats)
        self._tick.start()

    def cog_unload(self):
        self._tick.cancel()

    @app_commands.command(name="meme", description="Set up automatic memes for this server (Manage Server)")
    @app_commands.guild_only()
    async def meme(self, interaction: discord.Interaction):
        if not await _admin(interaction):
            return
        cfg, premium = await _state(interaction)
        await interaction.response.send_message(view=build_setup_view(interaction.guild, cfg, premium), ephemeral=True)

    async def post_now(self, guild_id: int, force: bool = False) -> bool:
        """Posts one meme for this server. Premium only. Respects the schedule unless force."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        cfg = await store.get_config(guild_id, self.clone_id)
        ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
        if not _can_post(ch, guild) or not await _is_premium(guild_id, self.clone_id):
            return False
        seen = self._seen.setdefault(guild_id, deque(maxlen=40))
        m = await mm.fetch_meme(mm.subs_from_text(cfg.get("subs")), set(seen))
        if not m:
            return False
        role = guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
        try:
            view = None
            if role:
                view = discord.ui.View(timeout=None)
                view.add_item(MemeRoleToggle())
            await ch.send(content=role.mention if role else None, embed=meme_embed(m), view=view,
                          allowed_mentions=discord.AllowedMentions(roles=[role] if role else False, users=False, everyone=False))
        except (discord.Forbidden, discord.HTTPException):
            logger.info("[memes] couldn't post in guild %s", guild_id)
            return False
        seen.append(m["url"])
        await store.mark_posted(guild_id, self.clone_id)
        return True

    @tasks.loop(minutes=TICK_MINUTES)
    async def _tick(self):
        try:
            now = datetime.now(timezone.utc)
            for cfg in await store.enabled_configs(self.clone_id):
                last = cfg.get("last_post_at")
                gap = timedelta(hours=mm.valid_interval(cfg.get("interval_hours")))
                if last is not None and now - last < gap - timedelta(minutes=1):
                    continue
                await self.post_now(cfg["guild_id"])
                await asyncio.sleep(1)
        except Exception:
            logger.exception("[memes] posting loop failed")

    @_tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(MemesCog(bot))
