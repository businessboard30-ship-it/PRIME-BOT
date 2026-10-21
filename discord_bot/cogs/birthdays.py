# path: discord_bot/cogs/birthdays.py

"""
Birthdays.

ONE slash command: /birthday. It opens a private wizard with buttons:
  Set my birthday | My birthday | Remove | Upcoming | Server setup (Manage Server only)
Server setup lets admins pick the channel, optional ping role, message, time zone / hour,
turn it on or off and send a test.

A background loop checks once every 10 minutes with ONE query (enabled servers only) and posts
at most once per server per local day. A birthday is stored as month + day only (no year).
Birthdays are global per user, so a member sets theirs once and it works in every server
that has the feature on.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules import birthday_store as store
from modules import birthdays as bd

logger = logging.getLogger(__name__)

def _can_post(channel, guild: discord.Guild) -> bool:
    me = guild.me
    return bool(channel and me and channel.permissions_for(me).send_messages
                and channel.permissions_for(me).embed_links)


def _config_embed(guild: discord.Guild, cfg: dict) -> discord.Embed:
    ch = guild.get_channel(cfg["channel_id"]) if cfg.get("channel_id") else None
    role = guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
    on = bool(cfg.get("enabled") and ch)
    embed = discord.Embed(
        title="🎂 Birthday setup",
        description="Members add their birthday with `/birthday`. "
                    "I announce it once on the day, in the channel you pick.",
        color=discord.Color.green() if on else discord.Color.dark_grey(),
    )
    embed.add_field(name="Status", value="✅ On" if on else "⏸️ Off (pick a channel, then turn on)", inline=True)
    embed.add_field(name="Channel", value=ch.mention if ch else "not set", inline=True)
    embed.add_field(name="Ping role", value=role.mention if role else "none", inline=True)
    off = int(cfg.get("utc_offset_hours") or 0)
    embed.add_field(name="Announce at", value=f"{int(cfg.get('announce_hour', 9)):02d}:00 (UTC{off:+d})", inline=True)
    embed.add_field(name="Message", value=(cfg.get("message") or bd.DEFAULT_MESSAGE)[:500], inline=False)
    embed.set_footer(text="Placeholders: {user} {name} {server}")
    return embed


# ---------------------------------------------------------------------------- setup panel

class _MessageModal(discord.ui.Modal, title="Birthday message"):
    text = discord.ui.TextInput(
        label="Message ({user} {name} {server})", style=discord.TextStyle.paragraph,
        max_length=bd.MAX_MESSAGE_LEN, required=False,
        placeholder="Leave empty to use the default message",
    )

    def __init__(self, panel: "SetupView"):
        super().__init__()
        self.panel = panel
        self.text.default = panel.cfg.get("message") or ""

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.text.value).strip() or None
        await self.panel.save(interaction, message=value)


class _TimeModal(discord.ui.Modal, title="Announcement time"):
    offset = discord.ui.TextInput(label="Your time zone as a UTC offset (-12 to +14)", max_length=6, placeholder="e.g. 0 for Ghana, +1, -5")
    hour = discord.ui.TextInput(label="Hour to announce (0-23)", max_length=2, placeholder="9")

    def __init__(self, panel: "SetupView"):
        super().__init__()
        self.panel = panel
        self.offset.default = str(int(panel.cfg.get("utc_offset_hours") or 0))
        self.hour.default = str(int(panel.cfg.get("announce_hour", 9)))

    async def on_submit(self, interaction: discord.Interaction):
        off, hr = bd.validate_offset(self.offset.value), bd.validate_hour(self.hour.value)
        if off is None or hr is None:
            return await interaction.response.send_message(
                "That didn't look right. Use a whole-number UTC offset from -12 to +14 and an hour from 0 to 23.",
                ephemeral=True)
        await self.panel.save(interaction, utc_offset_hours=off, announce_hour=hr)


class SetupView(discord.ui.View):
    def __init__(self, cog: "BirthdayCog", guild: discord.Guild, cfg: dict, owner_id: int):
        super().__init__(timeout=600)
        self.cog, self.guild, self.cfg, self.owner_id = cog, guild, cfg, owner_id
        self._sync_toggle()

    def _sync_toggle(self):
        self.toggle.label = "Turn off" if self.cfg.get("enabled") else "Turn on"
        self.toggle.style = discord.ButtonStyle.danger if self.cfg.get("enabled") else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else. Run `/birthday` yourself.", ephemeral=True)
            return False
        return True

    async def save(self, interaction: discord.Interaction, **fields):
        self.cfg = await store.set_config(self.guild.id, self.cog.clone_id, **fields)
        self._sync_toggle()
        await interaction.response.edit_message(embed=_config_embed(self.guild, self.cfg), view=self)

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
                       placeholder="Channel for birthday announcements", row=0)
    async def pick_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = self.guild.get_channel(select.values[0].id)
        if not _can_post(channel, self.guild):
            return await interaction.response.send_message(
                f"I can't post (Send Messages + Embed Links) in {channel.mention if channel else 'that channel'}. "
                "Fix its permissions or pick another one.", ephemeral=True)
        await self.save(interaction, channel_id=channel.id)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Optional: role to ping (pick none to skip)",
                       min_values=0, max_values=1, row=1)
    async def pick_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0] if select.values else None
        if role is not None and (role.is_default() or role.managed):
            return await interaction.response.send_message("Pick a normal role (not @everyone or a bot role).", ephemeral=True)
        await self.save(interaction, ping_role_id=role.id if role else None)

    @discord.ui.button(label="Turn on", style=discord.ButtonStyle.success, row=2)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cfg.get("enabled") and not self.cfg.get("channel_id"):
            return await interaction.response.send_message("Pick a channel first.", ephemeral=True)
        await self.save(interaction, enabled=not self.cfg.get("enabled"))

    @discord.ui.button(label="Edit message", style=discord.ButtonStyle.secondary, emoji="✏️", row=2)
    async def edit_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_MessageModal(self))

    @discord.ui.button(label="Time zone & hour", style=discord.ButtonStyle.secondary, emoji="🕘", row=2)
    async def edit_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_TimeModal(self))

    @discord.ui.button(label="Send test", style=discord.ButtonStyle.secondary, emoji="🧪", row=2)
    async def send_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = self.guild.get_channel(self.cfg["channel_id"]) if self.cfg.get("channel_id") else None
        if not _can_post(ch, self.guild):
            return await interaction.response.send_message("Pick a channel I can post in first.", ephemeral=True)
        text = bd.format_message(self.cfg.get("message"), interaction.user.mention, interaction.user.display_name, self.guild.name)
        await ch.send(f"🧪 **Test message**\n{text}", allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message(f"Sent a test to {ch.mention} (nobody was pinged).", ephemeral=True)


# ---------------------------------------------------------------------------- member wizard

class _DayModal(discord.ui.Modal, title="Your birthday: day"):
    day = discord.ui.TextInput(label="Day of the month (1-31)", max_length=2, placeholder="e.g. 25")

    def __init__(self, month: int):
        super().__init__()
        self.month = month
        self.day.label = f"Day in {bd.MONTH_NAMES[month - 1]} (1-31)"

    async def on_submit(self, interaction: discord.Interaction):
        valid = bd.validate_birthday(self.month, self.day.value)
        if valid is None:
            return await interaction.response.send_message(
                f"{bd.MONTH_NAMES[self.month - 1]} doesn't have a day “{self.day.value}”. Run `/birthday` and try again.",
                ephemeral=True)
        await store.set_birthday(interaction.user.id, *valid)
        await interaction.response.send_message(
            f"🎂 Saved: **{bd.pretty_date(*valid)}**. Servers that turned birthdays on will announce it that day. "
            "You can change or remove it any time from `/birthday`.", ephemeral=True)


class _MonthView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.select(placeholder="Pick your birth month",
                       options=[discord.SelectOption(label=n, value=str(i + 1)) for i, n in enumerate(bd.MONTH_NAMES)])
    async def pick_month(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(_DayModal(int(select.values[0])))


class MainView(discord.ui.View):
    """The /birthday wizard. Everything a member or admin does starts here."""

    def __init__(self, cog: "BirthdayCog", guild: discord.Guild, user: discord.Member):
        super().__init__(timeout=600)
        self.cog, self.guild, self.user = cog, guild, user
        if not user.guild_permissions.manage_guild:
            self.remove_item(self.server_setup)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This panel belongs to someone else. Run `/birthday` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Set my birthday", emoji="🎂", style=discord.ButtonStyle.primary, row=0)
    async def set_mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Pick your birth month (no year is saved):",
                                                view=_MonthView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="My birthday", emoji="👀", style=discord.ButtonStyle.secondary, row=0)
    async def view_mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await store.get_birthday(interaction.user.id)
        if not row:
            return await interaction.response.send_message("You haven't set one yet. Tap **Set my birthday**.", ephemeral=True)
        n = bd.days_until(row["month"], row["day"], discord.utils.utcnow().date())
        await interaction.response.send_message(
            f"Your birthday: **{bd.pretty_date(row['month'], row['day'])}** ({bd.days_until_text(n)}).", ephemeral=True)

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.secondary, row=0)
    async def remove_mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        gone = await store.remove_birthday(interaction.user.id)
        await interaction.response.send_message(
            "Removed. I no longer have your birthday." if gone else "You hadn't set one.", ephemeral=True)

    @discord.ui.button(label="Upcoming", emoji="📅", style=discord.ButtonStyle.secondary, row=1)
    async def upcoming(self, interaction: discord.Interaction, button: discord.ui.Button):
        now = time.monotonic()
        if now < self.cog._upcoming_cooldown.get(interaction.user.id, 0):
            return await interaction.response.send_message("Slow down, try again in a few seconds.", ephemeral=True)
        self.cog._upcoming_cooldown[interaction.user.id] = now + 10
        await interaction.response.defer(ephemeral=True)
        today = discord.utils.utcnow().date()
        rows = sorted(
            ((bd.days_until(r["month"], r["day"], today), r)
             for r in await store.all_birthdays() if self.guild.get_member(r["user_id"])),
            key=lambda x: x[0])[:10]
        if not rows:
            return await interaction.followup.send("Nobody here has set a birthday yet.", ephemeral=True)
        lines = [f"<@{r['user_id']}>: {bd.pretty_date(r['month'], r['day'])} ({bd.days_until_text(n)})" for n, r in rows]
        await interaction.followup.send(
            embed=discord.Embed(title="🎂 Upcoming birthdays", description="\n".join(lines), color=discord.Color.pink()),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Server setup", emoji="⚙️", style=discord.ButtonStyle.success, row=1)
    async def server_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Re-check on click: permissions can change while the panel is open.
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("You need the **Manage Server** permission for this.", ephemeral=True)
        cfg = await store.get_config(self.guild.id, self.cog.clone_id)
        await interaction.response.send_message(
            embed=_config_embed(self.guild, cfg),
            view=SetupView(self.cog, self.guild, cfg, interaction.user.id), ephemeral=True)


def _main_embed(guild: discord.Guild) -> discord.Embed:
    return discord.Embed(
        title="🎂 Birthdays",
        description="Add your birthday and I'll announce it in this server on the day.\n"
                    "Only the month and day are saved, never the year.",
        color=discord.Color.pink(),
    )


# ---------------------------------------------------------------------------- the cog

class BirthdayCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.clone_id: Optional[int] = getattr(bot, "clone_id", None)
        self._upcoming_cooldown: dict = {}

    async def cog_load(self):
        try:
            await store.ensure_tables()
        except Exception:
            logger.exception("[birthdays] couldn't create tables")
        self.announce_loop.start()

    async def cog_unload(self):
        self.announce_loop.cancel()

    @app_commands.command(name="birthday", description="Set your birthday, see who's next, or set up announcements")
    @app_commands.guild_only()
    async def birthday(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_main_embed(interaction.guild),
            view=MainView(self, interaction.guild, interaction.user), ephemeral=True)

    # ---- daily announcer ----

    @tasks.loop(minutes=10)
    async def announce_loop(self):
        try:
            configs = await store.enabled_configs(self.clone_id)
        except Exception:
            logger.exception("[birthdays] couldn't read configs")
            return
        for cfg in configs:
            guild = self.bot.get_guild(cfg["guild_id"])
            if guild is None:
                continue          # this server isn't on this bot process (e.g. another clone)
            try:
                local = bd.local_now(cfg.get("utc_offset_hours") or 0)
                if not bd.due_for_announcement(local, int(cfg.get("announce_hour", 9)), cfg.get("last_announced")):
                    continue
                if not await store.claim_day(guild.id, self.clone_id, local.date()):
                    continue      # someone else already posted today
                await self._announce(guild, cfg, local.date())
            except Exception:
                logger.exception("[birthdays] announcing failed in guild %s", cfg.get("guild_id"))

    @announce_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _announce(self, guild: discord.Guild, cfg: dict, local_date) -> None:
        channel = guild.get_channel(cfg["channel_id"])
        if not _can_post(channel, guild):
            logger.info("[birthdays] can't post in guild %s", guild.id)
            return
        members: List[discord.Member] = []
        for uid in await store.birthdays_on(local_date):
            m = guild.get_member(uid)
            if m and not m.bot:
                members.append(m)
        if not members:
            return
        shown = members[:bd.MAX_MENTIONS]
        extra = len(members) - len(shown)
        mentions = ", ".join(m.mention for m in shown) + (f" and {extra} more" if extra > 0 else "")
        names = ", ".join(m.display_name for m in shown)
        text = bd.format_message(cfg.get("message"), mentions, names, guild.name)
        role = guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") else None
        if role:
            text = f"{role.mention} {text}"
        await channel.send(
            text,
            allowed_mentions=discord.AllowedMentions(users=shown, roles=[role] if role else False, everyone=False),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
