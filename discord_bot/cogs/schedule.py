"""
General-purpose scheduled messages — distinct from autopost.py, which only
rotates a fixed self-promo content library. This lets an admin schedule
their OWN arbitrary text, either once or on a recurring interval, reusing
the same "DB-driven poller, not one asyncio task per job" background-loop
pattern as autopost.py/giveaways.py so schedules survive restarts.

Three entry points instead of trying to parse arbitrary natural language
like "every day 9am":
  /schedule once     — fires one time after a duration ("in 2h")
  /schedule recurring — fires repeatedly on a fixed interval ("every 1d")
  /schedule daily    — fires once every day at a UTC time ("09:00")
Between them these cover one-off reminders, interval reposts, and daily
announcements without needing a full cron/NLP parser.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_duration(text: str):
    matches = DURATION_RE.findall(text.strip())
    if not matches:
        return None
    total = 0
    for amount, unit in matches:
        total += int(amount) * UNIT_SECONDS[unit.lower()]
    return total or None


def _parse_time_of_day(text: str):
    m = TIME_RE.match(text.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour < 24 and 0 <= minute < 60:
        return hour, minute
    return None


def _require_perm(interaction: discord.Interaction, perm: str) -> bool:
    if interaction.guild is None:
        return False
    return getattr(interaction.permissions, perm, False)


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._poller.start()

    def cog_unload(self):
        self._poller.cancel()

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _poller(self):
        try:
            due = await db.get_due_scheduled_messages(getattr(self.bot, "clone_id", None))
        except Exception:
            logger.exception("[v0] Failed to poll due scheduled messages")
            return

        for job in due:
            channel = self.bot.get_channel(job["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(job["channel_id"])
                except discord.HTTPException:
                    logger.warning(
                        f"[v0] Scheduled message {job['id']} channel {job['channel_id']} is "
                        f"unreachable (deleted or cache miss) — disabling this schedule."
                    )
                    try:
                        await db.advance_or_disable_scheduled_message(job["id"])
                    except Exception:
                        logger.exception(f"[v0] Failed to disable scheduled message {job['id']} (missing channel)")
                    continue
            try:
                await channel.send(job["content"])
            except discord.HTTPException:
                logger.warning(f"[v0] Failed to send scheduled message {job['id']} in channel {job['channel_id']}")

            # Isolate this per-job: an uncaught exception here would abort
            # the loop for every remaining due job this tick AND leave this
            # job's next_run_at stale, causing a duplicate send next cycle.
            try:
                if job["interval_seconds"]:
                    next_run = job["next_run_at"] + timedelta(seconds=job["interval_seconds"])
                    now = datetime.now(timezone.utc)
                    while next_run <= now:
                        next_run += timedelta(seconds=job["interval_seconds"])
                    await db.advance_or_disable_scheduled_message(job["id"], next_run_at=next_run)
                else:
                    await db.advance_or_disable_scheduled_message(job["id"])
            except Exception:
                logger.exception(f"[v0] Failed to advance scheduled message {job['id']}")

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()

    group = app_commands.guild_only()(app_commands.Group(name="schedule", description="Schedule messages to a channel"))

    @group.command(name="once", description="Send a message once, after a delay")
    @app_commands.describe(channel="Channel to post in", delay="e.g. 2h, 30m, 1d", text="Message to send")
    async def once(self, interaction: discord.Interaction, channel: discord.TextChannel,
                    delay: str, text: app_commands.Range[str, 1, 2000]):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        seconds = _parse_duration(delay)
        if seconds is None:
            await interaction.followup.send("Couldn't parse that delay — try `2h`, `30m`, or `1d`.", ephemeral=True)
            return
        run_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        job = await db.create_scheduled_message(
            interaction.guild_id, channel.id, text, run_at, None, interaction.user.id, clone_id=_clone_id_of(interaction)
        )
        await interaction.followup.send(
            f"✅ Scheduled `#{job['id']}` — I'll post in {channel.mention} <t:{int(run_at.timestamp())}:R>.",
            ephemeral=True,
        )

    @group.command(name="recurring", description="Repost a message on a fixed interval")
    @app_commands.describe(channel="Channel to post in", interval="e.g. 1d, 12h, 30m", text="Message to send")
    async def recurring(self, interaction: discord.Interaction, channel: discord.TextChannel,
                         interval: str, text: app_commands.Range[str, 1, 2000]):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        seconds = _parse_duration(interval)
        if seconds is None or seconds < 60:
            await interaction.followup.send("Couldn't parse that interval — try `1d`, `12h`, or `30m` (minimum 1m).", ephemeral=True)
            return
        run_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        job = await db.create_scheduled_message(
            interaction.guild_id, channel.id, text, run_at, seconds, interaction.user.id, clone_id=_clone_id_of(interaction)
        )
        await interaction.followup.send(
            f"✅ Scheduled `#{job['id']}` — I'll post in {channel.mention} every **{interval}**, starting <t:{int(run_at.timestamp())}:R>.",
            ephemeral=True,
        )

    @group.command(name="daily", description="Post a message once every day at a UTC time")
    @app_commands.describe(channel="Channel to post in", time_utc="24h UTC time, e.g. 09:00", text="Message to send")
    async def daily(self, interaction: discord.Interaction, channel: discord.TextChannel,
                     time_utc: str, text: app_commands.Range[str, 1, 2000]):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        parsed = _parse_time_of_day(time_utc)
        if parsed is None:
            await interaction.followup.send("Couldn't parse that time — use 24h UTC format like `09:00` or `21:30`.", ephemeral=True)
            return
        hour, minute = parsed
        now = datetime.now(timezone.utc)
        run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        job = await db.create_scheduled_message(
            interaction.guild_id, channel.id, text, run_at, 86400, interaction.user.id, clone_id=_clone_id_of(interaction)
        )
        await interaction.followup.send(
            f"✅ Scheduled `#{job['id']}` — I'll post in {channel.mention} daily at **{time_utc} UTC**, starting <t:{int(run_at.timestamp())}:R>.",
            ephemeral=True,
        )

    @group.command(name="list", description="List scheduled messages in this server")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        jobs = await db.list_scheduled_messages(interaction.guild_id, clone_id=_clone_id_of(interaction))
        if not jobs:
            await interaction.followup.send("No scheduled messages in this server.", ephemeral=True)
            return
        embed = discord.Embed(title="Scheduled messages", color=discord.Color.blurple())
        for j in jobs:
            state = "🟢" if j["enabled"] else "🔴"
            kind = f"every {j['interval_seconds']}s" if j["interval_seconds"] else "one-off"
            preview = j["content"][:50] + ("…" if len(j["content"]) > 50 else "")
            embed.add_field(
                name=f"{state} #{j['id']} — <#{j['channel_id']}>",
                value=f"{kind} · next <t:{int(j['next_run_at'].timestamp())}:R>\n\"{preview}\"",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="cancel", description="Cancel a scheduled message")
    @app_commands.describe(schedule_id="The # shown in /schedule list")
    async def cancel(self, interaction: discord.Interaction, schedule_id: int):
        await interaction.response.defer(ephemeral=True)
        if not _require_perm(interaction, "manage_guild"):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        ok = await db.delete_scheduled_message(interaction.guild_id, schedule_id, clone_id=_clone_id_of(interaction))
        await interaction.followup.send(
            f"✅ Cancelled `#{schedule_id}`." if ok else f"No schedule `#{schedule_id}` found here.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
