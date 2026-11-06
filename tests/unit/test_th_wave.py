"""Bot test harness phase 4: the real channel-delete wave. Gated, needs an armed snapshot, capped, stops at the first
Discord refusal, always restores anti-nuke's settings and the server, and reports honestly."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import TestHarnessCog
from modules import ai_command_guard as guard
from modules import th_scenarios as scen
from modules.ai_command_allowlist import AI_COMMANDS_BY_NAME

TEST_GUILD, OTHER_GUILD, OWNER, STRANGER, BOT = 111, 222, 42, 99, 7777


def run(c):
    return asyncio.run(c)


def test_clamp_wave_default_and_cap():
    assert scen.clamp_wave(None) == 5 and scen.clamp_wave("x") == 5
    assert scen.clamp_wave(0) == 1 and scen.clamp_wave(10**6) == 25


def good(**kw):
    base = dict(created=5, deleted=5, audit_seen=5, own_trips=0, synth_trips=1, config_restored=True, aborted=None, seconds=1.0)
    base.update(kw)
    return scen.channel_wave_result(5, **base)


def test_result_passes_when_everything_checks_out():
    r = good()
    assert r["pass"] and not r["gaps"] and len(r["checks"]) == 6


@pytest.mark.parametrize("kw", [dict(created=4, deleted=4, audit_seen=4), dict(deleted=3), dict(audit_seen=2), dict(audit_seen=None),
                                dict(own_trips=1), dict(synth_trips=0), dict(synth_trips=2), dict(synth_trips=None),
                                dict(config_restored=False)])
def test_result_fails_on_any_bad_fact(kw):
    assert good(**kw)["pass"] is False


def test_aborted_run_fails_and_says_why():
    r = good(aborted="Discord refused creating channel 3: Forbidden")
    assert not r["pass"] and any("stopped early" in g for g in r["gaps"]) and "Stopped early" in r["notes"][0]


def test_wave_is_ai_only_test_guild_only_confirmed():
    spec = AI_COMMANDS_BY_NAME["testharness_wave"]
    assert spec.ai_only and spec.test_guild_only and spec.requires_confirmation


# ── cog ──────────────────────────────────────────────────────────────────
class FakeChannel:
    def __init__(self, name, fail_delete=False):
        self.name, self.fail_delete, self.deleted = name, fail_delete, False

    async def delete(self, reason=None):
        if self.fail_delete:
            raise discord.HTTPException(MagicMock(status=403, reason="x"), "no")
        self.deleted = True


def make_guild(fail_create_at=None, audit_n=None, fail_delete_at=None):
    made = []

    async def create(name, reason=None):
        if fail_create_at is not None and len(made) + 1 == fail_create_at:
            raise discord.HTTPException(MagicMock(status=403, reason="x"), "no")
        ch = FakeChannel(name, fail_delete=(fail_delete_at is not None and len(made) + 1 == fail_delete_at))
        made.append(ch)
        return ch

    async def audit_logs(limit=None, action=None):
        for _ in range(len(made) if audit_n is None else audit_n):
            yield SimpleNamespace(created_at=datetime.now(timezone.utc) + timedelta(seconds=1), user=SimpleNamespace(id=BOT))

    g = SimpleNamespace(id=TEST_GUILD, owner_id=OWNER, create_text_channel=create, audit_logs=audit_logs, made=made)
    return g


class Store:
    """In-memory stand-in for the anti-nuke config + run table."""
    def __init__(self, armed=True):
        self.cfg = {"enabled": False, "threshold": 3, "window_seconds": 10, "action": "contain", "log_channel_id": None, "triggered_count": 0}
        self.status = "armed" if armed else None
        self.history = []

    async def get(self, gid, cid=None):
        return dict(self.cfg)

    async def set(self, gid, cid=None, **f):
        self.cfg.update(f); self.history.append(dict(f)); return dict(self.cfg)

    def run(self):
        return {"id": 5, "guild_id": TEST_GUILD, "owner_id": OWNER, "status": self.status,
                "deadline_at": datetime.now(timezone.utc) + timedelta(minutes=10)}


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", TEST_GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    monkeypatch.setattr(cogmod, "WAVE_PAUSE", 0); monkeypatch.setattr(cogmod, "AUDIT_SETTLE", 0)
    st = Store()
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())
    monkeypatch.setattr(cogmod.db, "get_antinuke_config", st.get, raising=False)
    monkeypatch.setattr(cogmod.db, "set_antinuke_config", st.set, raising=False)
    monkeypatch.setattr(cogmod.db, "th_unfinished_runs", AsyncMock(side_effect=lambda gid=None: [st.run()] if st.status in ("armed", "running") else []), raising=False)
    monkeypatch.setattr(cogmod.db, "th_claim_run", AsyncMock(side_effect=lambda rid, frm, to: (st.status in frm) and (setattr(st, "status", to) or True)), raising=False)
    monkeypatch.setattr(cogmod.db, "th_get_run", AsyncMock(side_effect=lambda rid: st.run() if st.status in ("armed", "running") else None), raising=False)
    return st


def make_cog(st, synth_trips=1):
    bot = MagicMock(clone_id=None); bot.user = SimpleNamespace(id=BOT)
    listener = MagicMock()

    calls = {"n": 0}

    async def on_entry(entry):                       # like the real listener: one trip per 3 attacker entries
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            st.cfg["triggered_count"] += synth_trips
    listener.on_audit_log_entry_create = on_entry
    listener.invalidate = MagicMock()
    bot.get_cog = lambda n: listener if n == "AntiNukeCog" else None
    cog = TestHarnessCog(bot)
    cog._restore_run = AsyncMock()
    return cog


def inter(guild, guild_id=TEST_GUILD, user_id=OWNER, clone_id=None):
    i = SimpleNamespace(guild_id=guild_id, guild=guild, user=SimpleNamespace(id=user_id),
                        client=SimpleNamespace(clone_id=clone_id), channel=SimpleNamespace(id=9, send=AsyncMock()))
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


def test_prompt_refuses_without_an_armed_snapshot(env):
    env.status = None
    ok, text = run(make_cog(env).ai_wave_prompt(inter(make_guild()), count=5))
    assert not ok and "snapshot" in text.lower()


def test_prompt_states_size_and_snapshot(env):
    ok, text = run(make_cog(env).ai_wave_prompt(inter(make_guild()), count=500))
    assert ok and "**25**" in text and "#5" in text


def test_full_wave_passes_restores_config_and_the_server(env):
    cog, g = make_cog(env), make_guild()
    i = inter(g)
    run(cog.ai_wave(i, count=4))
    assert len(g.made) == 4 and all(c.deleted for c in g.made)
    assert env.cfg["enabled"] is False and env.cfg["log_channel_id"] is None          # settings put back
    cog._restore_run.assert_awaited_once()                                              # server restored afterwards
    sent = i.channel.send.await_args.args[0]
    assert "PASS" in sent and "Real scenario" in sent and "no changes made" not in sent


def test_stops_at_first_discord_refusal_and_still_cleans_up(env):
    cog, g = make_cog(env), make_guild(fail_create_at=3)
    i = inter(g)
    run(cog.ai_wave(i, count=10))
    assert len(g.made) == 2 and all(c.deleted for c in g.made)                          # never tried a 4th
    assert "FAIL" in i.channel.send.await_args.args[0] and "Stopped early" in i.channel.send.await_args.args[0]
    cog._restore_run.assert_awaited_once()
    assert env.cfg["enabled"] is False


def test_failed_delete_stops_and_reports(env):
    cog, g = make_cog(env), make_guild(fail_delete_at=2)
    i = inter(g)
    run(cog.ai_wave(i, count=3))
    assert "FAIL" in i.channel.send.await_args.args[0]
    cog._restore_run.assert_awaited_once()


def test_missing_audit_attribution_is_a_failure(env):
    cog, g = make_cog(env), make_guild(audit_n=0)
    i = inter(g)
    run(cog.ai_wave(i, count=2))
    assert "FAIL" in i.channel.send.await_args.args[0]


def test_listener_that_never_trips_is_a_failure(env):
    cog, g = make_cog(env, synth_trips=0), make_guild()
    i = inter(g)
    run(cog.ai_wave(i, count=2))
    assert "FAIL" in i.channel.send.await_args.args[0]


@pytest.mark.parametrize("kw", [dict(guild_id=OTHER_GUILD), dict(user_id=STRANGER), dict(clone_id=7)])
def test_refused_everywhere_else_and_creates_nothing(env, kw):
    cog, g = make_cog(env), make_guild()
    i = inter(g, **kw)
    run(cog.ai_wave(i, count=5))
    assert g.made == [] and env.history == [] and env.status == "armed"
    i.channel.send.assert_not_awaited()


def test_refused_when_id_unset(env, monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0, raising=False)
    cog, g = make_cog(env), make_guild()
    run(cog.ai_wave(inter(g), count=5))
    assert g.made == [] and env.history == []


def test_no_armed_run_means_nothing_is_created(env):
    env.status = None
    cog, g = make_cog(env), make_guild()
    run(cog.ai_wave(inter(g), count=5))
    assert g.made == [] and env.history == []


def test_direct_callback_still_needs_confirm(env):
    cog, g = make_cog(env), make_guild()
    i = inter(g)
    run(cog.harness_wave.callback(cog, i, count=5))
    assert g.made == []                                                                  # only a Confirm view was offered
