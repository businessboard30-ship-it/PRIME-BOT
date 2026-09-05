# path: discord_bot/cogs/verification.py

"""
Join verification / anti-raid gate.

One command only — /setupverification — everything else is the wizard
flow below (mode, channel, role pickers) plus one shared on_member_join
listener that applies whichever mode the guild picked. This mirrors the
guild-scoped-config pattern already used by automod/ticket/welcome: the
listener and the persistent button (_views_verification.py) both read
discord_verification_config fresh each time, so behaviour is uniform
across the bot and every clone without any bot-scoped code branching.

Wizard state (mode / channel / role picks) lives on the WizardView
instance itself while the admin is stepping through it — same as other
admin-only setup wizards in this codebase that don't need to survive a
restart mid-flow, unlike the persistent verify button which does.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs._views_verification import (
    build_verify_panel_embed,
    build_verify_panel_view,
    lockdown_guild_channels,
)
from database import db

logger = logging.getLogger(__name__)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


class ModeSelect(discord.ui.Select):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="1. Choose verification mode",
            options=[
                discord.SelectOption(label="Button click (low friction)", value="button", emoji="✅"),
                discord.SelectOption(label="Captcha (blocks scripted joins)", value="captcha", emoji="🧩"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.mode = self.values[0]
        await self.wizard.refresh(interaction)


class ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            placeholder="2. Choose the #verify channel",
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        self.wizard.channel_id = self.values[0].id
        await self.wizard.refresh(interaction)


class UnverifiedRoleSelect(discord.ui.RoleSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(placeholder="3. Choose the Unverified role")

    async def callback(self, interaction: discord.Interaction):
        self.wizard.unverified_role_id = self.values[0].id
        await self.wizard.refresh(interaction)


class AutoCreateUnverifiedButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(
            label="✨ Auto-create Unverified role",
            style=discord.ButtonStyle.primary,
            row=4,
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
        # instead of spawning a duplicate "Unverified" role.
        if wizard.auto_created_role_id:
            existing = interaction.guild.get_role(wizard.auto_created_role_id)
            if existing is not None:
                wizard.unverified_role_id = existing.id
                await interaction.response.send_message(
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
            await interaction.response.send_message(
                f"A role called {existing_by_name.mention} already exists in this server — "
                "reusing it instead of creating a duplicate.",
                ephemeral=True,
            )
            return

        wizard._creating_role = True
        guild = interaction.guild
        bot_member = guild.me

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
        try:
            target_position = max(1, bot_member.top_role.position - 1)
            await role.edit(position=target_position)
        except discord.HTTPException:
            # Non-fatal — the role still works, it just may need manual
            # repositioning if it ended up above the bot's top role.
            logger.warning(
                "verification: auto-created Unverified role %s in guild %s but failed to reposition it",
                role.id, guild.id,
            )

        wizard.unverified_role_id = role.id
        wizard.auto_created_role_id = role.id
        wizard._creating_role = False
        await wizard.refresh(interaction)
        await interaction.followup.send(
            f"✅ Created {role.mention} and selected it as your Unverified role.",
            ephemeral=True,
        )


class VerifiedRoleSelect(discord.ui.RoleSelect):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(placeholder="4. (Optional) Choose a Verified role", min_values=0, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.verified_role_id = self.values[0].id if self.values else None
        await self.wizard.refresh(interaction)


class FinishButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Lock down & enable", style=discord.ButtonStyle.success, emoji="🔒", row=4)

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

        if unverified_role.position >= guild.me.top_role.position:
            await interaction.followup.send(
                f"⚠️ {unverified_role.mention} is positioned at or above my highest role, so I won't be able to "
                "assign/remove it on join/verify. Move it below my role in Server Settings → Roles, then run "
                "/setupverification again.",
                ephemeral=True,
            )
            return

        touched = await lockdown_guild_channels(guild, unverified_role, wizard.channel_id)

        panel_embed = build_verify_panel_embed(guild.name, wizard.mode)
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

        clone_id = _clone_id_of(interaction)
        await db.set_verification_config(
            guild.id,
            clone_id=clone_id,
            mode=wizard.mode,
            channel_id=wizard.channel_id,
            unverified_role_id=wizard.unverified_role_id,
            verified_role_id=wizard.verified_role_id,
            message_id=panel_message.id,
            enabled=True,
        )

        await interaction.followup.send(
            f"✅ Verification is live — locked down {touched} channel(s) and posted the panel in {channel.mention}.",
            ephemeral=True,
        )
        wizard.stop()


class CancelButton(discord.ui.Button):
    def __init__(self, wizard: "WizardView"):
        self.wizard = wizard
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        self.wizard.stop()
        await interaction.response.edit_message(content="Verification setup cancelled.", embed=None, view=None)


class WizardView(discord.ui.View):
    def __init__(self, invoker_id: int, current: dict):
        super().__init__(timeout=600)
        self.invoker_id = invoker_id
        self.mode = current.get("mode") or "button"
        self.channel_id = current.get("channel_id")
        self.unverified_role_id = current.get("unverified_role_id")
        self.verified_role_id = current.get("verified_role_id")
        self.auto_created_role_id = None
        self._creating_role = False
        self.add_item(ModeSelect(self))
        self.add_item(ChannelSelect(self))
        self.add_item(UnverifiedRoleSelect(self))
        self.add_item(VerifiedRoleSelect(self))
        self.add_item(AutoCreateUnverifiedButton(self))
        self.add_item(FinishButton(self))
        self.add_item(CancelButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran /setupverification can use this.", ephemeral=True)
            return False
        return True

    def build_embed(self) -> discord.Embed:
        def fmt_channel(cid):
            return f"<#{cid}>" if cid else "*not set*"

        def fmt_role(rid):
            return f"<@&{rid}>" if rid else "*not set*"

        embed = discord.Embed(
            title="Join verification setup",
            description=(
                "New members get the Unverified role on join and can only see the verify channel "
                "until they pass this. Pick your options below, then hit **Lock down & enable** — "
                "the bot will apply the channel overwrites automatically."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Mode", value="Captcha 🧩" if self.mode == "captcha" else "Button click ✅", inline=False)
        embed.add_field(name="Verify channel", value=fmt_channel(self.channel_id), inline=True)
        embed.add_field(name="Unverified role", value=fmt_role(self.unverified_role_id), inline=True)
        embed.add_field(name="Verified role", value=fmt_role(self.verified_role_id) if self.verified_role_id else "*none — just removes Unverified*", inline=True)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class VerificationCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setupverification", description="Set up join verification (anti-raid gate) for this server")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setupverification(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        clone_id = _clone_id_of(interaction)
        current = await db.get_verification_config(interaction.guild_id, clone_id=clone_id)
        wizard = WizardView(interaction.user.id, current)
        await interaction.followup.send(embed=wizard.build_embed(), view=wizard, ephemeral=True)

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
        role = member.guild.get_role(config["unverified_role_id"])
        if role is None:
            return
        for attempt in range(3):
            try:
                await member.add_roles(role, reason="Join verification gate")
                return
            except discord.HTTPException:
                if attempt == 2:
                    logger.warning("verification: failed to apply unverified role to %s in guild %s", member.id, member.guild.id)
                else:
                    continue


async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
