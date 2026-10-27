# path: discord_bot/cogs/_views_verify_setup.py

"""Verification setup, opened from the join DM's "More features" screen (and usable from anywhere with a guild id).

Components V2, and it survives restarts: every control is a DynamicItem whose custom_id carries the guild and clone id,
and every callback reads the saved settings from the database and redraws the whole message from them. Nothing is
kept in memory between taps, so a setup message from before a restart keeps working after one. Each change is saved
the moment it is made; "Lock down & enable" is the only step that touches channels.

Free for every server:
  * how members verify (one tap, or a quick captcha)
  * verify by DM: the new member gets a DM with the verify button (the verify channel stays as the fallback for closed DMs)
  * verify channel, Unverified role, Verified role (pick one or let me create it)
  * lock down & enable / turn off
Premium extras (same rule as everywhere: saved now, used only while Premium is active; modules/verification_pro.py):
  * custom panel / DM look, a log channel, auto-kick, minimum account age, a welcome DM after verifying
"""

import logging
import re

import discord

from database import db
from modules import verification_pro
from discord_bot.cogs._views_bot_profile import _check_access

logger = logging.getLogger(__name__)

ACCENT = discord.Color.blurple()


def _cid(kind: str, guild_id: int, clone_id) -> str:
    return f"vfysetup_{kind}:{guild_id}:{'-' if clone_id is None else clone_id}"


def _pat(kind: str) -> str:
    return rf"^vfysetup_{kind}:(\d+):(-|\d+)$"


def _ids(match: "re.Match"):
    part = match.group(2)
    return int(match.group(1)), (None if part == "-" else int(part))


def _chan(cid) -> str:
    return f"<#{cid}>" if cid else "*not set*"


def _role(rid, empty="*not set*") -> str:
    return f"<@&{rid}>" if rid else empty


# ── the view ──────────────────────────────────────────────────────────────

def _section(container, text: str, button) -> None:
    section = discord.ui.Section(accessory=button)
    section.add_item(text)
    container.add_item(section)


def missing_for_enable(cfg: dict) -> list:
    return [name for name, value in (("a verify channel", cfg.get("channel_id")),
                                      ("an Unverified role", cfg.get("unverified_role_id"))) if not value]


async def build_view(client, guild_id: int, clone_id, page: str = "setup", note: str | None = None) -> discord.ui.LayoutView:
    cfg = await db.get_verification_config(guild_id, clone_id=clone_id)
    premium = await verification_pro.is_premium(guild_id, clone_id) if page == "pro" else False
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=ACCENT)
    if page == "pro":
        _build_pro(c, guild_id, clone_id, cfg, premium)
    else:
        _build_setup(c, guild_id, clone_id, cfg)
    if note:
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(note[:1500]))
    view.add_item(c)
    return view


def _build_setup(c, guild_id: int, clone_id, cfg: dict) -> None:
    live = bool(cfg.get("enabled"))
    miss = missing_for_enable(cfg)
    if live:
        status = "🟢 **Live.** New members get the Unverified role until they verify."
    elif miss:
        status = f"⚪ Not enabled yet. Still needed: {' and '.join(miss)}."
    else:
        status = "⚪ Not enabled yet. Everything is set: press **Lock down & enable**."
    c.add_item(discord.ui.TextDisplay(
        "## 🛡️ Verification\n"
        "Keep bots and raiders out. New members get the **Unverified** role and can only see the verify channel "
        "until they pass the check. Free.\n" + status))
    c.add_item(discord.ui.Separator())

    c.add_item(discord.ui.TextDisplay("### 1 · How members verify"))
    c.add_item(discord.ui.ActionRow(ModeSelect(guild_id, clone_id, cfg.get("mode") or "captcha")))

    dm_on = bool(cfg.get("dm_verify"))
    _section(c, "### 2 · Verify by DM (free)\n"
                + ("💬 **On.** New members get a DM with the verify button. If their DMs are closed they use the verify channel."
                   if dm_on else
                   "Off. Turn it on and I'll DM every new member the verify button, so they don't have to find the channel."),
             DmToggleButton(guild_id, clone_id, dm_on))
    c.add_item(discord.ui.Separator())

    _section(c, f"### 3 · Verify channel\n{_chan(cfg.get('channel_id'))}", MakeChannelButton(guild_id, clone_id))
    c.add_item(discord.ui.ActionRow(ChannelPick(guild_id, clone_id, cfg.get("channel_id"))))
    _section(c, f"### 4 · Unverified role\n{_role(cfg.get('unverified_role_id'))}", MakeUnverifiedButton(guild_id, clone_id))
    c.add_item(discord.ui.ActionRow(UnverifiedPick(guild_id, clone_id, cfg.get("unverified_role_id"))))
    _section(c, f"### 5 · Verified role (optional)\n{_role(cfg.get('verified_role_id'), '*none, I just remove Unverified*')}",
             MakeVerifiedButton(guild_id, clone_id))
    c.add_item(discord.ui.ActionRow(VerifiedPick(guild_id, clone_id, cfg.get("verified_role_id"))))
    c.add_item(discord.ui.Separator())

    paused = verification_pro.paused_extras(cfg)
    _section(c, "### 💎 Premium extras\nCustom panel and DM look, a log channel, auto-kick for members who never verify, "
                "a minimum account age and a welcome DM."
                + (" *Your saved extras are paused until Premium is active.*" if paused else ""),
             ProButton(guild_id, clone_id))
    row = [EnableButton(guild_id, clone_id, disabled=bool(miss))]
    if live:
        row.append(OffButton(guild_id, clone_id))
    c.add_item(discord.ui.ActionRow(*row))


def _build_pro(c, guild_id: int, clone_id, cfg: dict, premium: bool) -> None:
    status = ("✅ Premium is active on this server." if premium else
              "🔒 **Needs Premium.** You can set these up now; they're saved but stay off until Premium is active.")
    c.add_item(discord.ui.TextDisplay(f"## 💎 Premium extras\n{status}"))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(f"### Log channel\nWho verified or got kicked. Now: {_chan(cfg.get('log_channel_id'))}"))
    c.add_item(discord.ui.ActionRow(LogChannelPick(guild_id, clone_id, cfg.get("log_channel_id"))))
    kick = verification_pro.label_for(verification_pro.KICK_AFTER_CHOICES, cfg.get("kick_after_minutes"))
    c.add_item(discord.ui.TextDisplay(f"### Auto-kick unverified\nMembers who never verify are removed. Now: **{kick}**"))
    c.add_item(discord.ui.ActionRow(ChoicePick("kick", guild_id, clone_id, cfg.get("kick_after_minutes"))))
    age = verification_pro.label_for(verification_pro.MIN_AGE_CHOICES, cfg.get("min_account_age_days"))
    c.add_item(discord.ui.TextDisplay(f"### Minimum account age\nNewer accounts are kicked when they join. Now: **{age}**"))
    c.add_item(discord.ui.ActionRow(ChoicePick("age", guild_id, clone_id, cfg.get("min_account_age_days"))))
    bits = [n for n, k in (("title", "panel_title"), ("text", "panel_text"), ("colour", "panel_color"),
                           ("banner", "panel_image_url"), ("welcome DM", "welcome_text")) if cfg.get(k) not in (None, "")]
    _section(c, f"### Panel look & welcome DM\nCustom: {', '.join(bits) if bits else 'none, using the standard look'}. "
                "Used on the verify panel and on the verify DM.", LookButton(guild_id, clone_id))
    c.add_item(discord.ui.Separator())
    row = [BackButton(guild_id, clone_id)]
    if not premium:
        row.append(GetPremiumButton(guild_id, clone_id))
    c.add_item(discord.ui.ActionRow(*row))


async def redraw(interaction: discord.Interaction, guild_id: int, clone_id, page: str = "setup", note: str | None = None) -> None:
    """Edit the setup message in place from the saved settings (the interaction is already acknowledged)."""
    view = await build_view(interaction.client, guild_id, clone_id, page, note)
    try:
        await interaction.edit_original_response(view=view)
    except discord.HTTPException:
        logger.warning("verify setup: couldn't edit the message for guild %s", guild_id, exc_info=True)
        if note:
            await interaction.followup.send(note, ephemeral=True)


async def open_verify_setup(interaction: discord.Interaction, guild_id: int, clone_id) -> None:
    """Called by the More-features button (already deferred). Sends the setup as its own message."""
    view = await build_view(interaction.client, guild_id, clone_id)
    await interaction.followup.send(view=view, ephemeral=interaction.guild is not None)


# ── plumbing ──────────────────────────────────────────────────────────────

class _Mixin:
    """Shared from_custom_id + the Manage Server gate. Subclasses set KIND."""
    KIND = ""

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        guild_id, clone_id = _ids(match)
        return cls(guild_id, clone_id)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        return await _check_access(interaction, self.guild_id)

    def _guild(self, interaction: discord.Interaction):
        return interaction.client.get_guild(self.guild_id)


class _Button(_Mixin):
    LABEL, EMOJI, STYLE = "", None, discord.ButtonStyle.secondary

    def __init__(self, guild_id: int, clone_id, **kw):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(
            label=self.LABEL, emoji=self.EMOJI, style=self.STYLE, custom_id=_cid(self.KIND, guild_id, clone_id), **kw))


# ── selects ───────────────────────────────────────────────────────────────

class ModeSelect(_Mixin, discord.ui.DynamicItem[discord.ui.Select], template=_pat("mode")):
    KIND = "mode"

    def __init__(self, guild_id: int, clone_id, current: str = "captcha"):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Select(
            placeholder="Choose how members verify", custom_id=_cid("mode", guild_id, clone_id),
            options=[
                discord.SelectOption(label="Captcha (recommended)", value="captcha", emoji="🧩",
                                     description="A quick security check. Blocks scripted joins.", default=current == "captcha"),
                discord.SelectOption(label="Button click", value="button", emoji="✅",
                                     description="One tap. Low friction, but bots can click it too.", default=current == "button"),
            ]))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, mode=self.item.values[0])
        await redraw(interaction, self.guild_id, self.clone_id, note=await _live_note(self.guild_id, self.clone_id, "the new method"))


async def _live_note(guild_id: int, clone_id, what: str):
    """When verification is already live, saved changes reach the verify panel only after a re-enable."""
    cfg = await db.get_verification_config(guild_id, clone_id=clone_id)
    return (f"Saved. Press **Lock down & enable** again to update the panel with {what}." if cfg.get("enabled") else None)


class ChannelPick(_Mixin, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_pat("chan")):
    KIND = "chan"

    def __init__(self, guild_id: int, clone_id, current=None):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Choose the verify channel", channel_types=[discord.ChannelType.text],
            custom_id=_cid("chan", guild_id, clone_id),
            default_values=[discord.Object(id=current, type=discord.abc.GuildChannel)] if current else []))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, channel_id=self.item.values[0].id)
        await redraw(interaction, self.guild_id, self.clone_id, note=await _live_note(self.guild_id, self.clone_id, "the new channel"))


class _RolePick(_Mixin):
    FIELD = ""
    PLACEHOLDER = ""
    OPTIONAL = False

    def __init__(self, guild_id: int, clone_id, current=None):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.RoleSelect(
            placeholder=self.PLACEHOLDER, custom_id=_cid(self.KIND, guild_id, clone_id),
            min_values=0 if self.OPTIONAL else 1, max_values=1,
            default_values=[discord.Object(id=current, type=discord.Role)] if current else []))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        picked = self.item.values[0].id if self.item.values else None
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, **{self.FIELD: picked})
        await redraw(interaction, self.guild_id, self.clone_id, note=await _live_note(self.guild_id, self.clone_id, "the new role"))


class UnverifiedPick(_RolePick, discord.ui.DynamicItem[discord.ui.RoleSelect], template=_pat("unv")):
    KIND, FIELD, PLACEHOLDER = "unv", "unverified_role_id", "Choose the Unverified role"


class VerifiedPick(_RolePick, discord.ui.DynamicItem[discord.ui.RoleSelect], template=_pat("ver")):
    KIND, FIELD, PLACEHOLDER, OPTIONAL = "ver", "verified_role_id", "Optional: choose the Verified role", True


class LogChannelPick(_Mixin, discord.ui.DynamicItem[discord.ui.ChannelSelect], template=_pat("logch")):
    KIND = "logch"

    def __init__(self, guild_id: int, clone_id, current=None):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.ChannelSelect(
            placeholder="Optional: choose a log channel", channel_types=[discord.ChannelType.text],
            min_values=0, max_values=1, custom_id=_cid("logch", guild_id, clone_id),
            default_values=[discord.Object(id=current, type=discord.abc.GuildChannel)] if current else []))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        picked = self.item.values[0].id if self.item.values else None
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, log_channel_id=picked)
        await redraw(interaction, self.guild_id, self.clone_id, page="pro")


class ChoicePick(_Mixin, discord.ui.DynamicItem[discord.ui.Select], template=r"^vfysetup_(kick|age):(\d+):(-|\d+)$"):
    """Auto-kick delay / minimum account age. One class for both: the kind rides in the custom_id."""
    FIELDS = {"kick": ("kick_after_minutes", verification_pro.KICK_AFTER_CHOICES, "Kick members who haven't verified after…"),
              "age": ("min_account_age_days", verification_pro.MIN_AGE_CHOICES, "Minimum account age to join…")}

    def __init__(self, kind: str, guild_id: int, clone_id, current=None):
        self.kind, self.guild_id, self.clone_id = kind, guild_id, clone_id
        field, choices, placeholder = self.FIELDS[kind]
        super().__init__(discord.ui.Select(
            placeholder=placeholder, custom_id=_cid(kind, guild_id, clone_id),
            options=[discord.SelectOption(label=label, value="off" if value is None else str(value), default=value == current)
                     for value, label in choices]))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        part = match.group(3)
        return cls(match.group(1), int(match.group(2)), None if part == "-" else int(part))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        pick = self.item.values[0]
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id,
                                         **{self.FIELDS[self.kind][0]: None if pick == "off" else int(pick)})
        await redraw(interaction, self.guild_id, self.clone_id, page="pro")


# ── buttons ───────────────────────────────────────────────────────────────

class DmToggleButton(_Mixin, discord.ui.DynamicItem[discord.ui.Button], template=_pat("dm")):
    KIND = "dm"

    def __init__(self, guild_id: int, clone_id, on: bool = False):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(
            label="DM: On" if on else "DM: Off", emoji="💬",
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
            custom_id=_cid("dm", guild_id, clone_id)))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        cfg = await db.get_verification_config(self.guild_id, clone_id=self.clone_id)
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, dm_verify=not cfg.get("dm_verify"))
        await redraw(interaction, self.guild_id, self.clone_id)


async def _below_bot(guild: discord.Guild, role: discord.Role) -> bool:
    from discord_bot.cogs.verification import _ensure_below_bot
    return await _ensure_below_bot(guild, role)


class MakeChannelButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=_pat("mkchan")):
    KIND, LABEL, EMOJI, STYLE = "mkchan", "Create it", "✨", discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        guild = self._guild(interaction)
        if guild is None:
            return await redraw(interaction, self.guild_id, self.clone_id, note="I can't reach that server right now.")
        existing = discord.utils.find(lambda ch: ch.name.casefold() == "verify", guild.text_channels)
        note = None
        if existing is not None:
            channel = existing
            note = f"A channel called {existing.mention} already exists, so I'm using it."
        else:
            try:
                channel = await guild.create_text_channel("verify", reason=f"Verification setup by {interaction.user}")
            except discord.Forbidden:
                return await redraw(interaction, self.guild_id, self.clone_id,
                                    note="I need **Manage Channels** to create it. Grant it, or pick a channel from the list.")
            except discord.HTTPException as e:
                return await redraw(interaction, self.guild_id, self.clone_id, note=f"Couldn't create the channel: {e}")
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, channel_id=channel.id)
        await redraw(interaction, self.guild_id, self.clone_id, note=note or f"✅ Created {channel.mention} and picked it.")


class _MakeRole(_Button):
    ROLE_NAME = ""
    FIELD = ""
    LABEL, EMOJI, STYLE = "Create it", "✨", discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        guild = self._guild(interaction)
        if guild is None:
            return await redraw(interaction, self.guild_id, self.clone_id, note="I can't reach that server right now.")
        existing = discord.utils.find(lambda r: r.name.casefold() == self.ROLE_NAME.casefold(), guild.roles)
        if existing is not None:
            await db.set_verification_config(self.guild_id, clone_id=self.clone_id, **{self.FIELD: existing.id})
            return await redraw(interaction, self.guild_id, self.clone_id,
                                note=f"A role called {existing.mention} already exists, so I'm using it.")
        try:
            role = await guild.create_role(name=self.ROLE_NAME, permissions=discord.Permissions.none(),
                                           reason=f"Verification setup by {interaction.user}")
        except discord.Forbidden:
            return await redraw(interaction, self.guild_id, self.clone_id,
                                note="I need **Manage Roles** to create it. Grant it, or pick a role from the list.")
        except discord.HTTPException as e:
            return await redraw(interaction, self.guild_id, self.clone_id, note=f"Couldn't create the role: {e}")
        below = await _below_bot(guild, role)
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, **{self.FIELD: role.id})
        note = (f"✅ Created {role.mention} and picked it." if below else
                f"⚠️ Created {role.mention}, but it isn't below my own role, so I can't hand it out. In **Server Settings → "
                "Roles** drag my role above it.")
        await redraw(interaction, self.guild_id, self.clone_id, note=note)


class MakeUnverifiedButton(_MakeRole, discord.ui.DynamicItem[discord.ui.Button], template=_pat("mkunv")):
    KIND, ROLE_NAME, FIELD = "mkunv", "Unverified", "unverified_role_id"


class MakeVerifiedButton(_MakeRole, discord.ui.DynamicItem[discord.ui.Button], template=_pat("mkver")):
    KIND, ROLE_NAME, FIELD = "mkver", "Verified", "verified_role_id"


class ProButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=_pat("pro")):
    KIND, LABEL, EMOJI, STYLE = "pro", "Open", "💎", discord.ButtonStyle.primary

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await redraw(interaction, self.guild_id, self.clone_id, page="pro")


class BackButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=_pat("back")):
    KIND, LABEL, EMOJI = "back", "Back to setup", "⬅️"

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await redraw(interaction, self.guild_id, self.clone_id)


class GetPremiumButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=_pat("getprem")):
    KIND, LABEL, EMOJI, STYLE = "getprem", "Get Premium", "💎", discord.ButtonStyle.success

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=interaction.guild is not None)
        try:
            from discord_bot.cogs._views_premium import send_premium_pitch
            await send_premium_pitch(interaction, self.guild_id, self.clone_id)
        except Exception:
            logger.exception("verify setup: couldn't show the Premium pitch for guild %s", self.guild_id)
            await interaction.followup.send("💎 Run `/premium` to see the plans.", ephemeral=True)


class OffButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=_pat("off")):
    KIND, LABEL, EMOJI, STYLE = "off", "Turn off", "⏹️", discord.ButtonStyle.danger

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        await db.set_verification_config(self.guild_id, clone_id=self.clone_id, enabled=False)
        await redraw(interaction, self.guild_id, self.clone_id,
                     note="⏹️ Verification is off. New members get no Unverified role and no DM. Channel permissions are left "
                          "as they are, and anyone already verified keeps their access.")


class EnableButton(_Mixin, discord.ui.DynamicItem[discord.ui.Button], template=_pat("go")):
    KIND = "go"

    def __init__(self, guild_id: int, clone_id, disabled: bool = False):
        self.guild_id, self.clone_id = guild_id, clone_id
        super().__init__(discord.ui.Button(
            label="Lock down & enable", emoji="🔒", style=discord.ButtonStyle.success, disabled=disabled,
            custom_id=_cid("go", guild_id, clone_id)))

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        await interaction.response.defer()
        guild = self._guild(interaction)
        if guild is None:
            return await redraw(interaction, self.guild_id, self.clone_id, note="I can't reach that server right now.")
        note = await enable_verification(guild, self.clone_id)
        await redraw(interaction, self.guild_id, self.clone_id, note=note)


async def enable_verification(guild: discord.Guild, clone_id) -> str:
    """Check the saved settings, lock the channels down, post the panel, switch verification on. Returns a note for the
    setup message. Never raises."""
    from discord_bot.cogs._views_verification import (
        build_verify_panel_embed, build_verify_panel_view, lockdown_guild_channels)
    from discord_bot.cogs.verification import _fresh_role_positions
    try:
        cfg = await db.get_verification_config(guild.id, clone_id=clone_id)
        miss = missing_for_enable(cfg)
        if miss:
            return f"⚠️ Still needed: {' and '.join(miss)}."
        unverified = guild.get_role(cfg["unverified_role_id"])
        channel = guild.get_channel(cfg["channel_id"])
        if unverified is None or channel is None:
            return "⚠️ That role or channel no longer exists. Pick it again."
        pos, bot_pos = await _fresh_role_positions(guild, unverified)
        if pos is None or pos >= bot_pos:
            return (f"⚠️ {unverified.mention} is at or above my highest role, so I couldn't hand it out. Move it below my role "
                    "in **Server Settings → Roles**, then press the button again.")
        if cfg.get("verified_role_id"):
            verified = guild.get_role(cfg["verified_role_id"])
            if verified is None:
                return "⚠️ The Verified role you picked no longer exists. Pick it again (or clear it)."
            vpos, vbot = await _fresh_role_positions(guild, verified)
            if vpos is None or vpos >= vbot or verified.managed:
                return (f"⚠️ {verified.mention} is at or above my highest role (or is managed by an integration), so I couldn't "
                        "give it to members. Drag my role above it in **Server Settings → Roles**, then press the button again.")
        touched = await lockdown_guild_channels(guild, unverified, cfg["channel_id"])
        premium = await verification_pro.is_premium(guild.id, clone_id)
        look = verification_pro.panel_look(verification_pro.effective_cfg(cfg, premium))
        try:
            panel = await channel.send(embed=build_verify_panel_embed(guild.name, cfg.get("mode") or "captcha", look),
                                       view=build_verify_panel_view(guild.id))
        except discord.Forbidden:
            return (f"⚠️ Channels are locked down, but I can't post in {channel.mention}. Give me **View Channel**, "
                    "**Send Messages** and **Embed Links** there, then press the button again.")
        await db.set_verification_config(guild.id, clone_id=clone_id, message_id=panel.id, enabled=True)
        extra = ("\n💬 New members will also get a DM with the verify button." if cfg.get("dm_verify") else "")
        if not premium and verification_pro.paused_extras(cfg):
            extra += "\n💎 Your Premium extras are saved but stay off until Premium is active."
        return f"✅ **Live.** Locked down {touched} channel(s) and posted the panel in {channel.mention}.{extra}"
    except Exception:
        logger.exception("verify setup: enabling failed for guild %s", guild.id)
        return "⚠️ Something went wrong while enabling. Try again in a moment."


class LookModal(discord.ui.Modal, title="Panel look & welcome message"):
    def __init__(self, guild_id: int, clone_id, pro: dict):
        super().__init__()
        self.guild_id, self.clone_id = guild_id, clone_id
        color = pro.get("panel_color")
        self.title_in = discord.ui.TextInput(
            label="Panel title", required=False, max_length=verification_pro.TITLE_MAX,
            default=pro.get("panel_title") or "", placeholder="blank = Welcome to <server>")
        self.text_in = discord.ui.TextInput(
            label="Panel text", required=False, style=discord.TextStyle.paragraph, max_length=verification_pro.TEXT_MAX,
            default=pro.get("panel_text") or "", placeholder="blank = the standard text")
        self.color_in = discord.ui.TextInput(
            label="Panel colour (hex)", required=False, max_length=7,
            default=f"#{color:06X}" if color is not None else "", placeholder="#5865F2")
        self.image_in = discord.ui.TextInput(
            label="Banner image link (https)", required=False, max_length=verification_pro.IMAGE_URL_MAX,
            default=pro.get("panel_image_url") or "")
        self.welcome_in = discord.ui.TextInput(
            label="Welcome DM after verifying", required=False, style=discord.TextStyle.paragraph,
            max_length=verification_pro.WELCOME_MAX, default=pro.get("welcome_text") or "",
            placeholder="Use {user} and {server}. Blank = the standard message.")
        for item in (self.title_in, self.text_in, self.color_in, self.image_in, self.welcome_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = verification_pro.parse_color(self.color_in.value)
            image = verification_pro.clean_image_url(self.image_in.value)
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ Nothing saved: {e}.", ephemeral=True)
            return
        await interaction.response.defer()
        await db.set_verification_config(
            self.guild_id, clone_id=self.clone_id,
            panel_title=verification_pro.clean_text(self.title_in.value, verification_pro.TITLE_MAX),
            panel_text=verification_pro.clean_text(self.text_in.value, verification_pro.TEXT_MAX),
            panel_color=color, panel_image_url=image,
            welcome_text=verification_pro.clean_text(self.welcome_in.value, verification_pro.WELCOME_MAX))
        await redraw(interaction, self.guild_id, self.clone_id, page="pro")


class LookButton(_Button, discord.ui.DynamicItem[discord.ui.Button], template=_pat("look")):
    KIND, LABEL, EMOJI = "look", "Edit", "🎨"

    async def callback(self, interaction: discord.Interaction):
        if not await self._allowed(interaction):
            return
        cfg = await db.get_verification_config(self.guild_id, clone_id=self.clone_id)
        await interaction.response.send_modal(LookModal(self.guild_id, self.clone_id, cfg))


# Registered with the join DM's items (see _views_join_dm.py) and so in bot.py's setup_hook, like every other wizard.
DYNAMIC_ITEMS = (
    ModeSelect, ChannelPick, UnverifiedPick, VerifiedPick, LogChannelPick, ChoicePick,
    DmToggleButton, MakeChannelButton, MakeUnverifiedButton, MakeVerifiedButton,
    ProButton, BackButton, GetPremiumButton, OffButton, EnableButton, LookButton,
)
