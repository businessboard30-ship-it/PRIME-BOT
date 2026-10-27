# path: discord_bot/cogs/verification.py

"""
Join verification / anti-raid gate.

One command only — /setupverification — everything else is the wizard
flow below (a Components V2 layout with two pages: Setup, and Premium
extras) plus one shared on_member_join listener that applies whichever
mode the guild picked. Captcha is the default mode. This mirrors the
guild-scoped-config pattern already used by automod/ticket/welcome: the
listener and the persistent button (_views_verification.py) both read
discord_verification_config fresh each time, so behaviour is uniform
across the bot and every clone without any bot-scoped code branching.

Wizard state (mode / channel / role picks / premium extras) lives on the
WizardView instance itself while the admin is stepping through it — same as
other admin-only setup wizards in this codebase that don't need to survive a
restart mid-flow, unlike the persistent verify button which does. The
layout is rebuilt from that state on every refresh, so every picker always
shows what is currently chosen.
"""

import asyncio
import logging

import discord
from modules.text_styles import plain_name as _plain_name
from discord_bot import perm_check
from discord import app_commands
from discord.ext import commands, tasks

from discord_bot.cogs._adaptive_skip import AdaptiveSkip

from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_verification import (
    build_verify_panel_embed,
    build_verify_panel_view,
    lockdown_guild_channels,
    apply_verification_pass,
    send_verify_dm,
)
from database import db
from modules import verification_pro


async def _fresh_role_positions(guild, role):
    """(role_position, bot_top_position) read live from Discord, not the cache.

    Role positions in discord.py's cache go stale the moment a role is created or
    moved (Discord shifts every role above the insertion point but doesn't send an
    update for each one). Comparing cached numbers is what made the bot create a
    role and then claim it was "above" itself. role_position is None if the role
    is gone."""
    roles = await guild.fetch_roles()
    by_id = {r.id: r for r in roles}
    mine = max((by_id[r.id].position for r in guild.me.roles if r.id in by_id), default=0)
    target = by_id.get(role.id)
    return (target.position if target is not None else None), mine


async def _ensure_below_bot(guild, role) -> bool:
    """True if `role` sits below the bot's top role. Discord already creates new
    roles directly above @everyone (below the bot), so normally nothing is moved;
    only if the live check says it isn't below do we try one reposition."""
    pos, bot_pos = await _fresh_role_positions(guild, role)
    if pos is not None and pos >= bot_pos:
        try:
            await guild.edit_role_positions(positions={role: max(bot_pos - 1, 1)})
        except discord.HTTPException:
            logger.warning("verification: couldn't reposition role %s in guild %s", role.id, guild.id)
        pos, bot_pos = await _fresh_role_positions(guild, role)
    return pos is not None and pos < bot_pos

logger = logging.getLogger(__name__)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


class ModeSelect(discord.ui.Select):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="Choose how members verify",
            options=[
                discord.SelectOption(label="Captcha (recommended)", value="captcha", emoji="🧩",
                                     description="A quick security check. Blocks scripted joins.",
                                     default=wizard.mode == "captcha"),
                discord.SelectOption(label="Button click", value="button", emoji="✅",
                                     description="One tap. Low friction, but bots can click it too.",
                                     default=wizard.mode == "button"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.mode = self.values[0]
        await self.wizard.refresh(interaction)


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="Choose the verify channel",
            channel_types=[discord.ChannelType.text],
            # default_values pre-populates the picker with whatever's
            # already chosen — without this, Discord always renders a
            # select back at its bare placeholder after every
            # edit_message, even though the bot already has a value for
            # it internally. That mismatch is exactly what made this
            # look like "nothing was picked, pick it again" after an
            # auto-create or after a value carried over from a previous
            # /setupverification run.
            default_values=[discord.Object(id=wizard.channel_id, type=discord.abc.GuildChannel)] if wizard.channel_id else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.channel_id = self.values[0].id
        self.default_values = self.values
        await self.wizard.refresh(interaction)


class AutoCreateVerifyChannelButton(discord.ui.Button):
    """Mirrors AutoCreateUnverifiedButton below (same double-tap guard,
    same reuse-if-it-already-exists check, same "no room below me"
    guard) but for the #verify channel side of setup instead of the
    role side — the wizard previously only let you pick an EXISTING
    channel via ChannelSelect above, with no way to create one in one
    tap the way the Unverified role already could. Shares row 4 with
    AutoCreateUnverifiedButton/FinishButton/CancelButton — that row
    still has room for a 4th button (Discord allows up to 5 per row),
    and every select above already occupies its own full row (0-3), so
    this can't go anywhere else without exceeding Discord's 5-row cap
    per message."""

    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            label="✨ Auto-create",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard

        if wizard._creating_channel:
            await interaction.response.send_message(
                "Already creating the channel — one sec, don't tap again.", ephemeral=True
            )
            return

        # Reuse an earlier auto-create from this same session rather than
        # spawning a duplicate #verify channel on a double click. Still
        # refreshes the picker (not just an ephemeral note) so the
        # dropdown visibly shows the reused channel instead of looking
        # like nothing's selected.
        if wizard.auto_created_channel_id:
            existing = interaction.guild.get_channel(wizard.auto_created_channel_id)
            if existing is not None:
                wizard.channel_id = existing.id
                wizard.channel_select.default_values = [existing]
                await wizard.refresh(interaction)
                await interaction.followup.send(
                    f"Already created {existing.mention} earlier — reusing it instead of making a duplicate.",
                    ephemeral=True,
                )
                return
            # Deleted out-of-band since — fall through and create a fresh one.
            wizard.auto_created_channel_id = None

        # Cross-session guard, same reasoning as the role side: catches
        # re-running /setupverification (a brand new WizardView with no
        # memory of the earlier click) — source of truth is the guild
        # itself, not this view.
        existing_by_name = discord.utils.find(lambda c: _plain_name(c.name) == "verify", interaction.guild.text_channels)
        if existing_by_name is not None:
            wizard.channel_id = existing_by_name.id
            wizard.auto_created_channel_id = existing_by_name.id
            wizard.channel_select.default_values = [existing_by_name]
            await wizard.refresh(interaction)
            await interaction.followup.send(
                f"A channel called {existing_by_name.mention} already exists in this server — "
                "reusing it instead of creating a duplicate.",
                ephemeral=True,
            )
            return

        wizard._creating_channel = True
        guild = interaction.guild

        try:
            channel = await guild.create_text_channel(
                "verify", reason=f"Verification setup by {interaction.user} (auto-created)",
            )
        except discord.Forbidden:
            wizard._creating_channel = False
            await interaction.response.send_message(
                "I don't have permission to create channels here — grant me **Manage Channels**, "
                "or pick an existing channel from the dropdown instead.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            wizard._creating_channel = False
            await interaction.response.send_message(f"Couldn't create the channel: {e}", ephemeral=True)
            return

        wizard.channel_id = channel.id
        wizard.auto_created_channel_id = channel.id
        wizard.channel_select.default_values = [channel]
        wizard._creating_channel = False
        await wizard.refresh(interaction)
        await interaction.followup.send(
            f"✅ Created {channel.mention} and selected it as your verify channel.", ephemeral=True,
        )


class UnverifiedRoleSelect(discord.ui.RoleSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="Choose the Unverified role",
            # Same reasoning as ChannelSelect above — pre-populate so a
            # carried-over or auto-created role shows as actually
            # selected instead of resetting to the bare placeholder.
            default_values=[discord.Object(id=wizard.unverified_role_id, type=discord.Role)] if wizard.unverified_role_id else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.unverified_role_id = self.values[0].id
        self.default_values = self.values
        await self.wizard.refresh(interaction)


class AutoCreateUnverifiedButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            label="✨ Auto-create",
            style=discord.ButtonStyle.primary,
        )

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard

        # Guard against double-taps: a rapid second click can land before the
        # first one has finished creating the role and re-rendered the view.
        if wizard._creating_role:
            await interaction.response.send_message(
                "Already creating the role — one sec, don't tap again.", ephemeral=True
            )
            return

        # If we already auto-created a role earlier in this session, reuse it
        # instead of spawning a duplicate "Unverified" role. Refreshes the
        # picker (not just an ephemeral note) so it visibly shows the
        # reused role instead of looking unselected.
        if wizard.auto_created_role_id:
            existing = interaction.guild.get_role(wizard.auto_created_role_id)
            if existing is not None:
                wizard.unverified_role_id = existing.id
                wizard.unverified_role_select.default_values = [existing]
                await wizard.refresh(interaction)
                await interaction.followup.send(
                    f"Already created {existing.mention} earlier — reusing it instead of making a duplicate.",
                    ephemeral=True,
                )
                return
            # The previously created role was deleted out-of-band; fall
            # through and create a fresh one.
            wizard.auto_created_role_id = None

        # Cross-session guard: this also catches re-running /setupverification
        # (a brand new WizardView with no memory of the earlier click) and
        # protects against Discord occasionally re-delivering a component
        # interaction. Source of truth is the guild itself, not this view.
        existing_by_name = discord.utils.find(
            lambda r: r.name.casefold() == "unverified", interaction.guild.roles
        )
        if existing_by_name is not None:
            wizard.unverified_role_id = existing_by_name.id
            wizard.auto_created_role_id = existing_by_name.id
            wizard.unverified_role_select.default_values = [existing_by_name]
            await wizard.refresh(interaction)
            await interaction.followup.send(
                f"A role called {existing_by_name.mention} already exists in this server — "
                "reusing it instead of creating a duplicate.",
                ephemeral=True,
            )
            return

        wizard._creating_role = True
        guild = interaction.guild

        try:
            role = await guild.create_role(
                name="Unverified",
                permissions=discord.Permissions.none(),
                reason=f"Verification setup by {interaction.user} (auto-created)",
            )
        except discord.Forbidden:
            wizard._creating_role = False
            await interaction.response.send_message(
                "I don't have permission to create roles here — grant me **Manage Roles**, "
                "or pick an existing role from the dropdown instead.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            wizard._creating_role = False
            await interaction.response.send_message(f"Couldn't create the role: {e}", ephemeral=True)
            return

        # Slot it directly below the bot's top role so the bot can always
        # assign/remove it later, regardless of where else it ends up.
        #
        # A single role.edit(position=...) call used to be used here —
        # Discord's single-role position PATCH is well-known to be
        # unreliable/racy (it computes the new order against a snapshot
        # that can already be stale by the time it's applied, especially
        # right after create_role added a new row to that same list), so
        # this silently no-op'd or landed the role in the wrong slot more
        # often than it should have. That's exactly what produced the
        # confusing "I just created this role and now you're telling me
        # it's above me" report: the auto-create step created the role,
        # its own reposition attempt quietly failed or under/over-shot,
        # and the wizard's Finish-step check (further down, comparing
        # unverified_role.position to guild.me.top_role.position) is what
        # actually caught it — after the fact, with no obvious link back
        # to the button that just ran. guild.edit_role_positions() is
        # Discord's documented reliable path (bulk reorder, not a single
        # role's position field), so use that instead.
        #
        # Also guard the case no reposition can ever fix: if the bot's
        # OWN top role is already sitting at (or just above) @everyone,
        # there's no slot below it to put anything — no amount of
        # repositioning the new role changes that. Tell the owner to
        # move the BOT's role up instead of quietly creating a
        # role that's doomed to end up misplaced.
        below = await _ensure_below_bot(guild, role)

        wizard.unverified_role_id = role.id
        wizard.auto_created_role_id = role.id
        wizard.unverified_role_select.default_values = [role]
        wizard._creating_role = False
        await wizard.refresh(interaction)

        if not below:
            await interaction.followup.send(
                f"⚠️ Created {role.mention}, but it isn't below my own role, so I can't assign/remove it. "
                "In **Server Settings → Roles** drag my bot's role above it, then run /setupverification again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Created {role.mention} and selected it as your Unverified role.",
            ephemeral=True,
        )


class VerifiedRoleSelect(discord.ui.RoleSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="Optional: choose a Verified role", min_values=0, max_values=1,
            default_values=[discord.Object(id=wizard.verified_role_id, type=discord.Role)] if wizard.verified_role_id else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.verified_role_id = self.values[0].id if self.values else None
        self.default_values = self.values
        await self.wizard.refresh(interaction)


class AutoCreateVerifiedButton(discord.ui.Button):
    """One-tap Verified role (the optional role members get on passing
    verification). Same reuse/double-tap guards as AutoCreateUnverifiedButton."""

    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="✨ Auto-create", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard
        if wizard._creating_role:
            await interaction.response.send_message("Already creating a role — one sec, don't tap again.", ephemeral=True)
            return
        guild = interaction.guild
        existing = discord.utils.find(lambda r: r.name.casefold() == "verified", guild.roles)
        if existing is not None:
            wizard.verified_role_id = existing.id
            wizard.verified_role_select.default_values = [existing]
            await wizard.refresh(interaction)
            await interaction.followup.send(
                f"A role called {existing.mention} already exists — reusing it instead of creating a duplicate.",
                ephemeral=True,
            )
            return

        wizard._creating_role = True
        try:
            role = await guild.create_role(
                name="Verified", permissions=discord.Permissions.none(),
                reason=f"Verification setup by {interaction.user} (auto-created)",
            )
        except discord.Forbidden:
            wizard._creating_role = False
            await interaction.response.send_message(
                "I don't have permission to create roles here — grant me **Manage Roles**, "
                "or pick an existing role from the dropdown instead.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            wizard._creating_role = False
            await interaction.response.send_message(f"Couldn't create the role: {e}", ephemeral=True)
            return

        below = await _ensure_below_bot(guild, role)

        wizard.verified_role_id = role.id
        wizard.verified_role_select.default_values = [role]
        wizard._creating_role = False
        await wizard.refresh(interaction)

        if not below:
            await interaction.followup.send(
                f"⚠️ Created {role.mention}, but it isn't below my own role, so I can't hand it out. "
                "Drag my role above it in **Server Settings → Roles**.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(f"✅ Created {role.mention} and selected it as your Verified role.", ephemeral=True)


class FinishButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Lock down & enable", style=discord.ButtonStyle.success, emoji="🔒",
                         disabled=not (wizard.channel_id and wizard.unverified_role_id))

    async def callback(self, interaction: discord.Interaction):
        wizard = self.wizard
        if not wizard.channel_id or not wizard.unverified_role_id:
            await interaction.response.send_message(
                "Pick a verify channel and an Unverified role before finishing.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        unverified_role = guild.get_role(wizard.unverified_role_id)
        channel = guild.get_channel(wizard.channel_id)
        if unverified_role is None or channel is None:
            await interaction.followup.send("That role or channel no longer exists — please pick again.", ephemeral=True)
            return

        role_pos, bot_pos = await _fresh_role_positions(guild, unverified_role)
        if role_pos is None or role_pos >= bot_pos:
            await interaction.followup.send(
                f"⚠️ {unverified_role.mention} is positioned at or above my highest role, so I won't be able to "
                "assign/remove it on join/verify. Move it below my role in Server Settings → Roles, then run "
                "/setupverification again.",
                ephemeral=True,
            )
            return

        if wizard.verified_role_id:
            verified_role = guild.get_role(wizard.verified_role_id)
            if verified_role is None:
                await interaction.followup.send(
                    "The Verified role you picked no longer exists — pick it again (or clear it).", ephemeral=True)
                return
            v_pos, v_bot_pos = await _fresh_role_positions(guild, verified_role)
            if v_pos is None or v_pos >= v_bot_pos or verified_role.managed:
                await interaction.followup.send(
                    f"⚠️ {verified_role.mention} is at or above my highest role (or is managed by an integration), "
                    "so I won't be able to give it to members when they verify. Drag my bot's role above it in "
                    "Server Settings → Roles, then run /setupverification again.",
                    ephemeral=True,
                )
                return

        touched = await lockdown_guild_channels(guild, unverified_role, wizard.channel_id)

        clone_id = _clone_id_of(interaction)
        premium = await verification_pro.is_premium(guild.id, clone_id)
        look = verification_pro.panel_look(verification_pro.effective_cfg(wizard.pro, premium))
        panel_embed = build_verify_panel_embed(guild.name, wizard.mode, look)
        panel_view = build_verify_panel_view(guild.id)
        try:
            panel_message = await channel.send(embed=panel_embed, view=panel_view)
        except discord.Forbidden:
            # I'm missing Send Messages/Embed Links/View Channel in the
            # chosen verify channel — without this, the exception used to
            # propagate out of the callback and just get swallowed by
            # discord.py's view error handler, leaving the deferred
            # interaction hanging with no feedback at all.
            await interaction.followup.send(
                f"⚠️ Channels were locked down, but I don't have permission to post in "
                f"{channel.mention}. Give me **View Channel**, **Send Messages** and "
                f"**Embed Links** there, then run `/setupverification` again to post the panel.",
                ephemeral=True,
            )
            return

        await db.set_verification_config(
            guild.id,
            clone_id=clone_id,
            **wizard.pro,
            mode=wizard.mode,
            channel_id=wizard.channel_id,
            unverified_role_id=wizard.unverified_role_id,
            verified_role_id=wizard.verified_role_id,
            message_id=panel_message.id,
            enabled=True,
        )

        note = ""
        if not premium and verification_pro.paused_extras(wizard.pro):
            note = "\n💎 Your premium extras are saved, but stay off until Premium is active on this server."
        await interaction.followup.send(
            f"✅ Verification is live — locked down {touched} channel(s) and posted the panel in {channel.mention}.{note}",
            ephemeral=True,
        )
        wizard.stop()


class CancelButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.stop()
        done = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=discord.Color.light_grey())
        container.add_item(discord.ui.TextDisplay("Verification setup cancelled."))
        done.add_item(container)
        await interaction.response.edit_message(view=done)


# ── premium extras (page 2) ───────────────────────────────────────────────

class PremiumPageButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Premium extras", style=discord.ButtonStyle.primary, emoji="💎")

    async def callback(self, interaction: discord.Interaction):
        # Looked up when the page opens (not at construction) so building the wizard stays synchronous.
        self.wizard.premium = await verification_pro.is_premium(interaction.guild_id, _clone_id_of(interaction))
        self.wizard.page = "pro"
        await self.wizard.refresh(interaction)


class BackToSetupButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Back to setup", style=discord.ButtonStyle.secondary, emoji="⬅️")

    async def callback(self, interaction: discord.Interaction):
        self.wizard.page = "setup"
        await self.wizard.refresh(interaction)


class GetPremiumButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Get Premium", style=discord.ButtonStyle.success, emoji="💎")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        try:
            from discord_bot.cogs._views_premium import send_premium_pitch
            await send_premium_pitch(interaction, interaction.guild_id, clone_id)
        except Exception:
            logger.exception("verification: couldn't show the Premium pitch for guild %s", interaction.guild_id)
            await interaction.followup.send("💎 Run `/premium` to see the plans.", ephemeral=True)


class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        current = wizard.pro.get("log_channel_id")
        super().__init__(
            placeholder="Optional: choose a log channel", min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            default_values=[discord.Object(id=current, type=discord.abc.GuildChannel)] if current else [],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.pro["log_channel_id"] = self.values[0].id if self.values else None
        await self.wizard.refresh(interaction)


class _ChoiceSelect(discord.ui.Select):
    """Select over (stored value, label) choices where None = Off. Subclasses set `field`/`choices`/`placeholder_text`."""
    field = ""
    choices = ()
    placeholder_text = ""

    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        current = wizard.pro.get(self.field)
        super().__init__(
            placeholder=self.placeholder_text,
            options=[
                discord.SelectOption(label=label, value="off" if value is None else str(value), default=value == current)
                for value, label in self.choices
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        pick = self.values[0]
        self.wizard.pro[self.field] = None if pick == "off" else int(pick)
        await self.wizard.refresh(interaction)


class KickAfterSelect(_ChoiceSelect):
    field = "kick_after_minutes"
    choices = verification_pro.KICK_AFTER_CHOICES
    placeholder_text = "Kick members who haven't verified after…"


class MinAgeSelect(_ChoiceSelect):
    field = "min_account_age_days"
    choices = verification_pro.MIN_AGE_CHOICES
    placeholder_text = "Minimum account age to join…"


class LookModal(discord.ui.Modal, title="Panel look & welcome message"):
    def __init__(self, wizard: "WizardView"):
        super().__init__()
        self.wizard = wizard
        pro = wizard.pro
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
        self.wizard.pro.update(
            panel_title=verification_pro.clean_text(self.title_in.value, verification_pro.TITLE_MAX),
            panel_text=verification_pro.clean_text(self.text_in.value, verification_pro.TEXT_MAX),
            panel_color=color,
            panel_image_url=image,
            welcome_text=verification_pro.clean_text(self.welcome_in.value, verification_pro.WELCOME_MAX),
        )
        await self.wizard.refresh(interaction)


class LookButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Edit", style=discord.ButtonStyle.secondary, emoji="🎨")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LookModal(self.wizard))


# ── the wizard ────────────────────────────────────────────────────────────

class WizardView(discord.ui.LayoutView):
    """Two pages in one message: Setup (method, verify channel, roles, enable) and Premium extras.

    The layout is rebuilt from the state below on every refresh (never patched in place), so a picker always shows
    what is chosen and the Lock down button is only enabled once it can work."""

    def __init__(self, invoker_id: int, current: dict):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.mode = current.get("mode") or "captcha"
        self.channel_id = current.get("channel_id")
        self.unverified_role_id = current.get("unverified_role_id")
        self.verified_role_id = current.get("verified_role_id")
        self.pro = {k: current.get(k) for k in verification_pro.PRO_FIELDS}
        self.premium = False        # looked up when the Premium page opens
        self.page = "setup"
        self.auto_created_role_id = None
        self.auto_created_channel_id = None
        self._creating_role = False
        self._creating_channel = False
        # Kept as attributes (rebuilt every refresh) because the auto-create buttons update their .default_values.
        self.channel_select = self.unverified_role_select = self.verified_role_select = None
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran /setupverification can use this.", ephemeral=True)
            return False
        return True

    # -- layout --------------------------------------------------------

    @staticmethod
    def _chan(cid) -> str:
        return f"<#{cid}>" if cid else "*not set*"

    @staticmethod
    def _role(rid, empty="*not set*") -> str:
        return f"<@&{rid}>" if rid else empty

    @staticmethod
    def _row_with_button(container, text: str, button) -> None:
        section = discord.ui.Section(accessory=button)
        section.add_item(text)
        container.add_item(section)

    def _build(self) -> None:
        self.clear_items()
        container = discord.ui.Container(accent_colour=discord.Color.blurple())
        (self._build_pro if self.page == "pro" else self._build_setup)(container)
        self.add_item(container)

    def _build_setup(self, c) -> None:
        c.add_item(discord.ui.TextDisplay(
            "## 🔐 Join verification\n"
            "New members get the **Unverified** role and can only see the verify channel until they pass the check. "
            "Set it up below, then press **Lock down & enable**; I apply the channel permissions for you."
        ))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("### 1 · How members verify"))
        c.add_item(discord.ui.ActionRow(ModeSelect(self)))
        c.add_item(discord.ui.Separator())

        self._row_with_button(c, f"### 2 · Verify channel\n{self._chan(self.channel_id)}", AutoCreateVerifyChannelButton(self))
        self.channel_select = ChannelSelect(self)
        c.add_item(discord.ui.ActionRow(self.channel_select))

        self._row_with_button(c, f"### 3 · Unverified role\n{self._role(self.unverified_role_id)}", AutoCreateUnverifiedButton(self))
        self.unverified_role_select = UnverifiedRoleSelect(self)
        c.add_item(discord.ui.ActionRow(self.unverified_role_select))

        self._row_with_button(
            c, f"### 4 · Verified role (optional)\n{self._role(self.verified_role_id, '*none, I just remove Unverified*')}",
            AutoCreateVerifiedButton(self))
        self.verified_role_select = VerifiedRoleSelect(self)
        c.add_item(discord.ui.ActionRow(self.verified_role_select))

        c.add_item(discord.ui.Separator())
        missing = [n for n, v in (("a verify channel", self.channel_id), ("an Unverified role", self.unverified_role_id)) if not v]
        c.add_item(discord.ui.TextDisplay(
            "✅ Ready. Press **Lock down & enable**." if not missing else f"⚠️ Still needed: {' and '.join(missing)}."))
        c.add_item(discord.ui.ActionRow(FinishButton(self), PremiumPageButton(self), CancelButton(self)))

    def _build_pro(self, c) -> None:
        pro = self.pro
        status = ("✅ Premium is active on this server." if self.premium else
                  "🔒 **Needs Premium.** You can set these up now; they're saved but stay off until Premium is active.")
        c.add_item(discord.ui.TextDisplay(f"## 💎 Premium extras\n{status}"))
        c.add_item(discord.ui.Separator())

        c.add_item(discord.ui.TextDisplay(
            f"### Log channel\nWho verified or got kicked. Now: {self._chan(pro.get('log_channel_id'))}"))
        c.add_item(discord.ui.ActionRow(LogChannelSelect(self)))

        kick = verification_pro.label_for(verification_pro.KICK_AFTER_CHOICES, pro.get("kick_after_minutes"))
        c.add_item(discord.ui.TextDisplay(f"### Auto-kick unverified\nMembers who never verify are removed. Now: **{kick}**"))
        c.add_item(discord.ui.ActionRow(KickAfterSelect(self)))

        age = verification_pro.label_for(verification_pro.MIN_AGE_CHOICES, pro.get("min_account_age_days"))
        c.add_item(discord.ui.TextDisplay(f"### Minimum account age\nNewer accounts are kicked when they join. Now: **{age}**"))
        c.add_item(discord.ui.ActionRow(MinAgeSelect(self)))

        look_bits = [n for n, k in (("title", "panel_title"), ("text", "panel_text"), ("colour", "panel_color"),
                                     ("banner", "panel_image_url"), ("welcome DM", "welcome_text")) if pro.get(k) not in (None, "")]
        self._row_with_button(
            c, f"### Panel look & welcome DM\nCustom: {', '.join(look_bits) if look_bits else 'none, using the standard look'}",
            LookButton(self))

        c.add_item(discord.ui.Separator())
        row = [BackToSetupButton(self)]
        if not self.premium:
            row.append(GetPremiumButton(self))
        c.add_item(discord.ui.ActionRow(*row))

    async def refresh(self, interaction: discord.Interaction):
        self._build()
        await interaction.response.edit_message(view=self)


class VerificationCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Awake for 10 minutes (= how long a captcha link is valid) after startup and after every link handed out
        # (wake_poll, called by the captcha button); after that it checks every 10 s instead of every 5 s, never slower.
        self._gate = AdaptiveSkip(idle_threshold=120, idle_skip=1, name="verification_poll")

    def wake_poll(self) -> None:
        self._gate.wake()

    async def cog_load(self):
        import config
        if getattr(config, "TURNSTILE_ENABLED", False):
            self.poll_turnstile_passes.start()
        self.kick_unverified.start()

    async def cog_unload(self):
        self.poll_turnstile_passes.cancel()
        self.kick_unverified.cancel()

    # Premium: remove members who never verified. Max kicks per server per pass keeps a big backlog from hammering Discord.
    MAX_KICKS_PER_PASS = 25

    @tasks.loop(minutes=2)
    async def kick_unverified(self):
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            rows = await db.get_verification_kick_configs(clone_id)
        except Exception:
            logger.exception("verification: couldn't load auto-kick configs")
            return
        for row in rows:
            try:
                await self._kick_stale_in(row, clone_id)
            except Exception:
                logger.exception("verification: auto-kick failed for guild %s", row.get("guild_id"))

    @kick_unverified.before_loop
    async def _before_kick(self):
        await self.bot.wait_until_ready()

    async def _kick_stale_in(self, row: dict, clone_id) -> int:
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return 0
        cfg = await verification_pro.effective_for(guild.id, clone_id, row)
        minutes = cfg.get("kick_after_minutes")
        role = guild.get_role(cfg["unverified_role_id"]) if cfg.get("unverified_role_id") else None
        if not minutes or role is None:           # not premium (paused), or the role is gone
            return 0
        if not guild.me.guild_permissions.kick_members:
            perm_check.flag(guild.id, clone_id, "verification_kick",
                            "Auto-kick for unverified members is on, but I don't have the Kick Members permission.")
            return 0
        verified_role = guild.get_role(cfg["verified_role_id"]) if cfg.get("verified_role_id") else None
        kicked = 0
        for member in list(role.members):
            if member.bot or member == guild.owner or member.top_role >= guild.me.top_role:
                continue
            if verified_role is not None and verified_role in member.roles:
                continue
            if not verification_pro.should_kick(member.joined_at, minutes):
                continue
            label = verification_pro.label_for(verification_pro.KICK_AFTER_CHOICES, minutes)
            if await verification_pro.kick_for(
                member, cfg, reason=f"Didn't verify within {label} (join verification)",
                log_text=f"🚪 **Kicked:** {member.mention} (`{member.id}`) didn't verify within {label}.",
                dm=f"You were removed from **{guild.name}** because you didn't verify within {label}. You're welcome to rejoin and try again.",
            ):
                kicked += 1
            if kicked >= self.MAX_KICKS_PER_PASS:
                break
            await asyncio.sleep(1)
        return kicked

    @tasks.loop(seconds=5)
    async def poll_turnstile_passes(self):
        """Applies roles for members who solved the Cloudflare Turnstile page."""
        if not self._gate.should_run():
            return
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            passes = await db.claim_verification_passes(clone_id)
        except Exception:
            logger.exception("verification: couldn't claim turnstile passes")
            return
        self._gate.record(found_something=bool(passes))
        for row in passes:
            try:
                await apply_verification_pass(self.bot, row)
            except Exception:
                logger.exception("verification: failed applying pass for %s", row.get("user_id"))

    @poll_turnstile_passes.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    # ai_command_guard.execute_ai_command looks this up when
    # requires_confirmation=True (see ModerationCog.AI_CONFIRMED_HANDLERS
    # for the full rationale): confirm_interaction's response is already
    # consumed by AIConfirmView, so setupverification's own
    # interaction.response.defer() would raise discord.InteractionResponded.
    AI_CONFIRMED_HANDLERS = {
        "setupverification": "ai_setupverification",
    }

    @app_commands.command(name="setupverification", description="Set up join verification (anti-raid gate) for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setupverification(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        current = await db.get_verification_config(interaction.guild_id, clone_id=clone_id)
        wizard = WizardView(interaction.user.id, current)
        await interaction.followup.send(view=wizard, ephemeral=True)

    async def ai_setupverification(self, confirm_interaction: discord.Interaction):
        """Entry point for execute_ai_command — confirm_interaction's response
        is already used by AIConfirmView, so this replies via followup and
        never calls interaction.response.defer()/send_message(). Permission
        (manage_guild) was already re-verified by check_real_permission in
        resolve_and_check before this was invoked."""
        clone_id = _clone_id_of(confirm_interaction)
        current = await db.get_verification_config(confirm_interaction.guild_id, clone_id=clone_id)
        wizard = WizardView(confirm_interaction.user.id, current)
        await confirm_interaction.followup.send(view=wizard, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            config = await db.get_verification_config(member.guild.id, clone_id=clone_id)
        except Exception:
            logger.exception("verification: failed to load config for guild %s", member.guild.id)
            return
        if not config.get("enabled") or not config.get("unverified_role_id"):
            return
        # Premium: minimum account age. The role is never given to an account that gets kicked here.
        extras = await verification_pro.effective_for(member.guild.id, clone_id, config)
        min_days = extras.get("min_account_age_days")
        if verification_pro.account_too_young(member.created_at, min_days):
            if await verification_pro.kick_for(
                member, extras, reason=f"Account younger than {min_days} day(s) (join verification)",
                log_text=f"🚫 **Kicked:** {member.mention} (`{member.id}`), account is younger than {min_days} day(s).",
                dm=f"Your account is too new to join **{member.guild.name}** yet. Please try again once it is at least {min_days} day(s) old.",
            ):
                return
        role = member.guild.get_role(config["unverified_role_id"])
        if role is None:
            return
        for attempt in range(3):
            try:
                await member.add_roles(role, reason="Join verification gate")
                if config.get("dm_verify"):
                    # Free for every server. A closed DM is fine: the verify channel is the fallback.
                    if not await send_verify_dm(member, config):
                        await verification_pro.log_event(
                            member.guild, extras, f"💬 Couldn't DM {member.mention} (`{member.id}`): their DMs are closed, "
                            "so they have to verify in the verify channel.")
                return
            except discord.HTTPException:
                if attempt == 2:
                    logger.warning("verification: failed to apply unverified role to %s in guild %s", member.id, member.guild.id)
                    perm_check.flag(
                        member.guild.id, clone_id, "verification_role",
                        "New members aren't getting the Unverified role — "
                        + (perm_check.role_problem(member.guild, role) or "check my permissions."),
                    )
                else:
                    continue


async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
