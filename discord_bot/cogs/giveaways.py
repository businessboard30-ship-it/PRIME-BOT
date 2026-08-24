"""
Giveaways — `/giveaway start prize:'Nitro' duration:1h winners:1` posts an
embed with a button entry (button instead of emoji reaction so it survives
restarts the same way ticket.py/reaction_roles.py's persistent views do,
and avoids needing raw-reaction tracking just for this one feature).

A 30s background loop (`_poller`) is the single source of truth for ending
giveaways — it queries discord_giveaways for anything past ends_at rather
than scheduling one asyncio task per giveaway, so a bot restart never loses
track of a giveaway mid-flight.
"""

import logging
import random
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.cogs._views_giveaway_wizard import build_wizard_view as build_giveaway_wizard_view

logger = logging.getLogger(__name__)

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def _parse_duration(text: str) -> int | None:
    """Parses strings like '1h', '30m', '2d12h' into total seconds. Returns
    None if nothing matched. Sums every unit found rather than requiring one
    single token, so '1h30m' works alongside plain '1h'."""
    matches = DURATION_RE.findall(text.strip())
    if not matches:
        return None
    total = 0
    for amount, unit in matches:
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _giveaway_embed(prize: str, winner_count: int, ends_at: datetime, host: discord.abc.User, entrant_count: int,
                     ended: bool = False, role_requirement_id: int = None) -> discord.Embed:
    requirement_line = f"**Requires role:** <@&{role_requirement_id}>\n" if role_requirement_id else ""
    embed = discord.Embed(
        title=f"🎉 Giveaway: {prize}",
        description=(
            f"Click 🎉 **Enter** below to join!\n\n"
            f"{requirement_line}"
            f"**Winners:** {winner_count}\n"
            f"**Ends:** <t:{int(ends_at.timestamp())}:R>\n"
            f"**Entries:** {entrant_count}"
        ),
        color=discord.Color.gold() if not ended else discord.Color.dark_grey(),
    )
    embed.set_footer(text=f"Hosted by {host.display_name}")
    return embed


class GiveawayEntryView(discord.ui.View):
    def __init__(self, cog: "GiveawayCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Enter", style=discord.ButtonStyle.success, emoji="🎉", custom_id="giveaway:enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_entry(interaction)


class GiveawayCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._poller.start()

    def cog_unload(self):
        self._poller.cancel()

    async def cog_load(self):
        self.bot.add_view(GiveawayEntryView(self))

    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    async def handle_entry(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        giveaway = await db.get_giveaway_by_message(interaction.message.id)
        if giveaway is None or giveaway["status"] != "active":
            await interaction.followup.send("This giveaway has already ended.", ephemeral=True)
            return

        if interaction.user.id in giveaway["entrant_ids"]:
            updated = await db.remove_giveaway_entrant(interaction.message.id, interaction.user.id)
            await interaction.followup.send("➖ You left the giveaway.", ephemeral=True)
        else:
            role_requirement_id = giveaway.get("role_requirement_id")
            if role_requirement_id and isinstance(interaction.user, discord.Member):
                if not any(r.id == role_requirement_id for r in interaction.user.roles):
                    await interaction.followup.send(
                        f"You need the <@&{role_requirement_id}> role to enter this giveaway.", ephemeral=True
                    )
                    return
            updated = await db.add_giveaway_entrant(interaction.message.id, interaction.user.id)
            await interaction.followup.send("🎉 You're in!", ephemeral=True)

        if updated:
            embed = _giveaway_embed(
                updated["prize"], updated["winner_count"], updated["ends_at"], interaction.client.user,
                len(updated["entrant_ids"]), role_requirement_id=updated.get("role_requirement_id"),
            )
            try:
                await interaction.message.edit(embed=embed)
            except discord.HTTPException:
                pass

    def _pick_winners(self, entrant_ids: list, count: int) -> list:
        pool = list(entrant_ids)
        random.shuffle(pool)
        return pool[:count]

    async def _finish(self, giveaway: dict):
        winners = self._pick_winners(giveaway["entrant_ids"], giveaway["winner_count"])
        await db.finish_giveaway(giveaway["message_id"], winners)

        channel = self.bot.get_channel(giveaway["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(giveaway["channel_id"])
            except discord.HTTPException:
                logger.warning(
                    f"[v0] Giveaway {giveaway['id']} ended but channel {giveaway['channel_id']} "
                    f"is unreachable (deleted or cache miss) — winners picked but never announced."
                )
                return
        try:
            message = await channel.fetch_message(giveaway["message_id"])
        except discord.HTTPException:
            message = None

        host = self.bot.get_user(giveaway["host_id"]) or self.bot.user
        embed = _giveaway_embed(
            giveaway["prize"], giveaway["winner_count"], giveaway["ends_at"], host,
            len(giveaway["entrant_ids"]), ended=True, role_requirement_id=giveaway.get("role_requirement_id"),
        )
        if message:
            try:
                await message.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

        if not winners:
            try:
                await channel.send(f"🎉 The giveaway for **{giveaway['prize']}** ended with no entrants.")
            except discord.HTTPException:
                pass
            return
        mentions = ", ".join(f"<@{w}>" for w in winners)
        try:
            await channel.send(f"🎉 Congratulations {mentions}! You won **{giveaway['prize']}**!")
        except discord.HTTPException:
            pass

    @tasks.loop(seconds=30)
    async def _poller(self):
        try:
            active = await db.get_active_giveaways(getattr(self.bot, "clone_id", None))
        except Exception:
            logger.exception("[v0] Failed to poll active giveaways")
            return
        now = datetime.now(timezone.utc)
        for giveaway in active:
            ends_at = giveaway["ends_at"]
            if ends_at.tzinfo is None:
                ends_at = ends_at.replace(tzinfo=timezone.utc)
            if ends_at <= now:
                try:
                    await self._finish(giveaway)
                except Exception:
                    logger.exception(f"[v0] Failed to finish giveaway {giveaway['id']}")

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()

    group = app_commands.guild_only()(app_commands.Group(name="giveaway", description="Run giveaways"))

    @group.command(name="setup", description="Create a giveaway with a guided step-by-step wizard")
    async def setup_wizard(self, interaction: discord.Interaction):
        if not _require_perm(interaction, "manage_guild"):
            await interaction.response.send_message("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        # Draft row is keyed by the wizard message's own id, which doesn't
        # exist yet — placeholder row created with message_id 0 first,
        # then immediately corrected to the real id once Discord returns
        # it, same two-step "post, then learn your own message id" shape
        # /ticket setup and the others use for their wizard_message_id
        # pointer (just persisted a message earlier here, since this
        # wizard's whole state lives in that row).
        await interaction.response.send_message(view=build_giveaway_wizard_view(0, {}))
        sent = await interaction.original_response()
        await db.upsert_giveaway_draft(
            sent.id, interaction.guild_id, sent.channel.id, interaction.user.id,
            clone_id=_clone_id_of(interaction),
        )
        view = build_giveaway_wizard_view(sent.id, await db.get_giveaway_draft(sent.id))
        await sent.edit(view=view)

    @group.command(name="start", description="Start a giveaway")
    @app_commands.describe(prize="What's being given away", duration="e.g. 1h, 30m, 2d12h", winners="Number of winners")
    async def start(self, interaction: discord.Interaction, prize: app_commands.Range[str, 1, 200],
                     duration: str, winners: app_commands.Range[int, 1, 20] = 1):
        if not _require_perm(interaction, "manage_guild"):
            await interaction.response.send_message("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        seconds = _parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                "Couldn't parse that duration — try something like `1h`, `30m`, or `2d12h`.", ephemeral=True
            )
            return
        ends_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)

        embed = _giveaway_embed(prize, winners, ends_at, interaction.user, 0)
        await interaction.response.send_message(embed=embed, view=GiveawayEntryView(self))
        message = await interaction.original_response()

        await db.create_giveaway(
            interaction.guild_id, interaction.channel_id, message.id, interaction.user.id,
            prize, winners, ends_at, clone_id=_clone_id_of(interaction),
        )

    @group.command(name="reroll", description="Reroll winner(s) for an ended giveaway")
    @app_commands.describe(message_id="The giveaway message ID")
    async def reroll(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        try:
            mid = int(message_id)
        except ValueError:
            await interaction.followup.send("That doesn't look like a valid message ID.", ephemeral=True)
            return
        giveaway = await db.get_giveaway_by_message(mid)
        if giveaway is None or giveaway["status"] != "ended":
            await interaction.followup.send("No ended giveaway found with that message ID.", ephemeral=True)
            return
        if not giveaway["entrant_ids"]:
            await interaction.followup.send("That giveaway had no entrants to reroll from.", ephemeral=True)
            return

        new_winners = self._pick_winners(giveaway["entrant_ids"], giveaway["winner_count"])
        await db.set_giveaway_winners(mid, new_winners)
        mentions = ", ".join(f"<@{w}>" for w in new_winners)
        await interaction.followup.send(f"🎉 New winner(s) for **{giveaway['prize']}**: {mentions}")


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))
