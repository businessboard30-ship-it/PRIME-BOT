# path: discord_bot/cogs/ai_jobs.py

"""
Persistent AI scheduler (Phase 3). Jobs are rows in ai_scheduled_jobs, so they survive restarts; a DB-driven poller
(same pattern as schedule.py) claims due jobs atomically, runs them one at a time, and records history.

Option 1 "safe first": only allow-listed, non-destructive steps (post a message). All rules live in modules/ai_jobs.py.

  /aijobs create   -- one or more messages, on a delay, an interval, or daily (UTC)
  /aijobs list | history | pause | resume | cancel
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from discord_bot.cogs._adaptive_skip import AdaptiveSkip
from discord_bot.cogs.schedule import _parse_duration, _parse_time_of_day
from modules import ai_jobs, ai_plan

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
MAX_JOBS_PER_TICK = 5
_ICON = {"scheduled": "\U0001f7e2", "running": "\u2699\ufe0f", "retrying": "\U0001f501", "paused": "\u23f8\ufe0f",
         "done": "\u2705", "failed": "\u274c", "disabled": "\u26d4"}


def _clone_id_of(interaction: discord.Interaction):
    return getattr(interaction.client, "clone_id", None)


def _can_manage(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and bool(getattr(interaction.permissions, "manage_guild", False))


class AIJobsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._gate = AdaptiveSkip(idle_threshold=5, idle_skip=5)
        self._poller.start()

    def cog_unload(self):
        self._poller.cancel()

    # ---- background runner ---------------------------------------------------------------------------------
    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _poller(self):
        if not self._gate.should_run():
            return
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            for job in await db.get_interrupted_ai_jobs(clone_id):
                await self._finish(job, ai_jobs.INTERRUPTED,
                                   "The bot restarted while this job was running; it was not repeated automatically.")
            due = await db.claim_due_ai_jobs(clone_id, MAX_JOBS_PER_TICK, ai_jobs.CLAIM_LOCK_SECONDS)
        except Exception:
            logger.exception("[ai_jobs] poll failed")
            return
        self._gate.record(found_something=bool(due))
        for job in due:                       # strictly one at a time: no request bursts
            try:
                status, report = await ai_jobs.execute_job(job, self._sender(), self._permission_check(job))
            except Exception:
                logger.exception("[ai_jobs] job %s crashed", job.get("id"))
                status, report = ai_jobs.FAILED, "Unexpected error (see logs)."
            try:
                await self._finish(job, status, report)
            except Exception:
                logger.exception("[ai_jobs] could not record result of job %s", job.get("id"))

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()

    async def _finish(self, job: dict, status: str, report: str):
        out = ai_jobs.after_run(job, status, datetime.now(timezone.utc))
        await db.finish_ai_job(job["id"], job_status=out["job_status"], run_status=status, report=report,
                               enabled=out["enabled"], next_run_at=out["next_run_at"],
                               consecutive_failures=out["consecutive_failures"],
                               error=None if status == ai_jobs.OK else report[:300])

    def _sender(self):
        async def send(step: dict) -> None:
            channel = self.bot.get_channel(step["channel_id"])
            try:
                if channel is None:
                    channel = await self.bot.fetch_channel(step["channel_id"])
                await channel.send(step["content"], allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                raise ai_plan.classify_http_status(exc.status, text=getattr(exc, "text", "") or "")
        return send

    def _permission_check(self, job: dict):
        async def can_run() -> str:
            guild = self.bot.get_guild(job["guild_id"])
            if guild is None:
                return "the bot is no longer in that server"
            member = guild.get_member(job["created_by"])
            if member is None:
                try:
                    member = await guild.fetch_member(job["created_by"])
                except discord.HTTPException:
                    return "the person who created this job is no longer in the server"
            if not member.guild_permissions.manage_guild:
                return "the person who created this job no longer has Manage Server"
            for step in job["steps"]:
                ch = guild.get_channel(step["channel_id"])
                if ch is None:
                    return f"channel <#{step['channel_id']}> no longer exists"
                if not ch.permissions_for(member).send_messages:
                    return f"the creator can no longer post in <#{step['channel_id']}>"
            return ""
        return can_run

    # ---- commands ------------------------------------------------------------------------------------------
    group = app_commands.guild_only()(app_commands.Group(name="aijobs", description="Scheduled multi-step jobs"))

    @group.command(name="create", description="Schedule one or more messages (once, repeating, or daily)")
    @app_commands.describe(title="Short name", channel="Channel to post in", text="Message 1",
                           text2="Message 2 (sent after message 1)", text3="Message 3",
                           delay="Run once after e.g. 2h, 30m, 1d", every="Repeat every e.g. 6h, 1d (min 5m)",
                           daily="Run every day at a UTC time, e.g. 09:00")
    async def create(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, ai_jobs.MAX_TITLE],
                     channel: discord.TextChannel, text: app_commands.Range[str, 1, ai_jobs.MAX_CONTENT],
                     text2: app_commands.Range[str, 1, ai_jobs.MAX_CONTENT] = None,
                     text3: app_commands.Range[str, 1, ai_jobs.MAX_CONTENT] = None,
                     delay: str = None, every: str = None, daily: str = None):
        await interaction.response.defer(ephemeral=True)
        if not _can_manage(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        if not channel.permissions_for(interaction.user).send_messages:
            await interaction.followup.send("You can't post in that channel yourself, so I won't schedule it.", ephemeral=True)
            return
        if sum(x is not None for x in (delay, every, daily)) != 1:
            await interaction.followup.send("Give exactly one of **delay**, **every** or **daily**.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        interval = daily_at = None
        if delay is not None:
            kind, secs = "once", _parse_duration(delay)
            if not secs:
                await interaction.followup.send("Couldn't parse that delay: try `2h`, `30m` or `1d`.", ephemeral=True)
                return
            first = now + timedelta(seconds=secs)
        elif every is not None:
            kind, interval = "interval", _parse_duration(every)
            first = now + timedelta(seconds=interval or 0)
        else:
            kind, daily_at = "daily", daily.strip()
            parsed = _parse_time_of_day(daily_at)
            if not parsed:
                await interaction.followup.send("Daily time must look like `09:00` (UTC).", ephemeral=True)
                return
            first = ai_jobs.next_run_after("daily", now, daily_at=f"{parsed[0]}:{parsed[1]}")
        err = ai_jobs.validate_schedule(kind, interval, daily_at)
        steps, serr = ai_jobs.validate_steps(
            [{"kind": "send_message", "channel_id": channel.id, "content": t} for t in (text, text2, text3) if t])
        if err or steps is None:
            await interaction.followup.send(err or serr, ephemeral=True)
            return
        cid = _clone_id_of(interaction)
        if await db.count_ai_jobs(interaction.guild_id, cid) >= ai_jobs.MAX_JOBS_PER_GUILD:
            await interaction.followup.send(f"This server already has {ai_jobs.MAX_JOBS_PER_GUILD} jobs. Cancel one first.", ephemeral=True)
            return
        job = await db.create_ai_job(interaction.guild_id, interaction.user.id, title.strip(), steps, kind, first,
                                     interval_seconds=interval, daily_at=daily_at, clone_id=cid)
        await interaction.followup.send(
            f"\u2705 Job `#{job['id']}` **{job['title']}** scheduled: {len(steps)} message(s) in {channel.mention}, "
            f"first run <t:{int(first.timestamp())}:R>.", ephemeral=True)

    @group.command(name="list", description="List scheduled jobs in this server")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        jobs = await db.list_ai_jobs(interaction.guild_id, _clone_id_of(interaction))
        if not jobs:
            await interaction.followup.send("No scheduled jobs in this server.", ephemeral=True)
            return
        embed = discord.Embed(title="Scheduled jobs", color=discord.Color.blurple())
        for j in jobs:
            nxt = f"next <t:{int(j['next_run_at'].timestamp())}:R>" if j["enabled"] else "not scheduled"
            last = f" \u00b7 last: {j['last_status']}" if j.get("last_status") else ""
            embed.add_field(name=f"{_ICON.get(j['status'], '')} #{j['id']} {j['title']}",
                            value=f"{j['schedule_kind']} \u00b7 {len(j['steps'])} step(s) \u00b7 {nxt}{last}", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="history", description="Recent runs of a job")
    @app_commands.describe(job_id="The # shown in /aijobs list")
    async def history(self, interaction: discord.Interaction, job_id: int):
        await interaction.response.defer(ephemeral=True)
        job = await db.get_ai_job(interaction.guild_id, job_id, _clone_id_of(interaction))
        if not job:
            await interaction.followup.send(f"No job `#{job_id}` here.", ephemeral=True)
            return
        runs = await db.list_ai_job_runs(job_id, 5)
        if not runs:
            await interaction.followup.send(f"Job `#{job_id}` hasn't run yet.", ephemeral=True)
            return
        text = "\n\n".join(f"<t:{int(r['started_at'].timestamp())}:f> \u2014 **{r['status']}**\n{(r['report'] or '')[:350]}" for r in runs)
        await interaction.followup.send(text[:1900], ephemeral=True)

    @group.command(name="pause", description="Pause a job")
    @app_commands.describe(job_id="The # shown in /aijobs list")
    async def pause(self, interaction: discord.Interaction, job_id: int):
        await self._toggle(interaction, job_id, False)

    @group.command(name="resume", description="Resume a paused, failed or finished job")
    @app_commands.describe(job_id="The # shown in /aijobs list")
    async def resume(self, interaction: discord.Interaction, job_id: int):
        await self._toggle(interaction, job_id, True)

    async def _toggle(self, interaction: discord.Interaction, job_id: int, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        if not _can_manage(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        cid = _clone_id_of(interaction)
        job = await db.get_ai_job(interaction.guild_id, job_id, cid)
        if not job:
            await interaction.followup.send(f"No job `#{job_id}` here.", ephemeral=True)
            return
        nxt = None
        if enabled:
            now = datetime.now(timezone.utc)
            if job["schedule_kind"] == "once":
                nxt = now + timedelta(minutes=1)          # re-arm a finished/failed one-off: runs again shortly
            elif job["next_run_at"] <= now:               # never a catch-up burst: jump to the next future slot
                nxt = ai_jobs.next_run_after(job["schedule_kind"], now, interval_seconds=job.get("interval_seconds"),
                                             daily_at=job.get("daily_at"))
        ok = await db.set_ai_job_enabled(interaction.guild_id, job_id, enabled, next_run_at=nxt, clone_id=cid)
        await interaction.followup.send(
            (f"\u2705 Job `#{job_id}` {'resumed' if enabled else 'paused'}." if ok
             else f"Couldn't change `#{job_id}` right now (it may be running). Try again in a minute."), ephemeral=True)

    @group.command(name="cancel", description="Delete a job and its history")
    @app_commands.describe(job_id="The # shown in /aijobs list")
    async def cancel(self, interaction: discord.Interaction, job_id: int):
        await interaction.response.defer(ephemeral=True)
        if not _can_manage(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        ok = await db.delete_ai_job(interaction.guild_id, job_id, _clone_id_of(interaction))
        await interaction.followup.send(f"\u2705 Deleted `#{job_id}`." if ok else f"No job `#{job_id}` here.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIJobsCog(bot))
