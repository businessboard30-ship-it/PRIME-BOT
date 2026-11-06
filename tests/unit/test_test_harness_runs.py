"""Test harness phase 2: runs, the confirm-gated snapshot, the stop kill switch, the dead-man timer and restart recovery,
against a fake guild and an in-memory stand-in for the test_harness_runs table."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import config
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import TestHarnessCog
from modules import ai_command_guard as guard
from modules import guild_snapshot as gs
from modules import test_harness as th
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME, needs_confirmation
from tests.unit._fake_guild import FakeGuild

OWNER, STRANGER = 42, 99


def run(coro):
    return asyncio.run(coro)


class FakeDb:
    """Same semantics as the SQL: one unfinished run per guild, atomic claim."""

    def __init__(self):
        self.runs, self.n, self.tamper = {}, 0, False

    async def th_create_run(self, guild_id, owner_id, snapshot_json, snapshot_hash, deadline_at, report_channel_id=None):
        if any(r["guild_id"] == guild_id and r["status"] in th.UNFINISHED for r in self.runs.values()):
            return None
        self.n += 1
        self.runs[self.n] = dict(id=self.n, guild_id=guild_id, owner_id=owner_id, status="armed", snapshot_json=snapshot_json,
                                 snapshot_hash=snapshot_hash, report_channel_id=report_channel_id, deadline_at=deadline_at,
                                 started_at=datetime.now(timezone.utc), finished_at=None, result_json=None)
        return self.n

    async def th_get_run(self, run_id):
        r = dict(self.runs[run_id]) if run_id in self.runs else None
        if r and self.tamper:
            snap = json.loads(r["snapshot_json"])
            snap["roles"][0]["name"] = "tampered"
            r["snapshot_json"] = json.dumps(snap)
        return r

    async def th_unfinished_runs(self, guild_id=None):
        return [dict(r) for r in self.runs.values() if r["status"] in th.UNFINISHED and (guild_id is None or r["guild_id"] == guild_id)]

    async def th_latest_run(self, guild_id):
        rs = [r for r in self.runs.values() if r["guild_id"] == guild_id]
        return dict(rs[-1]) if rs else None

    async def th_claim_run(self, run_id, from_statuses, to_status):
        r = self.runs.get(run_id)
        if r and r["status"] in from_statuses:
            r["status"] = to_status
            return True
        return False

    async def th_finish_run(self, run_id, status, result_json=None):
        self.runs[run_id].update(status=status, result_json=result_json, finished_at=datetime.now(timezone.utc))


class Inter:
    def __init__(self, guild, channel, user_id=OWNER, clone_id=None):
        self.guild, self.guild_id, self.channel, self.channel_id = guild, guild.id, channel, channel.id
        self.user = SimpleNamespace(id=user_id)
        self.client = SimpleNamespace(clone_id=clone_id)
        self.said, self._done = [], False
        self.response = SimpleNamespace(is_done=lambda: self._done, defer=self._defer, send_message=self._send)
        self.followup = SimpleNamespace(send=self._follow)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, text, **kw):
        self._done = True
        self.said.append((text, kw))

    async def _follow(self, text, **kw):
        self.said.append((text, kw))

    @property
    def text(self):
        return "\n".join(t for t, _ in self.said)


@pytest.fixture()
def env(monkeypatch):
    g = FakeGuild()
    monkeypatch.setattr(config, "TEST_GUILD_ID", g.id)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    fdb = FakeDb()
    monkeypatch.setattr(cogmod, "db", fdb)
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())
    monkeypatch.setattr(cogmod, "RESTORE_PAUSE", 0)                     # the pause between Discord calls is real in production only
    admin = g.add_role("Admin", permissions=8)
    cat = g.add_channel("CHAT", "category")
    gen = g.add_channel("general", "text", cat, topic="hello")
    g.add_channel("staff", "text", cat)
    gen.overwrites[admin] = discord.PermissionOverwrite.from_pair(discord.Permissions(1024), discord.Permissions(0))
    bot = SimpleNamespace(clone_id=None, get_guild=lambda gid: g if gid == g.id else None, wait_until_ready=AsyncMock())
    cog = TestHarnessCog(bot)
    return SimpleNamespace(g=g, db=fdb, cog=cog, bot=bot, gen=gen, cat=cat)


def arm(env, minutes=None, **kw):
    i = Inter(env.g, env.gen, **kw)
    run(env.cog.ai_snapshot(i, minutes=minutes))
    return i


def damage(env):
    for c in [c for c in env.g.channels if c.type.name != "category"]:
        run(c.delete())
    env.g.add_channel("raid1", "text")
    env.g.add_role("raider")


def chan(env, name):
    return next((c for c in env.g.channels if c.name == name), None)


# ── structural ───────────────────────────────────────────────────────────
def test_specs_and_confirm_hooks_line_up():
    assert needs_confirmation(AI_COMMANDS_BY_NAME["testharness_snapshot"], {}) is True
    assert needs_confirmation(AI_COMMANDS_BY_NAME["testharness_stop"], {}) is False        # kill switch is instant
    assert needs_confirmation(AI_COMMANDS_BY_NAME["testharness_status"], {}) is False
    for key in ("testharness_snapshot", "testharness_stop", "testharness_status"):
        assert AI_COMMANDS_BY_NAME[key].test_guild_only and AI_COMMANDS_BY_NAME[key].ai_only
    for key, hook in TestHarnessCog.AI_CONFIRMED_HANDLERS.items():
        assert callable(getattr(TestHarnessCog, hook))
    for key, hook in TestHarnessCog.AI_CONFIRM_PROMPTS.items():
        assert callable(getattr(TestHarnessCog, hook))


# ── snapshot ─────────────────────────────────────────────────────────────
def test_snapshot_prompt_warns_that_additions_will_be_deleted(env):
    ok, text = run(env.cog.ai_snapshot_prompt(Inter(env.g, env.gen), minutes=5))
    assert ok and "5-minute" in text and "deleted" in text and "Confirm" in text


def test_snapshot_arms_a_verified_run_with_a_deadline(env):
    i = arm(env)
    r = env.db.runs[1]
    assert r["status"] == "armed" and "armed" in i.text and r["report_channel_id"] == env.gen.id
    assert gs.verify(json.loads(r["snapshot_json"])) and r["snapshot_hash"] == json.loads(r["snapshot_json"])["hash"]
    mins = (r["deadline_at"] - datetime.now(timezone.utc)).total_seconds() / 60
    assert 14 < mins <= 15


@pytest.mark.parametrize("asked,expected", [(None, 15), (5, 5), (999, 60), (0, 15), (-3, 15), ("7", 7), ("x", 15)])
def test_deadman_minutes_are_clamped(env, asked, expected):
    arm(env, minutes=asked)
    mins = (env.db.runs[1]["deadline_at"] - datetime.now(timezone.utc)).total_seconds() / 60
    assert expected - 1 < mins <= expected


def test_only_one_unfinished_run_per_guild(env):
    arm(env)
    ok, text = run(env.cog.ai_snapshot_prompt(Inter(env.g, env.gen)))
    assert not ok and "already armed" in text
    i = arm(env)
    assert "already armed" in i.text and len(env.db.runs) == 1


def test_snapshot_that_does_not_read_back_intact_is_cancelled_and_never_arms(env):
    env.db.tamper = True
    i = arm(env)
    assert env.db.runs[1]["status"] == "cancelled" and "did not read back intact" in i.text
    assert run(env.db.th_unfinished_runs()) == []


def test_direct_slash_callback_still_needs_the_confirm_click(env):
    i = Inter(env.g, env.gen)
    run(TestHarnessCog.harness_snapshot.callback(env.cog, i, minutes=None))
    assert env.db.runs == {} and "view" in i.said[0][1]


# ── stop (kill switch) ───────────────────────────────────────────────────
def test_stop_restores_verifies_posts_a_report_and_finishes_the_run(env):
    arm(env)
    damage(env)
    i = Inter(env.g, env.gen)
    run(TestHarnessCog.harness_stop.callback(env.cog, i))
    assert env.db.runs[1]["status"] == "restored" and json.loads(env.db.runs[1]["result_json"])["ok"] is True
    assert chan(env, "raid1") is None and chan(env, "general").topic == "hello" and chan(env, "staff")
    assert not any(r.name == "raider" for r in env.g.roles)
    assert "Restore verified" in i.text and any("Restore verified" in m for m in chan(env, "general").sent)
    assert gs.plan_restore(json.loads(env.db.runs[1]["snapshot_json"]), gs.capture(env.g))["ops"] == []


def test_stop_with_nothing_armed_says_so(env):
    i = Inter(env.g, env.gen)
    run(TestHarnessCog.harness_stop.callback(env.cog, i))
    assert "nothing to restore" in i.text


def test_report_goes_to_the_recreated_channel_when_the_report_channel_was_destroyed(env):
    arm(env)
    run(env.gen.delete())
    run(env.cog._restore_run(run_of(env), why="test"))
    assert any("Restore" in m for m in chan(env, "general").sent)


def run_of(env, n=1):
    return dict(env.db.runs[n])


# ── dead-man timer and restart recovery ──────────────────────────────────
def test_timer_does_nothing_before_the_deadline(env):
    arm(env)
    run(env.cog._tick())                                                # first tick = recovery, so burn it...
    env.db.runs[1]["status"] = "armed"
    damage(env)
    run(env.cog._tick())
    assert env.db.runs[1]["status"] == "armed" and chan(env, "raid1")


def test_timer_restores_after_the_deadline(env):
    arm(env)
    env.cog._recovered = True
    damage(env)
    env.db.runs[1]["deadline_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    run(env.cog._tick())
    assert env.db.runs[1]["status"] == "restored" and chan(env, "raid1") is None
    assert any("dead-man timer" in m for m in chan(env, "general").sent)


def test_first_tick_after_a_restart_restores_even_before_the_deadline(env):
    arm(env)
    damage(env)
    fresh = TestHarnessCog(env.bot)                                     # "the bot restarted": a brand new cog instance
    run(fresh._tick())
    assert env.db.runs[1]["status"] == "restored" and chan(env, "raid1") is None
    assert any("restart recovery" in m for m in chan(env, "general").sent)


def test_an_interrupted_restore_is_resumed(env):
    arm(env)
    env.cog._recovered = True
    damage(env)
    env.db.runs[1]["status"] = "restoring"                              # the bot died halfway through
    run(env.cog._tick())
    assert env.db.runs[1]["status"] == "restored" and chan(env, "raid1") is None


def test_only_one_restore_runs_when_two_triggers_race(env):
    arm(env)
    damage(env)
    results = run(_both(env))
    assert sum(r is not None for r in results) == 1
    assert env.db.runs[1]["status"] == "restored"


async def _both(env):
    r = run_of(env)
    return await asyncio.gather(env.cog._restore_run(r, why="a"), env.cog._restore_run(dict(r), why="b"))


# ── gates for paths that have no asker ───────────────────────────────────
def test_disabled_feature_never_restores(env, monkeypatch):
    arm(env)
    damage(env)
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0)
    run(env.cog._tick())
    assert env.db.runs[1]["status"] == "armed" and chan(env, "raid1")
    assert run(env.cog._restore_run(run_of(env), why="x")) is None


def test_other_guild_run_is_never_touched(env):
    other = FakeGuild(id=777)
    r = SimpleNamespace(**{})
    env.db.runs[5] = dict(id=5, guild_id=777, owner_id=OWNER, status="armed", snapshot_json="{}", snapshot_hash="x",
                          report_channel_id=None, deadline_at=datetime.now(timezone.utc) - timedelta(hours=1))
    assert run(env.cog._restore_run(dict(env.db.runs[5]), why="x")) is None
    assert env.db.runs[5]["status"] == "armed"


def test_clone_never_restores(env):
    arm(env)
    damage(env)
    env.bot.clone_id = 3
    run(env.cog._tick())
    assert env.db.runs[1]["status"] == "armed" and chan(env, "raid1")


def test_gate_closing_mid_restore_stops_it_and_marks_it_failed(env, monkeypatch):
    arm(env)
    damage(env)
    calls = {"n": 0}
    real = env.cog._guild_ok

    def flaky(gid):
        calls["n"] += 1
        return calls["n"] <= 3
    monkeypatch.setattr(env.cog, "_guild_ok", flaky)
    res = run(env.cog._restore_run(run_of(env), why="x"))
    assert res["aborted"] and env.db.runs[1]["status"] == "restore_failed"


# ── failure paths ────────────────────────────────────────────────────────
def test_a_stored_snapshot_that_does_not_match_its_hash_is_refused_and_the_server_untouched(env):
    arm(env)
    damage(env)
    env.db.runs[1]["snapshot_hash"] = "0" * 64
    res = run(env.cog._restore_run(run_of(env), why="x"))
    assert not res["ok"] and "hash" in res["error"] and env.db.runs[1]["status"] == "restore_failed"
    assert chan(env, "raid1") and chan(env, "general") is None


def test_incomplete_restore_is_reported_honestly(env):
    arm(env)
    damage(env)
    env.g.fail["create_channel"] = -1
    i = Inter(env.g, env.gen)
    res = run(env.cog._restore_run(run_of(env), why="x", interaction=i))
    assert not res["ok"] and env.db.runs[1]["status"] == "restore_failed"
    assert "Restore incomplete" in i.text and "Not restorable by design" in i.text


# ── handler gate on every handler ────────────────────────────────────────
@pytest.mark.parametrize("kw", [dict(user_id=STRANGER), dict(clone_id=3)])
def test_every_handler_refuses_non_owner_and_clone(env, kw):
    i = Inter(env.g, env.gen, **kw)
    ok, text = run(env.cog.ai_snapshot_prompt(i))
    assert (ok, text) == (False, cogmod.REFUSAL)
    run(env.cog.ai_snapshot(i))
    run(TestHarnessCog.harness_stop.callback(env.cog, i))
    run(TestHarnessCog.harness_status.callback(env.cog, i))
    assert env.db.runs == {} and all(t == cogmod.REFUSAL for t, _ in i.said)


def test_every_handler_refuses_in_another_guild(env):
    other = FakeGuild(id=777)
    ch = other.add_channel("x", "text")
    i = Inter(other, ch)
    assert run(env.cog.ai_snapshot_prompt(i)) == (False, cogmod.REFUSAL)
    run(env.cog.ai_snapshot(i))
    assert env.db.runs == {} and i.said[0][0] == cogmod.REFUSAL


# ── through the real guard ───────────────────────────────────────────────
def test_through_the_guard_snapshot_needs_confirm_and_stop_does_not(env, monkeypatch):
    monkeypatch.setattr(guard, "_AI_ONLY_COMMANDS", {})
    guard.register_hidden_cog(env.cog)
    monkeypatch.setattr(guard.db, "log_ai_command", AsyncMock())
    monkeypatch.setattr(guard, "check_ai_command_cap", AsyncMock(return_value=(True, None)))
    i = Inter(env.g, env.gen)
    i.permissions = discord.Permissions.all()
    i.client = SimpleNamespace(clone_id=None, tree=SimpleNamespace(get_command=lambda n: None, walk_commands=lambda: []))
    for name in ("testharness_snapshot", "testharness_stop", "testharness_status"):
        spec, cmd, reason, cap = run(guard.resolve_and_check(i, name, {}))
        assert reason is None and cmd.binding is env.cog
    run(guard.execute_ai_command(i, "testharness_snapshot"))            # what the Confirm click calls
    assert env.db.runs[1]["status"] == "armed"
    damage(env)
    run(guard.execute_ai_command(i, "testharness_stop", invoke_directly=True))
    assert env.db.runs[1]["status"] == "restored"
