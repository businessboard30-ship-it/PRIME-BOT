"""
Ticket / support system — `/ticket setup` posts a panel with an "Open
Ticket" button. Pressing it creates a private text channel visible only to
the opener + support role + staff (Manage Channels), pings the support
role, and gives staff a Claim/Close button pair on the ticket channel
itself.

Uses real private channels (not threads) so permission isolation is exact
(a thread under a public channel is still readable by anyone who can read
that channel's history unless it's specifically private, and private
threads have their own set of limitations around role-based visibility) —
a channel under a dedicated category is simpler to reason about and matches
what most support bots do.

Both views are persistent (timeout=None + custom_id), rebuilt in cog_load
the same way reaction_roles.py rebuilds its panels, so buttons keep working
across restarts.
"""

import io
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.cogs._views_ticket_wizard import (
    build_wizard_view as build_ticket_wizard_view,
    remember_wizard_message as remember_ticket_wizard_message,
)

logger = logging.getLogger(__name__)


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_open(interaction)


class TicketControlView(discord.ui.View):
    def __init__(self, cog: "TicketCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, emoji="🙋", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_claim(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_close(interaction)


class TicketCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    async def cog_load(self):
        # Persistent views have no per-message state, so one instance each
        # covers every panel/ticket channel this process owns.
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlView(self))

    # ── opening ────────────────────────────────────────────────────────

    async def handle_open(self, interaction: discord.Interaction):
        await interaction.response.defer()
        clone_id = self._clone_id()
        existing = await db.get_open_ticket_for_opener(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        if existing:
            await interaction.followup.send(
                f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True
            )
            return

        config = await db.get_ticket_config(interaction.guild_id, clone_id=clone_id)
        guild = interaction.guild
        category = guild.get_channel(config["category_id"]) if config["category_id"] else None
        support_role = guild.get_role(config["support_role_id"]) if config["support_role_id"] else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        safe_name = f"ticket-{interaction.user.name}".lower().replace(" ", "-")[:90]
        try:
            channel = await guild.create_text_channel(
                name=safe_name, category=category, overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("I don't have permission to create ticket channels here.", ephemeral=True)
            return

        await db.create_ticket(interaction.guild_id, channel.id, interaction.user.id, clone_id=clone_id)

        ping = support_role.mention if support_role else ""
        template = config.get("welcome_message") or "Thanks for reaching out, {member}. Support will be with you shortly."
        description = template.replace("{member}", interaction.user.mention).replace("{guild}", guild.name)
        embed = discord.Embed(
            title="🎫 Support Ticket",
            description=description,
            color=discord.Color.blurple(),
        )
        await channel.send(content=ping or None, embed=embed, view=TicketControlView(self))
        await interaction.followup.send(f"✅ Ticket created: {channel.mention}", ephemeral=True)

    # ── claim / close ─────────────────────────────────────────────────

    async def handle_claim(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket is None:
            await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
            return
        if not _require_perm(interaction, "manage_channels"):
            await interaction.followup.send("Only staff can claim tickets.", ephemeral=True)
            return
        await db.claim_ticket(interaction.channel_id, interaction.user.id)
        await interaction.followup.send(f"🙋 {interaction.user.mention} claimed this ticket.")

    async def handle_close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket is None:
            await interaction.followup.send("This isn't a ticket channel.", ephemeral=True)
            return
        if not _require_perm(interaction, "manage_channels") and interaction.user.id != ticket["opener_id"]:
            await interaction.followup.send("Only staff or the ticket opener can close this.", ephemeral=True)
            return

        await interaction.followup.send("🔒 Closing this ticket and archiving the transcript...")
        transcript = await self._build_transcript(interaction.channel)
        await db.close_ticket(interaction.channel_id)

        opener = interaction.guild.get_member(ticket["opener_id"])
        if opener is None:
            # Member cache miss (e.g. recent join, incomplete chunking) is
            # NOT the same as "opener left the guild" — get_member can't
            # tell those apart, and treating a miss as "gone" would leave
            # their view_channel override on the "closed" channel intact.
            try:
                opener = await interaction.guild.fetch_member(ticket["opener_id"])
            except discord.HTTPException:
                opener = None  # actually not in the guild (or truly unreachable)
        if opener:
            try:
                await opener.send(
                    f"Your ticket in **{interaction.guild.name}** was closed.",
                    file=discord.File(io.BytesIO(transcript.encode()), filename=f"transcript-{interaction.channel.name}.txt"),
                )
            except discord.Forbidden:
                pass  # DMs closed — not fatal, transcript is still attached below

        try:
            await interaction.channel.send(
                file=discord.File(io.BytesIO(transcript.encode()), filename=f"transcript-{interaction.channel.name}.txt")
            )
        except discord.HTTPException:
            pass

        try:
            await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:100])
            await interaction.channel.set_permissions(interaction.guild.default_role, view_channel=False)
            if opener:
                await interaction.channel.set_permissions(opener, view_channel=False)
        except discord.HTTPException:
            logger.warning(f"[v0] Couldn't fully lock down closed ticket channel {interaction.channel.id}")

    async def _build_transcript(self, channel: discord.TextChannel) -> str:
        lines = []
        async for message in channel.history(limit=500, oldest_first=True):
            ts = message.created_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{ts}] {message.author}: {message.content}")
        return "\n".join(lines) or "(no messages)"

    # ── setup commands ────────────────────────────────────────────────

    group = app_commands.guild_only()(app_commands.Group(name="ticket", description="Configure the ticket system"))

    @group.command(name="setup", description="Set up the ticket system with a guided step-by-step wizard")
    async def setup_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return

        clone_id = _clone_id_of(interaction)
        config = await db.get_ticket_config(interaction.guild_id, clone_id=clone_id)
        view = build_ticket_wizard_view(interaction.guild_id, clone_id, interaction.user.id, config)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_ticket_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
