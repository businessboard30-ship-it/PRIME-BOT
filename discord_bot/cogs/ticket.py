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
import time

import discord
from discord_bot import perm_check
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog
from discord_bot.cogs import _views_ticket_panel as panel
from modules import ticket_panel as tp

from database import db
from discord_bot.cogs._views_ticket_panel import TicketPanelLayout
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


class IntakeModal(discord.ui.Modal):
    """Premium: the member answers one question before the ticket opens, and staff see the answer first."""

    def __init__(self, cog: "TicketCog", category: dict):
        super().__init__(title=f"{category['label']}"[:45])
        self.cog, self.category = cog, category
        self.answer = discord.ui.TextInput(label=tp.QUESTION_LABEL, style=discord.TextStyle.paragraph,
                                           placeholder=category.get("description") or "Tell us a little about it", min_length=1, max_length=700)
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.create_ticket(interaction, self.category, str(self.answer.value).strip())


class TicketCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    async def cog_load(self):
        # Persistent views have no per-message state, so one instance each
        # covers every panel/ticket channel this process owns.
        self.bot.add_view(TicketPanelLayout(self))      # V2 panels: the category dropdown and Open Ticket
        self.bot.add_view(TicketControlView(self))      # Claim / Close (the same ids are used by the V2 ticket message)
        self.autoclose_loop.start()

    async def cog_unload(self):
        self.autoclose_loop.cancel()

    # ── opening ────────────────────────────────────────────────────────

    async def _premium(self, guild_id: int) -> bool:
        try:
            return bool(await db.is_guild_premium_active(guild_id, self._clone_id()))
        except Exception:
            logger.warning("[ticket] couldn't read premium", exc_info=True)
            return False

    async def handle_open(self, interaction: discord.Interaction):
        """The Open Ticket button (free panel, and every panel posted before the dropdown existed)."""
        await self.create_ticket(interaction, None, None)

    async def handle_category(self, interaction: discord.Interaction, key):
        """The category dropdown (premium panels). A server that lost Premium after posting the panel still gets its ticket,
        just without the category and the question."""
        cfg = await db.get_ticket_config(interaction.guild_id, clone_id=self._clone_id())
        category = tp.find_category(cfg.get("categories_json"), key)
        await self._reset_panel(interaction, cfg)
        if category is None or not await self._premium(interaction.guild_id):
            await self.create_ticket(interaction, None, None)
            return
        existing = await db.get_open_ticket_for_opener(interaction.guild_id, interaction.user.id, clone_id=self._clone_id())
        if existing:
            await interaction.response.send_message(f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
            return
        await interaction.response.send_modal(IntakeModal(self, category))

    async def _reset_panel(self, interaction: discord.Interaction, cfg: dict) -> None:
        """Re-draws the panel so the dropdown goes back to its placeholder after someone picked an option."""
        try:
            if interaction.message is not None and interaction.message.flags.components_v2:
                premium = await self._premium(interaction.guild_id)
                await interaction.message.edit(view=panel.build_panel_view(interaction.guild, cfg, premium))
        except Exception:
            logger.debug("[ticket] couldn't reset the panel", exc_info=True)

    async def create_ticket(self, interaction: discord.Interaction, category, topic):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        clone_id = self._clone_id()
        existing = await db.get_open_ticket_for_opener(interaction.guild_id, interaction.user.id, clone_id=clone_id)
        if existing:
            await interaction.followup.send(f"You already have an open ticket: <#{existing['channel_id']}>", ephemeral=True)
            return

        config = await db.get_ticket_config(interaction.guild_id, clone_id=clone_id)
        guild = interaction.guild
        cat_channel = guild.get_channel(config["category_id"]) if config["category_id"] else None
        support_role = guild.get_role(config["support_role_id"]) if config["support_role_id"] else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                          attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, embed_links=True),
        }
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        try:
            channel = await guild.create_text_channel(
                name=tp.ticket_channel_name(interaction.user.name, category), category=cat_channel, overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            perm_check.flag(
                guild.id, clone_id, "ticket_create",
                "Members can't open tickets — I'm missing **Manage Channels** (on my role or the ticket category).",
            )
            await interaction.followup.send("I don't have permission to create ticket channels here.", ephemeral=True)
            return

        await db.create_ticket(interaction.guild_id, channel.id, interaction.user.id, clone_id=clone_id,
                               category=category["key"] if category else None, topic=topic)

        template = config.get("welcome_message") or "Thanks for reaching out, {member}. Support will be with you shortly."
        description = template.replace("{member}", interaction.user.mention).replace("{guild}", guild.name)
        ping = support_role.mention if support_role else ""
        await channel.send(
            view=panel.ticket_view(interaction.user, guild, description, category, topic, ping),
            allowed_mentions=discord.AllowedMentions(roles=[support_role] if support_role else False, users=[interaction.user]))
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
        if ticket.get("status") == "closed":
            await interaction.followup.send("This ticket is already closed.", ephemeral=True)
            return
        await interaction.followup.send("🔒 Closing this ticket and archiving the transcript...")
        await self.close_ticket(interaction.guild, interaction.channel, ticket, f"Closed by {interaction.user.mention}")

    async def close_ticket(self, guild: discord.Guild, channel: discord.TextChannel, ticket: dict, why: str) -> None:
        """Shared by the Close button and the auto-close pass: transcript, DM to the opener, premium transcript log and
        close message, then lock the channel."""
        clone_id = self._clone_id()
        transcript = await self._build_transcript(channel)
        await db.close_ticket(channel.id)
        premium = await self._premium(guild.id)
        cfg = await db.get_ticket_config(guild.id, clone_id=clone_id) if premium else {}
        fname = f"transcript-{channel.name}.txt"

        opener = guild.get_member(ticket["opener_id"])
        if opener is None:
            # Member cache miss (e.g. recent join, incomplete chunking) is NOT the same as "opener left the guild" —
            # get_member can't tell those apart, and treating a miss as "gone" would leave their view_channel override
            # on the "closed" channel intact.
            try:
                opener = await guild.fetch_member(ticket["opener_id"])
            except discord.HTTPException:
                opener = None  # actually not in the guild (or truly unreachable)
        if opener:
            try:
                await opener.send(f"Your ticket in **{guild.name}** was closed.",
                                  file=discord.File(io.BytesIO(transcript.encode()), filename=fname))
            except discord.Forbidden:
                pass  # DMs closed — not fatal, transcript is still attached below

        close_msg = (cfg.get("custom_close_message") or "").strip() if premium else ""
        try:
            await channel.send(close_msg or f"🔒 {why}", file=discord.File(io.BytesIO(transcript.encode()), filename=fname),
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

        log_id = cfg.get("transcript_channel_id") if premium else None
        log_channel = guild.get_channel(log_id) if log_id else None
        if log_channel is not None:
            try:
                who = f"<@{ticket['opener_id']}>"
                label = ticket.get("category") or "no category"
                await log_channel.send(f"🎫 Ticket **{channel.name}** from {who} ({label}) closed. {why}",
                                       file=discord.File(io.BytesIO(transcript.encode()), filename=fname),
                                       allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.info("[ticket] couldn't post the transcript in the log channel of guild %s", guild.id)

        try:
            await channel.edit(name=f"closed-{channel.name}"[:100])
            await channel.set_permissions(guild.default_role, view_channel=False)
            if opener:
                await channel.set_permissions(opener, view_channel=False)
        except discord.HTTPException:
            logger.warning(f"[v0] Couldn't fully lock down closed ticket channel {channel.id}")

    # ── auto-close (premium) ─────────────────────────────────────────

    @tasks.loop(minutes=30)
    async def autoclose_loop(self):
        try:
            await self.autoclose_pass()
        except Exception:
            logger.exception("[ticket] auto-close pass failed")

    @autoclose_loop.before_loop
    async def _before_autoclose(self):
        await self.bot.wait_until_ready()

    async def autoclose_pass(self) -> int:
        """Closes tickets with no message for the server's chosen number of hours. Premium only. At most 20 per pass."""
        clone_id, closed, now = self._clone_id(), 0, time.time()
        cfgs: dict = {}
        for ticket in await db.list_open_tickets(clone_id):
            if closed >= 20:
                break
            guild = self.bot.get_guild(ticket["guild_id"])
            if guild is None:
                continue
            if guild.id not in cfgs:
                cfgs[guild.id] = await db.get_ticket_config(guild.id, clone_id=clone_id) if await self._premium(guild.id) else {}
            hours = cfgs[guild.id].get("auto_close_hours")
            if not hours:
                continue
            channel = guild.get_channel(ticket["channel_id"])
            if channel is None:
                await db.close_ticket(ticket["channel_id"])      # the channel was deleted by hand
                continue
            last = channel.last_message_id
            ts = discord.utils.snowflake_time(last).timestamp() if last else channel.created_at.timestamp()
            if tp.is_inactive(ts, now, hours):
                await channel.send(f"⏰ No activity for {hours}h, closing this ticket.", allowed_mentions=discord.AllowedMentions.none())
                await self.close_ticket(guild, channel, ticket, f"Auto-closed after {hours}h without activity.")
                closed += 1
        return closed

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
        await db.set_ticket_config(interaction.guild_id, clone_id=clone_id, wizard_invoker_id=interaction.user.id)
        config = dict(await db.get_ticket_config(interaction.guild_id, clone_id=clone_id))
        config["clone_id"] = clone_id
        try:
            premium = bool(await db.is_guild_premium_active(interaction.guild_id, clone_id))
        except Exception:
            logger.warning("swallowed exception in TicketCog.setup_cmd", exc_info=True)
            premium = False
        view = build_ticket_wizard_view(interaction.guild, config, premium)
        await interaction.followup.send(view=view)
        sent = await interaction.original_response()
        await remember_ticket_wizard_message(interaction.guild_id, clone_id, interaction.user.id, sent.channel.id, sent.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
