"""Bot test harness phase 3: simulated scenarios are pure, capped, honest about failures, and gated."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs.test_harness import TestHarnessCog
from modules import th_scenarios as scen

TEST_GUILD, OTHER_GUILD, OWNER, STRANGER = 111, 222, 42, 99


def run(c):
    return asyncio.run(c)


def inter(guild_id=TEST_GUILD, user_id=OWNER, clone_id=None):
    i = SimpleNamespace(guild_id=guild_id, guild=SimpleNamespace(id=guild_id), user=SimpleNamespace(id=user_id),
                        client=SimpleNamespace(clone_id=clone_id), channel=SimpleNamespace(send=AsyncMock()))
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


@pytest.fixture()
def armed(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", TEST_GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})
    monkeypatch.setattr(cogmod.modx, "log_action", AsyncMock())


def fake_check(hits):
    """A stand-in for scam_shield.check_text that matches only the given substrings."""
    return lambda text, allowed=(), strict=False: ("word", "x", None) if any(h in text.lower() for h in hits) else None


def test_join_burst_passes_with_default_filters():
    r = scen.join_burst(10)
    assert r["pass"] and not r["gaps"] and len(r["checks"]) == 4


def test_join_burst_is_capped():
    assert "(15 accounts)" in scen.join_burst(500)["title"]
    assert "(1 accounts)" in scen.join_burst(-3)["title"]


def test_join_burst_reports_a_gap_when_protections_are_off():
    r = scen.join_burst(5, gate_cfg={"enabled": False}, raid_cfg={})
    assert not r["pass"] and len(r["gaps"]) == 2          # gate and profile filter both missed; controls still fine


def test_scam_links_pass_when_everything_is_caught_and_clean_is_left_alone():
    every = [t.lower()[:12] for _, t in scen.SCAM_SAMPLES]
    r = scen.scam_links(check=fake_check(every))
    assert r["pass"] and len(r["checks"]) == len(scen.SCAM_SAMPLES) + len(scen.CLEAN_SAMPLES)


def test_scam_links_report_missed_scam_and_false_positive():
    assert not scen.scam_links(check=fake_check([]))["pass"]                       # catches nothing
    assert not scen.scam_links(check=fake_check(["hey everyone"]))["pass"]         # flags normal chat


def test_scam_links_with_the_real_rules_catch_the_builtin_heuristics():
    r = scen.scam_links()                                                           # empty rule cache: heuristics + strict only
    assert r["pass"], r["gaps"]


def test_unknown_scenario_fails_closed():
    assert scen.run("nope")["pass"] is False


def test_format_results_lists_gaps():
    text = scen.format_results([scen.join_burst(3, gate_cfg={"enabled": False})])
    assert "FAIL" in text and "Gaps:" in text and len(text) <= 1990


def test_simulate_runs_for_owner_in_test_guild_and_posts_report(armed):
    cog, i = TestHarnessCog(MagicMock(clone_id=None)), inter()
    run(cog.harness_simulate.callback(cog, i))
    assert i.followup.send.await_count + i.response.send_message.await_count >= 1
    i.channel.send.assert_awaited()
    cogmod.modx.log_action.assert_awaited()


@pytest.mark.parametrize("kw", [dict(guild_id=OTHER_GUILD), dict(user_id=STRANGER), dict(clone_id=7)])
def test_simulate_refused_everywhere_else(armed, kw):
    cog, i = TestHarnessCog(MagicMock(clone_id=None)), inter(**kw)
    run(cog.harness_simulate.callback(cog, i))
    i.channel.send.assert_not_awaited()
    cogmod.modx.log_action.assert_not_awaited()


def test_simulate_refused_when_id_unset(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0, raising=False)
    cog, i = TestHarnessCog(MagicMock(clone_id=None)), inter()
    run(cog.harness_simulate.callback(cog, i))
    i.channel.send.assert_not_awaited()


def test_simulate_rejects_unknown_scenario(armed):
    cog, i = TestHarnessCog(MagicMock(clone_id=None)), inter()
    run(cog.harness_simulate.callback(cog, i, scenario="nuke"))
    i.channel.send.assert_not_awaited()
