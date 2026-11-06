# path: discord_bot/cogs/test_harness.py

"""
AI-ONLY bot test harness -- phases 1-2 (docs/BOT_TEST_HARNESS_PLAN.md).

Like discord_bot/cogs/ai_server_ops.py this cog is NEVER added to the slash tree: setup() hands the commands to the
AI-only registry, so nothing shows in Discord's command list and no command slot is used. Do NOT add it to the tree,
do NOT mention it in the README or help text.

Commands (all test-guild-only, owner-only, main-bot-only):
  testharness status    read-only
  testharness snapshot  saves + verifies a snapshot, arms the dead-man timer. NEEDS A CONFIRM CLICK, because the restore
                        that follows removes anything added to the server after the snapshot.
  testharness stop      the kill switch: cancel the run and restore right now (no confirm: it must be instant)

Three gates (rule 2): offer + execution gates live in modules.ai_command_guard; the HANDLER gate is `handler_gate` here,
called at the top of every handler. The timer and restart recovery have no asker, so they use th.check_guild (enabled,
main bot, test guild) and, inside the restore, re-check the gate before every single Discord call.

Dead-man timer + restart recovery: a 30 s loop restores any unfinished run whose deadline passed; its FIRST tick after a
restart restores every unfinished run regardless of deadline. The loop is started from setup() because this cog is
never add_cog'd (so cog_load / cog listeners would not fire).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from modules import guild_snapshot as gs
from modules import moderation_extra as modx
from modules import test_harness as th

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
# Same wording the guard gives for an unknown command, so a refusal never confirms the feature exists.
REFUSAL = "That isn't something I'm able to run."
# Seconds between Discord calls during a restore (plan rule 10: never parallel bursts). Tests set this to 0.
RESTORE_PAUSE = 0.5


def handler_gate(interaction) -> bool:
    """Handler gate (layer 3). Call at the top of EVERY handler in this cog, and again before every destructive step."""
    return th.check_gates(
        guild_id=getattr(interaction, "guild_id", None),
        user_id=getattr(getattr(interaction, "user", None), "id", None),
        clone_id=getattr(getattr(interaction, "client", None), "clone_id", None),
    ) is None


async def _say(i, text: str, **kw):
    kw.setdefault("allowed_mentions", NO_PINGS)
    if i.response.is_done():
        await i.followup.send(text[:1990], ephemeral=True, **kw)
    else:
        await i.response.send_message(text[:1990], ephemeral=True, **kw)


def _slim(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "idmap"}


class TestHarnessCog(commands.Cog):
    __test__ = False  # not a pytest class

    # The guard calls the prompt hook before showing Confirm, then the handler after the click (and re-runs its own gate).
    AI_CONFIRM_PROMPTS = {"testharness_snapshot": "ai_snapshot_prompt"}
    AI_CONFIRMED_HANDLERS = {"testharness_snapshot": "ai_snapshot"}

    def __init__(self, bot):
        self.bot = bot
        self._locks = {}
        self._recovered = False

    # ── helpers ───────────────────────────────────────────────────────────
    @property
    def _clone_id(self):
        return getattr(self.bot, "clone_id", None)

    def _guild_ok(self, guild_id) -> bool:
        return th.check_guild(guild_id, self._clone_id) is None

    async def _audit(self, guild_id, action, user_id, reason):
        try:
            await modx.log_action(guild_id, action, user_id, reason=reason)
        except Exception:
            logger.exception("[test_harness] audit log failed")

    async def _post_report(self, guild, run, text, result):
        """Post the report in the test server: the channel the run started in (matched through the restore's id map if
        it had to be recreated), else the system channel. Failure to post never fails the restore."""
        try:
            cid = run.get("report_channel_id")
            live = (result.get("idmap") or {}).get(cid, cid)
            ch = guild.get_channel(live) if live else None
            ch = ch or getattr(guild, "system_channel", None)
            if ch is not None:
                await ch.send(text, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[test_harness] could not post the report")

    # ── restore (shared by stop, the dead-man timer and restart recovery) ──
    async def _restore_run(self, run: dict, *, why: str, interaction=None) -> Optional[dict]:
        gid = run["guild_id"]
        lock = self._locks.setdefault(gid, asyncio.Lock())
        if lock.locked():
            return None                                   # a restore is already running for this guild
        async with lock:
            if not self._guild_ok(gid):
                return None                               # handler gate for paths with no asker
            if not await db.th_claim_run(run["id"], list(th.UNFINISHED), "restoring"):
                return None                               # somebody else already finished it
            guild = self.bot.get_guild(gid)
            if guild is None:
                result = {"ok": False, "error": "the test server is not available to the bot", "done": 0, "planned": 0}
            else:
                try:
                    snap = json.loads(run["snapshot_json"])
                    if snap.get("hash") != run["snapshot_hash"]:
                        result = {"ok": False, "error": "snapshot does not match its stored hash", "done": 0, "planned": 0}
                    else:
                        result = await gs.restore(guild, snap, reason=f"test harness run #{run['id']}: {why}",
                                                  pause=RESTORE_PAUSE, still_allowed=lambda: self._guild_ok(gid))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[test_harness] restore crashed")
                    result = {"ok": False, "error": f"restore crashed: {type(exc).__name__}", "done": 0, "planned": 0}
            status = "restored" if result.get("ok") else "restore_failed"
            await db.th_finish_run(run["id"], status, json.dumps(_slim(result), default=str))
            text = th.format_report(result, why=why)
            await self._audit(gid, f"ai_testharness_{status}", run["owner_id"], f"run {run['id']}: {why}")
            if guild is not None:
                await self._post_report(guild, run, text, result)
            if interaction is not None:
                try:
                    await _say(interaction, text)
                except Exception:
                    logger.exception("[test_harness] could not reply")
            return result

    async def _tick(self):
        """One pass of the dead-man timer. The first pass after a restart restores EVERY unfinished run."""
        if not th.is_enabled() or not self._guild_ok(th.test_guild_id()):
            return
        recovering = not self._recovered
        self._recovered = True
        now = datetime.now(timezone.utc)
        for run in await db.th_unfinished_runs(th.test_guild_id()):
            stale_restore = run["status"] == "restoring" and not self._locks.get(run["guild_id"], asyncio.Lock()).locked()
            due = run["deadline_at"] <= now
            if recovering:
                await self._restore_run(run, why="restart recovery")
            elif run["status"] in ("armed", "running") and due:
                await self._restore_run(run, why="dead-man timer")
            elif stale_restore:
                await self._restore_run(run, why="resuming an interrupted restore")

    @tasks.loop(seconds=30)
    async def _deadman(self):
        try:
            await self._tick()
        except Exception:
            logger.exception("[test_harness] dead-man tick failed")

    @_deadman.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ── commands ──────────────────────────────────────────────────────────
    harness_g = app_commands.guild_only()(app_commands.Group(name="testharness", description="Owner-only bot test harness"))

    @harness_g.command(name="status", description="Report whether the test harness is armed in this server")
    async def harness_status(self, interaction: discord.Interaction):
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        run = await db.th_latest_run(interaction.guild_id)
        if run and run["status"] in th.UNFINISHED:
            left = max(0, int((run["deadline_at"] - datetime.now(timezone.utc)).total_seconds() // 60))
            extra = f" Run #{run['id']} is **{run['status']}**; the dead-man timer restores in ~{left} min."
        elif run:
            extra = f" Last run #{run['id']}: {run['status']}."
        else:
            extra = " No runs yet."
        await self._audit(interaction.guild.id, "ai_testharness_status", interaction.user.id, "status check")
        await _say(interaction, f"🧪 Test harness: **armed** in this server (phase 2: snapshot/restore, no scenarios yet).{extra}")

    async def ai_snapshot_prompt(self, interaction, minutes=None, **_):
        """Confirm text + early refusal. (ok, text)."""
        if not handler_gate(interaction):
            return False, REFUSAL
        if await db.th_unfinished_runs(interaction.guild_id):
            return False, "A test run is already armed here. Say `stop` to restore it first."
        snap = gs.capture(interaction.guild)
        m = th.clamp_minutes(minutes)
        return True, (f"🧪 I'll snapshot this server ({gs.summary(snap)}) and arm a **{m}-minute** restore timer. "
                      f"When it ends (or you say stop) **everything added after the snapshot is deleted and anything changed is put back** — "
                      f"don't add anything here you want to keep. Confirm?")

    async def ai_snapshot(self, interaction, minutes=None, **_):
        """Runs after the Confirm click (the guard re-ran its own gate just before this)."""
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        gid, m = interaction.guild_id, th.clamp_minutes(minutes)
        snap = gs.seal(gs.capture(interaction.guild))
        if not gs.verify(snap):
            return await _say(interaction, "❌ The snapshot failed its own integrity check, so nothing was started.")
        deadline = datetime.now(timezone.utc) + timedelta(minutes=m)
        report_ch = getattr(getattr(interaction, "channel", None), "id", None) or getattr(interaction, "channel_id", None)
        run_id = await db.th_create_run(gid, interaction.user.id, json.dumps(snap), snap["hash"], deadline, report_ch)
        if run_id is None:
            return await _say(interaction, "❌ A test run is already armed here. Say `stop` to restore it first.")
        stored = await db.th_get_run(run_id)                    # read it back: verified BEFORE anything may run
        try:
            back = json.loads(stored["snapshot_json"])
        except Exception:
            back = None
        if not (stored and stored["snapshot_hash"] == snap["hash"] and gs.verify(back)):
            await db.th_finish_run(run_id, "cancelled", json.dumps({"error": "snapshot read-back failed"}))
            return await _say(interaction, "❌ The saved snapshot did not read back intact, so the run was cancelled. Nothing changed.")
        await self._audit(gid, "ai_testharness_snapshot", interaction.user.id, f"run {run_id}, {m} min, {gs.summary(snap)}")
        await _say(interaction, f"✅ Run **#{run_id}** armed. Snapshot saved and verified ({gs.summary(snap)}). "
                                f"Restore timer: **{m} min**. Say `stop` to restore now.")

    @harness_g.command(name="snapshot", description="Save a snapshot of this test server and arm the restore timer")
    @app_commands.describe(minutes="Restore timer in minutes (default 15, max 60)")
    async def harness_snapshot(self, interaction: discord.Interaction, minutes: Optional[int] = None):
        # Only reached if something calls the callback directly (the AI goes through the confirm hooks above). Never
        # skip the Confirm click here either.
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        ok, text = await self.ai_snapshot_prompt(interaction, minutes=minutes)
        if not ok:
            return await _say(interaction, text)
        from discord_bot.cogs._views_moderation import ConfirmActionView

        async def go(ci):
            await self.ai_snapshot(ci, minutes=minutes)
        await _say(interaction, text, view=ConfirmActionView(interaction.user.id, go, confirm_label="Snapshot & arm", danger=True))

    @harness_g.command(name="stop", description="Kill switch: stop the run and restore this test server now")
    async def harness_stop(self, interaction: discord.Interaction):
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        runs = await db.th_unfinished_runs(interaction.guild_id)
        if not runs:
            return await _say(interaction, "No test run is armed here, so there is nothing to restore.")
        for run in runs:
            await self._restore_run(run, why=f"stopped by <@{interaction.user.id}>", interaction=interaction)


async def setup(bot: commands.Bot):
    """Deliberately NOT bot.add_cog (see module docstring). Clones never register it."""
    if getattr(bot, "clone_id", None) is not None:
        return
    from modules.ai_command_guard import register_hidden_cog
    cog = TestHarnessCog(bot)
    n = register_hidden_cog(cog)
    if not cog._deadman.is_running():
        cog._deadman.start()
    logger.info(f"[test_harness] registered {n} AI-only command(s) (not on the slash tree); dead-man timer running")
