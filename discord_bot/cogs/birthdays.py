# path: discord_bot/cogs/birthdays.py

"""
Birthdays.

ONE slash command: /birthday. It opens a private wizard with buttons:
  Set my birthday | My birthday | Remove | Upcoming | Server setup (Manage Server only)
Server setup lets admins pick the channel, optional ping role, message, time zone / hour,
turn it on or off and send a test.

v2 adds 💎 Premium perks, gated on the server's Premium (db.is_guild_premium_active, same check as every other
Premium feature): styled embed, a birthday role for the day, and extra random messages. Settings are saved for
everyone but only applied while Premium is active, so a lapse quietly returns the server to the plain look.

An on-demand worker checks every 10 minutes with ONE query (enabled servers only) and posts
at most once per server per local day. It only runs while at least one server has birthdays on (or a birthday
role is waiting to be removed) and stops itself otherwise, so an unused feature costs nothing. A birthday is stored as month + day only (no year).
Birthdays are global per user, so a member sets theirs once and it works in every server
that has the feature on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from modules import birthday_store as store
from modules import birthdays as bd
from modules import goodbye_style as gs

logger = logging.getLogger(__name__)

async def _is_premium(guild_id: int, clone_id) -> bool:
    """Whether this server has Premium right now. Never raises (a failed lookup = free look)."""
    try:
        from database import db
        return bool(await db.is_guild_premium_active(guild_id, clone_id))
    except Exception:
        logger.exception("[birthdays] premium lookup failed for guild %s", guild_id)
        return False


async def _premium_gate(interaction: discord.Interaction, guild: discord.Guild, clone_id) -> bool:
    """True if Premium. If not, shows the normal Go Premium checkout (same one every other feature uses)."""
    if await _is_premium(guild.id, clone_id):
        return True
    from discord_bot.cogs._views_premium import send_premium_pitch
    await interaction.response.defer(ephemeral=True)
    await send_premium_pitch(interaction, guild.id, clone_id)
    return False


def _can_post(channel, guild: discord.Guild) -> bool:
    me = guild.me
    return bool(channel and me and channel.permissions_for(me).send_messages
                and channel.permissions_for(me).embed_links)


def _config_embed(guild: discord.Guild, cfg: dict, premium: bool = False) -> discord.Embed:
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
    if premium:
        brole = guild.get_role(cfg["birthday_role_id"]) if cfg.get("birthday_role_id") else None
        extra = len([m for m in (cfg.get("extra_messages") or []) if m])
        embed.add_field(
            name="💎 Premium perks (active)",
            value=f"🎨 Style: {bd.style_summary(cfg)}\n🎭 Birthday role: {brole.mention if brole else 'none'}\n"
                  f"🔁 Extra messages: {extra}", inline=False)
    else:
        embed.add_field(name="💎 Premium perks", value=bd.PREMIUM_PERKS + "\nTap a 💎 button to unlock them.", inline=False)
    embed.set_footer(text="Placeholders: {user} {name} {server}")
    return embed


def _build_post(guild: discord.Guild, cfg: dict, premium: bool, members: List[discord.Member], test: bool = False):
    """(content, embed_or_None, allowed_mentions) for one announcement."""
    shown = members[:bd.MAX_MENTIONS]
    extra = len(members) - len(shown)
    mentions = ", ".join(m.mention for m in shown) + (f" and {extra} more" if extra > 0 else "")
    names = ", ".join(m.display_name for m in shown)
    text = bd.format_message(bd.pick_message(cfg, premium), mentions, names, guild.name)
    ping_role = guild.get_role(cfg["ping_role_id"]) if cfg.get("ping_role_id") and not test else None
    allowed = discord.AllowedMentions(users=[] if test else shown, roles=[ping_role] if ping_role else False, everyone=False)
    style = bd.effective_style(cfg, premium)
    prefix = "🧪 **Test message**\n" if test else ""
    if not style["styled"]:
        return f"{prefix}{ping_role.mention + ' ' if ping_role else ''}{text}", None, allowed
    embed = discord.Embed(title=style["title"], description=text, color=discord.Color(style["color"]))
    if style["show_avatar"] and len(shown) == 1:
        embed.set_thumbnail(url=shown[0].display_avatar.url)
    if style["image_url"]:
        embed.set_image(url=style["image_url"])
    if style["footer"]:
        embed.set_footer(text=style["footer"])
    # Mentions inside an embed never ping, so the pings go in the message text above it.
    pings = ((ping_role.mention + " ") if ping_role else "") + ("" if test else ", ".join(m.mention for m in shown))
    return (prefix + pings).strip() or None, embed, allowed


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


class _StyleModal(discord.ui.Modal, title="💎 Announcement style"):
    title_in = discord.ui.TextInput(label="Title", max_length=bd.TITLE_MAX, required=False, placeholder="🎂 Happy Birthday!")
    color_in = discord.ui.TextInput(label="Colour (hex)", max_length=7, required=False, placeholder="#EE79C2")
    image_in = discord.ui.TextInput(label="Banner image link (https, png/jpg/gif/webp)", max_length=500, required=False)
    footer_in = discord.ui.TextInput(label="Footer", max_length=bd.FOOTER_MAX, required=False)
    avatar_in = discord.ui.TextInput(label="Show their avatar? (yes / no)", max_length=3, required=False, placeholder="yes")

    def __init__(self, panel: "SetupView"):
        super().__init__()
        self.panel = panel
        c = panel.cfg
        self.title_in.default = c.get("embed_title") or ""
        self.color_in.default = gs.color_hex(c["embed_color"]) if c.get("embed_color") is not None else ""
        self.image_in.default = c.get("embed_image_url") or ""
        self.footer_in.default = c.get("embed_footer") or ""
        self.avatar_in.default = "no" if c.get("show_avatar") is False else "yes"

    async def on_submit(self, interaction: discord.Interaction):
        color = None
        if str(self.color_in.value).strip():
            color = gs.parse_color(self.color_in.value)
            if color is None:
                return await interaction.response.send_message("That colour isn't valid. Use a hex code like `#EE79C2`.", ephemeral=True)
        ok, image = gs.check_image_url(self.image_in.value)
        if not ok:
            return await interaction.response.send_message("The banner must be an **https** link ending in .png, .jpg, .gif or .webp.", ephemeral=True)
        await self.panel.save(
            interaction,
            embed_title=str(self.title_in.value).strip() or None, embed_color=color, embed_image_url=image,
            embed_footer=str(self.footer_in.value).strip() or None,
            show_avatar=bd.parse_yes_no(self.avatar_in.value, True))


class _ExtraMessagesModal(discord.ui.Modal, title="💎 More messages"):
    lines = discord.ui.TextInput(
        label=f"Up to {bd.MAX_EXTRA_MESSAGES}, one per line (random pick)", style=discord.TextStyle.paragraph,
        max_length=1600, required=False, placeholder="Happy birthday {user}! 🥳\nAnother year older, {name}! 🎈")

    def __init__(self, panel: "SetupView"):
        super().__init__()
        self.panel = panel
        self.lines.default = "\n".join(panel.cfg.get("extra_messages") or [])

    async def on_submit(self, interaction: discord.Interaction):
        msgs, err = bd.parse_extra_messages(self.lines.value)
        if err:
            return await interaction.response.send_message(err, ephemeral=True)
        await self.panel.save(interaction, extra_messages=msgs or None)


class SetupView(discord.ui.View):
    def __init__(self, cog: "BirthdayCog", guild: discord.Guild, cfg: dict, owner_id: int, premium: bool = False):
        super().__init__(timeout=600)
        self.cog, self.guild, self.cfg, self.owner_id, self.premium = cog, guild, cfg, owner_id, premium
        self._sync_toggle()

    def _sync_toggle(self):
        tag = "" if self.premium else " 💎"
        self.style_btn.label = f"Style{tag}"
        self.more_btn.label = f"More messages{tag}"
        self.pick_birthday_role.placeholder = ("Birthday role for the day (optional)" if self.premium
                                               else "💎 Birthday role for the day (Premium)")
        self.toggle.label = "Turn off" if self.cfg.get("enabled") else "Turn on"
        self.toggle.style = discord.ButtonStyle.danger if self.cfg.get("enabled") else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This panel belongs to someone else. Run `/birthday` yourself.", ephemeral=True)
            return False
        return True

    async def save(self, interaction: discord.Interaction, **fields):
        self.cfg = await store.set_config(self.guild.id, self.cog.clone_id, **fields)
        self.premium = await _is_premium(self.guild.id, self.cog.clone_id)
        if self.cfg.get("enabled"):
            self.cog.wake()          # make sure the announcer is running for this server
        self._sync_toggle()
        await interaction.response.edit_message(embed=_config_embed(self.guild, self.cfg, self.premium), view=self)

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
        premium = await _is_premium(self.guild.id, self.cog.clone_id)
        content, embed, allowed = _build_post(self.guild, self.cfg, premium, [interaction.user], test=True)
        await ch.send(content=content, embed=embed, allowed_mentions=allowed)
        await interaction.response.send_message(f"Sent a test to {ch.mention} (nobody was pinged).", ephemeral=True)

    # ---- 💎 Premium perks (each one checks Premium first and shows the checkout if the server doesn't have it) ----

    @discord.ui.button(label="Style 💎", style=discord.ButtonStyle.primary, emoji="🎨", row=3)
    async def style_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _premium_gate(interaction, self.guild, self.cog.clone_id):
            return
        await interaction.response.send_modal(_StyleModal(self))

    @discord.ui.button(label="More messages 💎", style=discord.ButtonStyle.primary, emoji="🔁", row=3)
    async def more_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _premium_gate(interaction, self.guild, self.cog.clone_id):
            return
        await interaction.response.send_modal(_ExtraMessagesModal(self))

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="💎 Birthday role for the day (Premium)",
                       min_values=0, max_values=1, row=4)
    async def pick_birthday_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not await _premium_gate(interaction, self.guild, self.cog.clone_id):
            return
        role = select.values[0] if select.values else None
        if role is not None:
            me = self.guild.me
            real = self.guild.get_role(role.id)
            if real is None or real.is_default() or real.managed:
                return await interaction.response.send_message("Pick a normal role (not @everyone or a bot role).", ephemeral=True)
            if not me.guild_permissions.manage_roles or real >= me.top_role:
                return await interaction.response.send_message(
                    f"I can't hand out {real.mention}. Give me **Manage Roles** and move my role above it in "
                    "Server Settings → Roles, then pick it again.", ephemeral=True)
        await self.save(interaction, birthday_role_id=role.id if role else None)


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
        cd = self.cog._upcoming_cooldown
        if len(cd) > 500:
            cd.clear()
        cd[interaction.user.id] = now + 10
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
        premium = await _is_premium(self.guild.id, self.cog.clone_id)
        await interaction.response.send_message(
            embed=_config_embed(self.guild, cfg, premium),
            view=SetupView(self.cog, self.guild, cfg, interaction.user.id, premium), ephemeral=True)


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

    # ---- on-demand worker -------------------------------------------------------------------
    #
    # Nothing runs for birthdays until a server sets them up. At startup ONE cheap query decides
    # whether to start the worker at all; /birthday, "Set my birthday", etc. never start it.
    # The worker wakes when a server turns birthdays on (wake()), checks every 10 minutes, and
    # shuts itself down again as soon as no server has birthdays on and no birthday role is pending.

    CHECK_SECONDS = 600

    async def cog_load(self):
        self._task: Optional[asyncio.Task] = None
        self._dirty = False
        asyncio.create_task(self._startup_check())

    async def cog_unload(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _startup_check(self):
        try:
            await self.bot.wait_until_ready()
            if await store.has_work(self.clone_id):
                self.wake()
            else:
                logger.info("[birthdays] no server uses birthdays yet; staying idle")
        except Exception:
            logger.exception("[birthdays] startup check failed; starting the worker to be safe")
            self.wake()          # it shuts itself down again if there turns out to be nothing to do

    def wake(self) -> None:
        """Start the worker if it isn't running. Cheap and safe to call any time."""
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        try:
            await self.bot.wait_until_ready()
            while True:
                self._dirty = False
                busy = await self._tick()
                if not busy and not self._dirty:
                    self._task = None       # no await between this check and returning: no missed wake-ups
                    logger.info("[birthdays] nothing set up; worker stopped")
                    return
                if not self._dirty:
                    await asyncio.sleep(self.CHECK_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[birthdays] worker crashed")
            self._task = None

    @app_commands.command(name="birthday", description="Set your birthday, see who's next, or set up announcements")
    @app_commands.guild_only()
    async def birthday(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_main_embed(interaction.guild),
            view=MainView(self, interaction.guild, interaction.user), ephemeral=True)

    # ---- daily announcer ----

    async def _tick(self) -> bool:
        """One pass. Returns True while there is still something to watch (so the worker keeps running)."""
        try:
            configs = await store.enabled_configs(self.clone_id)
        except Exception:
            logger.exception("[birthdays] couldn't read configs")
            return True              # transient problem: keep going and retry next time
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
        pending_roles = await self._expire_roles()
        return bool(configs) or pending_roles

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
        premium = await _is_premium(guild.id, self.clone_id)
        content, embed, allowed = _build_post(guild, cfg, premium, members)
        await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        if premium and cfg.get("birthday_role_id"):
            await self._give_roles(guild, cfg, members, local_date)

    async def _give_roles(self, guild: discord.Guild, cfg: dict, members: List[discord.Member], local_date) -> None:
        """💎 Birthday role: given on the day, taken back at the start of the next local day."""
        role = guild.get_role(cfg["birthday_role_id"])
        me = guild.me
        if role is None or role.managed or me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
            logger.info("[birthdays] can't hand out the birthday role in guild %s", guild.id)
            return
        remove_after = bd.role_remove_after(local_date)
        for m in members:
            try:
                await m.add_roles(role, reason="Birthday role")
                await store.add_role_grant(guild.id, self.clone_id, m.id, role.id, remove_after)
            except (discord.Forbidden, discord.HTTPException):
                logger.info("[birthdays] couldn't give the birthday role to %s in guild %s", m.id, guild.id)

    async def _expire_roles(self) -> bool:
        """Runs even when Premium has lapsed, so a birthday role is never left on someone."""
        try:
            grants = await store.role_grants(self.clone_id)
        except Exception:
            logger.exception("[birthdays] couldn't read role grants")
            return True
        remaining = False
        offsets: dict = {}
        for g in grants:
            guild = self.bot.get_guild(g["guild_id"])
            if guild is None:
                continue
            try:
                if guild.id not in offsets:
                    offsets[guild.id] = (await store.get_config(guild.id, self.clone_id)).get("utc_offset_hours") or 0
                if not bd.role_grant_expired(g["remove_after"], bd.local_now(offsets[guild.id]).date()):
                    remaining = True
                    continue
                member, role = guild.get_member(g["user_id"]), guild.get_role(g["role_id"])
                if member and role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Birthday is over")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.info("[birthdays] couldn't take back the birthday role in guild %s", guild.id)
                await store.delete_role_grant(guild.id, self.clone_id, g["user_id"], g["role_id"])
            except Exception:
                logger.exception("[birthdays] role expiry failed in guild %s", g.get("guild_id"))
                remaining = True
        return remaining


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
