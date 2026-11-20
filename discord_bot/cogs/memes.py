# path: discord_bot/cogs/memes.py

"""
Meme feed. One slash command, /meme, opens a private setup wizard (Manage Server) built like the anime spawn wizard: channel,
how often, sources, optional ping role, ⚡ Quick setup, create-channel / create-role buttons, Preview, test post and on/off. The
same wizard opens from the join DM's "More features" screen. Nothing happens in a server until it is turned on.

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


ACCENT = discord.Color.orange()
AUTO_CHANNEL = "memes"
AUTO_ROLE = "Meme Fans"
OOPS = "😿 Something went wrong saving that. Try again."


def _config_text(guild: discord.Guild, cfg: dict, premium: bool, note: Optional[str] = None) -> str:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    role = guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
    subs = ", ".join(mm.PRESETS[x] for x in mm.subs_from_text(cfg.get("subs")))
    on = bool(cfg.get("enabled") and ch)
    if on and not premium:
        status = "PAUSED (the server needs 💎 Premium to post automatically)"
    else:
        status = "ON" if on else "OFF (asleep)"
    parts = [note, ""] if note else []
    parts += ["## 😂 Memes",
              "Pick a channel (or let me create one), choose how often a meme is posted, and turn it on. **Quick setup** does all of it in one tap. "
              "Memes come from Reddit through meme-api, never NSFW, and the same one never repeats. Until you set this up, I do nothing in your server.", "",
              f"**Channel:** {ch.mention if ch else 'not picked yet'}",
              f"**Posts:** every {mm.valid_interval(cfg.get('interval_hours'))} hour(s)",
              f"**Sources:** {subs}",
              f"**Ping role:** {role.mention if role else 'none (optional)'}",
              f"**Posting:** {status}",
              "-# Setting up is free. 💎 Premium turns on the automatic posting. Members can opt in to pings with the 🔔 button under each meme."]
    return "\n".join(parts)


def _btn(label, cb, style=discord.ButtonStyle.secondary, emoji=None, disabled=False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, style=style, emoji=emoji, disabled=disabled)
    b.callback = cb
    return b


class MemeSetupView(discord.ui.LayoutView):
    """Private, owner-locked setup wizard (same model as the anime spawn wizard). Nothing is posted or turned on until the
    person presses Quick setup or Turn on. It times out into a plain summary, so no live panel is left behind."""

    def __init__(self, guild: discord.Guild, clone_id, owner_id: int, cfg: dict, premium: bool):
        super().__init__(timeout=900)
        self.guild, self.clone_id, self.owner_id, self.cfg, self.premium = guild, clone_id, owner_id, cfg, premium
        self.note: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            try:
                await interaction.response.send_message("😂 This setup belongs to the person who opened it.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        ready = bool(self.cfg.get("channel_id"))
        sel = discord.ui.ChannelSelect(channel_types=[discord.ChannelType.text], placeholder="Pick the meme channel")
        sel.callback = self._on_channel
        pace = discord.ui.Select(placeholder="How often should a meme be posted?", options=[
            discord.SelectOption(label=f"Every {h} hour" + ("s" if h != 1 else ""), value=str(h),
                                 default=(h == mm.valid_interval(self.cfg.get("interval_hours")))) for h in mm.INTERVALS])
        pace.callback = self._on_interval
        cur = set(mm.subs_from_text(self.cfg.get("subs")))
        src = discord.ui.Select(placeholder="Where do the memes come from?", min_values=1, max_values=len(mm.PRESETS), options=[
            discord.SelectOption(label=label, value=key, default=(key in cur)) for key, label in mm.PRESETS.items()])
        src.callback = self._on_sources
        role = discord.ui.RoleSelect(placeholder="Optional: a role to ping on every meme", min_values=0, max_values=1)
        role.callback = self._on_role
        on = bool(self.cfg.get("enabled"))
        rows = [discord.ui.ActionRow(sel), discord.ui.ActionRow(pace), discord.ui.ActionRow(src), discord.ui.ActionRow(role),
                discord.ui.ActionRow(
                    _btn("Quick setup", self._on_quick, discord.ButtonStyle.success, "⚡"),
                    _btn("Create channel", self._on_create_channel, emoji="➕"),
                    _btn("Create ping role", self._on_create_role, emoji="🎭")),
                discord.ui.ActionRow(
                    _btn("Preview", self._on_preview, discord.ButtonStyle.primary, "👀"),
                    _btn("Post a test", self._on_test, emoji="🧪", disabled=not ready),
                    _btn("Turn off" if on else "Turn on", self._on_toggle,
                         discord.ButtonStyle.danger if on else discord.ButtonStyle.success, disabled=not ready),
                    _btn("Close", self._on_close))]
        box = discord.ui.Container(accent_colour=ACCENT)
        box.add_item(discord.ui.TextDisplay(_config_text(self.guild, self.cfg, self.premium, self.note)[:3900]))
        for r in rows:
            box.add_item(r)
        self.add_item(box)

    async def _push(self, interaction: discord.Interaction) -> None:
        self._build()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def _save(self, **fields) -> bool:
        try:
            self.cfg = await store.set_config(self.guild.id, self.clone_id, **fields)
            return True
        except Exception:
            logger.exception("[memes] couldn't save settings")
            self.note = OOPS
            return False

    async def _need_premium(self, interaction: discord.Interaction) -> bool:
        """Activation is Premium. Shows the normal Go Premium checkout when the server doesn't have it."""
        self.premium = await _is_premium(self.guild.id, self.clone_id)
        return await _premium_gate(interaction, self.guild, self.clone_id)

    async def _on_channel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ch = self.guild.get_channel(int(interaction.data["values"][0]))
        if not _can_post(ch, self.guild):
            self.note = f"🚫 I need **View Channel, Send Messages** and **Embed Links** in {ch.mention if ch else 'that channel'}."
        elif await self._save(channel_id=ch.id):
            self.note = f"✅ Meme channel: {ch.mention}."
        await self._push(interaction)

    async def _on_interval(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if await self._save(interval_hours=mm.valid_interval(interaction.data["values"][0])):
            self.note = f"✅ A meme every {self.cfg['interval_hours']} hour(s)."
        await self._push(interaction)

    async def _on_sources(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if await self._save(subs=",".join(mm.clean_subs(interaction.data["values"]))):
            self.note = "✅ Sources updated."
        await self._push(interaction)

    async def _on_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vals = interaction.data.get("values") or []
        role = self.guild.get_role(int(vals[0])) if vals else None
        if await self._save(ping_role_id=role.id if role else None):
            self.note = f"✅ I'll ping {role.mention} on every meme." if role else "Ping role cleared."
        await self._push(interaction)

    async def _make_channel(self) -> Optional[discord.TextChannel]:
        ch, err = await create_text_channel(self.guild, AUTO_CHANNEL, "Fresh memes, posted automatically. 😂")
        if err:
            self.note = "🚫 " + err
        return ch

    async def _on_create_channel(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ch = await self._make_channel()
        if ch is not None and await self._save(channel_id=ch.id):
            self.note = f"✅ Channel ready: {ch.mention}."
        await self._push(interaction)

    async def _on_create_role(self, interaction: discord.Interaction):
        await interaction.response.defer()
        role, err = await create_role(self.guild, AUTO_ROLE, discord.Color.orange())
        if err:
            self.note = "🚫 " + err
        elif await self._save(ping_role_id=role.id):
            self.note = f"✅ I'll ping {role.mention} on every meme. Members opt in with the 🔔 button under each meme."
        await self._push(interaction)

    async def _on_quick(self, interaction: discord.Interaction):
        """Channel (kept if already picked, otherwise created) + the schedule already shown + switched on, in one tap. Premium."""
        if not await self._need_premium(interaction):
            return
        await interaction.response.defer()
        ch = self.guild.get_channel(self.cfg["channel_id"]) if self.cfg.get("channel_id") else None
        if not _can_post(ch, self.guild):
            ch = await self._make_channel()
        if ch is not None and await self._save(channel_id=ch.id, enabled=True):
            self.note = (f"⚡ All set. Memes are ON in {ch.mention}, every {self.cfg['interval_hours']} hour(s). "
                         "The first one is on its way. Change anything below any time.")
            cog = interaction.client.get_cog("MemesCog")
            if cog is not None:
                asyncio.create_task(cog.post_now(self.guild.id, force=True))
        await self._push(interaction)

    async def _on_preview(self, interaction: discord.Interaction):
        await interaction.response.defer()
        m = await mm.fetch_meme(mm.subs_from_text(self.cfg.get("subs")))
        if m:
            await interaction.followup.send(embed=meme_embed(m), ephemeral=True)
        else:
            self.note = "😿 I can't reach the meme service right now. Try again in a minute."
            await self._push(interaction)

    async def _on_test(self, interaction: discord.Interaction):
        if not await self._need_premium(interaction):
            return
        await interaction.response.defer()
        cog = interaction.client.get_cog("MemesCog")
        ok = await cog.post_now(self.guild.id, force=True) if cog is not None else False
        self.note = "🧪 Test meme posted." if ok else "🚫 I couldn't post a test. Check my permissions in that channel."
        await self._push(interaction)

    async def _on_toggle(self, interaction: discord.Interaction):
        if self.cfg.get("enabled"):
            await interaction.response.defer()
            if await self._save(enabled=False):
                self.note = "⏸️ Memes are OFF."
            return await self._push(interaction)
        if not await self._need_premium(interaction):
            return
        await interaction.response.defer()
        if await self._save(enabled=True):
            self.note = "✅ Memes are ON. The first one comes after one interval."
        await self._push(interaction)

    async def _on_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=_final_view(_config_text(self.guild, self.cfg, self.premium)))

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=_final_view(_config_text(self.guild, self.cfg, self.premium)))
            except discord.HTTPException:
                pass


def _final_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    box = discord.ui.Container(accent_colour=ACCENT)
    box.add_item(discord.ui.TextDisplay(text[:3900]))
    view.add_item(box)
    return view


async def open_setup(bot, guild: discord.Guild, clone_id, owner_id: int, channel) -> discord.Message:
    """Posts the wizard in `channel` for `owner_id` (used from the join DM). Raises discord.HTTPException if it can't post."""
    cfg = await store.get_config(guild.id, clone_id)
    view = MemeSetupView(guild, clone_id, owner_id, cfg, await _is_premium(guild.id, clone_id))
    msg = await channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg
    return msg


async def open_setup_ephemeral(interaction: discord.Interaction) -> None:
    cfg = await store.get_config(interaction.guild.id, _clone(interaction))
    view = MemeSetupView(interaction.guild, _clone(interaction), interaction.user.id, cfg,
                         await _is_premium(interaction.guild.id, _clone(interaction)))
    await interaction.response.send_message(view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass


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


DYNAMIC_ITEMS = (MemeRoleToggle,)


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
        await open_setup_ephemeral(interaction)

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
