# path: discord_bot/cogs/test_harness.py

"""
AI-ONLY bot test harness -- phases 1-4 (docs/BOT_TEST_HARNESS_PLAN.md).

Like discord_bot/cogs/ai_server_ops.py this cog is NEVER added to the slash tree: setup() hands the commands to the
AI-only registry, so nothing shows in Discord's command list and no command slot is used. Do NOT add it to the tree,
do NOT mention it in the README or help text.

Commands (all test-guild-only, owner-only, main-bot-only):
  testharness simulate  phase 3: simulated scenarios, no server changes (no confirm, no snapshot)
  testharness rolewave  phase 4b, REAL: same as wave but with throw-away roles (cap 20).
  testharness wave      phase 4, REAL: create then delete <=25 throw-away channels, check audit log + anti-nuke, restore.
                        Needs an armed snapshot and a CONFIRM CLICK.
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
from types import SimpleNamespace
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import db
from modules import guild_snapshot as gs
from modules import moderation_extra as modx
from modules import test_harness as th
from modules import th_scenarios as scen

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
# Same wording the guard gives for an unknown command, so a refusal never confirms the feature exists.
REFUSAL = "That isn't something I'm able to run."
# Seconds between Discord calls during a restore (plan rule 10: never parallel bursts). Tests set this to 0.
RESTORE_PAUSE = 0.5
# Seconds between Discord calls during a real wave (plan rule 10). Tests set this to 0.
WAVE_PAUSE = 0.7
AUDIT_SETTLE = 3.0   # seconds to let Discord write the audit entries before reading them back


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


class HarnessPanel(discord.ui.LayoutView):
    """Button-only panel (opened by one word, see cogs/word_router.py). Every press goes through the same gated, confirmed
    handlers as the AI path; only the person who opened it can press."""

    def __init__(self, cog, owner_id: int):
        super().__init__(timeout=300)
        self.cog, self.owner_id = cog, owner_id
        self.message = None
        box = discord.ui.Container(accent_colour=discord.Color.red())
        rows = (
            (("Run all", "🧪", discord.ButtonStyle.danger, cog.harness_run_all),),
            (("Snapshot", "📸", discord.ButtonStyle.secondary, cog.harness_snapshot.callback),
             ("Channel wave", "💥", discord.ButtonStyle.secondary, cog.harness_wave.callback),
             ("Role wave", "🎭", discord.ButtonStyle.secondary, cog.harness_rolewave.callback)),
            (("Simulate", "🧫", discord.ButtonStyle.secondary, cog.harness_simulate.callback),
             ("Status", "📋", discord.ButtonStyle.secondary, cog.harness_status.callback),
             ("Stop", "🛑", discord.ButtonStyle.danger, cog.harness_stop.callback)),
        )
        for row_spec in rows:
            row = discord.ui.ActionRow()
            for label, emoji, style, fn in row_spec:
                b = discord.ui.Button(label=label, emoji=emoji, style=style)
                b.callback = self._press(fn)
                row.add_item(b)
            box.add_item(row)
        self.add_item(box)

    def _press(self, fn):
        async def cb(i: discord.Interaction):
            if not handler_gate(i):
                return await _say(i, REFUSAL)
            try:
                # bound method (run all) or an unbound command callback that wants the cog first
                await (fn(i) if getattr(fn, "__self__", None) is not None else fn(self.cog, i))
            except Exception:
                logger.exception("[test_harness] panel button failed")
                await _say(i, "Something went wrong.")
        return cb

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.owner_id:
            await _say(i, "Not yours.")
            return False
        return True

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass



class TestHarnessCog(commands.Cog):
    __test__ = False  # not a pytest class

    # The guard calls the prompt hook before showing Confirm, then the handler after the click (and re-runs its own gate).
    AI_CONFIRM_PROMPTS = {"testharness_snapshot": "ai_snapshot_prompt", "testharness_wave": "ai_wave_prompt",
                         "testharness_rolewave": "ai_rolewave_prompt"}
    AI_CONFIRMED_HANDLERS = {"testharness_snapshot": "ai_snapshot", "testharness_wave": "ai_wave",
                          "testharness_rolewave": "ai_rolewave"}

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
        for run in await db.th_unfinished_runs(th.test_guild_id(), self._clone_id):      # only THIS bot's own runs
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
        run = await db.th_latest_run(interaction.guild_id, self._clone_id)
        if run and run["status"] in th.UNFINISHED:
            left = max(0, int((run["deadline_at"] - datetime.now(timezone.utc)).total_seconds() // 60))
            extra = f" Run #{run['id']} is **{run['status']}**; the dead-man timer restores in ~{left} min."
        elif run:
            extra = f" Last run #{run['id']}: {run['status']}."
        else:
            extra = " No runs yet."
        await self._audit(interaction.guild.id, "ai_testharness_status", interaction.user.id, "status check")
        await _say(interaction, f"🧪 Test harness: **armed** in this server (phase 3: snapshot/restore + simulated scenarios).{extra}")

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
        run_id = await db.th_create_run(gid, interaction.user.id, json.dumps(snap), snap["hash"], deadline, report_ch, self._clone_id)
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

    @harness_g.command(name="simulate", description="Run simulated protection scenarios (no changes to the server)")
    @app_commands.describe(scenario="join_burst or scam_links (default: both)", count="Join-burst size (max 15)")
    async def harness_simulate(self, interaction: discord.Interaction, scenario: Optional[str] = None,
                               count: Optional[int] = None):
        """Phase 3, category A. Pure decision-function checks: no Discord writes, no DB writes, so no confirm and no snapshot."""
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        name = (scenario or "").strip().lower() or None
        if name and name not in scen.SCENARIOS:
            return await _say(interaction, f"Unknown scenario. Choose from: {', '.join(scen.SCENARIOS)}.")
        try:
            from modules import scam_shield
            await scam_shield.load()                    # make sure the live rules are in memory before testing them
        except Exception:
            logger.exception("[test_harness] could not load Scam Shield rules")
        n = max(1, min(int(count or 10), scen.MAX_JOINERS))
        results = [scen.run(name, **({"n": n} if name == "join_burst" else {}))] if name else scen.run_all(n=n)
        if not handler_gate(interaction):               # re-check before reporting (plan rule 2, layer 3)
            return await _say(interaction, REFUSAL)
        summary = ", ".join(f"{r['name']}={'pass' if r['pass'] else 'FAIL'}" for r in results)
        await self._audit(interaction.guild_id, "ai_testharness_simulate", interaction.user.id, summary)
        text = scen.format_results(results)
        await _say(interaction, text)
        try:
            if interaction.channel is not None:
                await interaction.channel.send(text, allowed_mentions=NO_PINGS)     # report posted in the test server
        except Exception:
            logger.exception("[test_harness] could not post the simulation report")

    # ── phase 4: real channel-delete wave ─────────────────────────────────
    async def _armed_run(self, guild_id):
        runs = [r for r in await db.th_unfinished_runs(guild_id, self._clone_id) if r["status"] == "armed"]
        return runs[0] if runs else None

    async def _wave_prompt(self, interaction, kind, count):
        """Confirm text + early refusal. (ok, text). Refuses unless a verified snapshot is armed (plan rule 7)."""
        if not handler_gate(interaction):
            return False, REFUSAL
        run = await self._armed_run(interaction.guild_id)
        if run is None:
            return False, "No armed snapshot here. Run `testharness snapshot` first; nothing destructive starts without one."
        n, noun = scen.clamp_wave(count, kind), scen.WAVES[kind][1]
        left = max(0, int((run["deadline_at"] - datetime.now(timezone.utc)).total_seconds() // 60))
        return True, (f"🧪 REAL wave in this test server: I'll create **{n}** throw-away {noun}, delete them one by one "
                      f"(~{WAVE_PAUSE}s apart), check the audit log and anti-nuke, then restore the server from snapshot run "
                      f"#{run['id']} (its timer has ~{left} min left; the run ends when I finish). Stops at the first Discord error. Confirm?")

    async def ai_wave_prompt(self, interaction, count=None, **_):
        return await self._wave_prompt(interaction, "channel", count)

    async def ai_rolewave_prompt(self, interaction, count=None, **_):
        return await self._wave_prompt(interaction, "role", count)

    async def ai_wave(self, interaction, count=None, **_):
        return await self._wave(interaction, "channel", count)

    async def ai_rolewave(self, interaction, count=None, **_):
        return await self._wave(interaction, "role", count)

    async def _wave(self, interaction, kind, count):
        """Runs after the Confirm click. Handler gate re-checked before EVERY Discord call."""
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        gid, n, guild = interaction.guild_id, scen.clamp_wave(count, kind), interaction.guild
        noun = scen.WAVES[kind][1]
        audit_action = discord.AuditLogAction.channel_delete if kind == "channel" else discord.AuditLogAction.role_delete
        run = await self._armed_run(gid)
        if run is None or not await db.th_claim_run(run["id"], ["armed"], "running"):
            return await _say(interaction, "No armed snapshot (or a run is already going), so nothing was started.")
        import time as _time
        t0, created, deleted, aborted = _time.perf_counter(), [], 0, None
        bot_id = getattr(self.bot.user, "id", None)
        prev = await db.get_antinuke_config(gid, self._clone_id)
        listener = self.bot.get_cog("AntiNukeCog")
        synth, own_trips, audit_seen, restored_cfg = None, 0, None, False
        started = datetime.now(timezone.utc)
        await self._audit(gid, "ai_testharness_wave_start", interaction.user.id, f"run {run['id']}, {n} {noun}")
        try:
            # Anti-nuke on for the test, pointed at this channel, with the default threshold.
            report_ch = getattr(interaction.channel, "id", None)
            await db.set_antinuke_config(gid, self._clone_id, enabled=True, threshold=3, window_seconds=10, action="contain",
                                         log_channel_id=report_ch)
            if listener:
                listener.invalidate(gid)
            trips_before = (await db.get_antinuke_config(gid, self._clone_id)).get("triggered_count", 0)
            for i in range(n):
                if not handler_gate(interaction):
                    aborted = "the gates no longer allow this server"
                    break
                try:
                    nm, why = f"th-wave-{run['id']}-{i + 1}", f"test harness run #{run['id']}"
                    ch = (await guild.create_text_channel(nm, reason=why) if kind == "channel"
                          else await guild.create_role(name=nm, permissions=discord.Permissions.none(), reason=why))
                except discord.HTTPException as exc:
                    aborted = f"Discord refused creating {kind} {i + 1}: {type(exc).__name__}"
                    break
                created.append(ch)
                await asyncio.sleep(WAVE_PAUSE)
            for ch in list(created):
                if not handler_gate(interaction):
                    aborted = aborted or "the gates no longer allow this server"
                    break
                try:
                    await ch.delete(reason=f"test harness run #{run['id']}")
                    deleted += 1
                except discord.HTTPException as exc:
                    aborted = aborted or f"Discord refused deleting {ch.name}: {type(exc).__name__}"
                    break
                await asyncio.sleep(WAVE_PAUSE)
            await asyncio.sleep(AUDIT_SETTLE)
            # Did Discord attribute the deletes to the bot? (anti-nuke's whole input)
            try:
                if handler_gate(interaction):
                    seen = 0
                    async for e in guild.audit_logs(limit=min(50, n * 2 + 10), action=audit_action):
                        if e.created_at >= started and getattr(e.user, "id", None) == bot_id:
                            seen += 1
                    audit_seen = seen
            except discord.HTTPException:
                audit_seen = None
            after_own = (await db.get_antinuke_config(gid, self._clone_id)).get("triggered_count", 0)
            own_trips = max(0, after_own - trips_before)
            # Synthetic attacker through the REAL listener: must trip exactly once and alert.
            if listener is not None and handler_gate(interaction):
                from modules import antinuke as an
                an.reset(gid)
                for _ in range(3):
                    await listener.on_audit_log_entry_create(SimpleNamespace(
                        action=audit_action, guild=guild,
                        user=SimpleNamespace(id=scen.SYNTH_ATTACKER_ID)))
                an.reset(gid)
                synth = (await db.get_antinuke_config(gid, self._clone_id)).get("triggered_count", 0) - after_own
        except Exception as exc:  # noqa: BLE001
            logger.exception("[test_harness] wave crashed")
            aborted = aborted or f"crashed: {type(exc).__name__}"
        finally:
            try:
                await db.set_antinuke_config(gid, self._clone_id, enabled=prev["enabled"], threshold=prev["threshold"],
                                             window_seconds=prev["window_seconds"], action=prev["action"],
                                             log_channel_id=prev["log_channel_id"])
                if listener:
                    listener.invalidate(gid)
                restored_cfg = True
            except Exception:
                logger.exception("[test_harness] could not restore the anti-nuke settings")
        result = scen.wave_result(kind, n, created=len(created), deleted=deleted, audit_seen=audit_seen, own_trips=own_trips,
                                          synth_trips=synth, config_restored=restored_cfg, aborted=aborted,
                                          seconds=_time.perf_counter() - t0)
        text = scen.format_results([result])
        await self._audit(gid, f"ai_testharness_{kind}wave", interaction.user.id,
                          f"run {run['id']}: {'pass' if result['pass'] else 'FAIL'}; {'; '.join(result['gaps']) or 'no gaps'}"[:500])
        await _say(interaction, text)
        try:
            if interaction.channel is not None:
                await interaction.channel.send(text, allowed_mentions=NO_PINGS)
        except Exception:
            logger.exception("[test_harness] could not post the wave report")
        fresh = await db.th_get_run(run["id"])
        if fresh and fresh["status"] in th.UNFINISHED:               # always restore afterwards (plan rule 7)
            await self._restore_run(fresh, why="wave finished", interaction=interaction)

    @harness_g.command(name="wave", description="REAL: create+delete a small wave of throw-away channels, check protections, restore")
    @app_commands.describe(count="Channels in the wave (default 5, max 25)")
    async def harness_wave(self, interaction: discord.Interaction, count: Optional[int] = None):
        # Only reached if something calls the callback directly (the AI goes through the confirm hooks). Never skip Confirm.
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        ok, text = await self.ai_wave_prompt(interaction, count=count)
        if not ok:
            return await _say(interaction, text)
        from discord_bot.cogs._views_moderation import ConfirmActionView

        async def go(ci):
            await self.ai_wave(ci, count=count)
        await _say(interaction, text, view=ConfirmActionView(interaction.user.id, go, confirm_label="Run wave", danger=True))

    @harness_g.command(name="rolewave", description="REAL: create+delete a small wave of throw-away roles, check protections, restore")
    @app_commands.describe(count="Roles in the wave (default 5, max 20)")
    async def harness_rolewave(self, interaction: discord.Interaction, count: Optional[int] = None):
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        ok, text = await self.ai_rolewave_prompt(interaction, count=count)
        if not ok:
            return await _say(interaction, text)
        from discord_bot.cogs._views_moderation import ConfirmActionView

        async def go(ci):
            await self.ai_rolewave(ci, count=count)
        await _say(interaction, text, view=ConfirmActionView(interaction.user.id, go, confirm_label="Run wave", danger=True))

    async def harness_run_all(self, interaction: discord.Interaction):
        """One Confirm, then: snapshot -> channel wave (restores) -> snapshot -> role wave (restores). Stops at the first
        step that does not leave an armed snapshot behind, so nothing destructive ever runs without one."""
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        ok, text = await self.ai_snapshot_prompt(interaction)
        if not ok:
            return await _say(interaction, text)
        from discord_bot.cogs._views_moderation import ConfirmActionView
        n_c, n_r = scen.WAVE_DEFAULT, scen.WAVE_DEFAULT
        plan = (f"🧪 Full test in this server: snapshot, create+delete **{n_c}** throw-away channels, restore, then snapshot, "
                f"create+delete **{n_r}** throw-away roles, restore. Reports are posted here. Confirm?")

        async def go(ci):
            for kind, n in (("channel", n_c), ("role", n_r)):
                if not handler_gate(ci):
                    return await _say(ci, REFUSAL)
                await self.ai_snapshot(ci)
                if await self._armed_run(ci.guild_id) is None:
                    return await _say(ci, "No armed snapshot, so I stopped before anything destructive.")
                await self._wave(ci, kind, n)
        await _say(interaction, plan, view=ConfirmActionView(interaction.user.id, go, confirm_label="Run all", danger=True))

    @harness_g.command(name="stop", description="Kill switch: stop the run and restore this test server now")
    async def harness_stop(self, interaction: discord.Interaction):
        if not handler_gate(interaction):
            return await _say(interaction, REFUSAL)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        runs = await db.th_unfinished_runs(interaction.guild_id, self._clone_id)
        if not runs:
            if await db.th_unfinished_runs(interaction.guild_id):          # another harness bot owns it: never touch it from here
                return await _say(interaction, "The armed run belongs to the other test bot. Press Stop there.")
            return await _say(interaction, "No test run is armed here, so there is nothing to restore.")
        for run in runs:
            await self._restore_run(run, why=f"stopped by <@{interaction.user.id}>", interaction=interaction)


async def setup(bot: commands.Bot):
    """Deliberately NOT bot.add_cog (see module docstring). Only the one harness bot registers it (the main bot, or the single
    clone named by config.TEST_CLONE_ID); every other bot returns here, so it has no harness command, timer or panel."""
    if not th.bot_may_run(getattr(bot, "clone_id", None)):
        return
    from modules.ai_command_guard import register_hidden_cog
    cog = TestHarnessCog(bot)
    bot.test_harness_cog = cog                  # the one-word router opens the harness panel through this
    n = register_hidden_cog(cog)
    if not cog._deadman.is_running():
        cog._deadman.start()
    logger.info(f"[test_harness] registered {n} AI-only command(s) (not on the slash tree); dead-man timer running")
