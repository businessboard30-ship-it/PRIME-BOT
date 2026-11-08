"""One word ('test', 'nuke', ...) opens a button-only harness panel for the owner in the test server only; every press is gated;
Run all needs one Confirm and never starts anything destructive without an armed snapshot."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from discord_bot.cogs import test_harness as cogmod
from discord_bot.cogs import word_router as wrcog
from discord_bot.cogs.test_harness import HarnessPanel, TestHarnessCog
from modules import word_router as wr

TEST_GUILD, OTHER_GUILD, OWNER, STRANGER, BOT = 111, 222, 42, 99, 777



@pytest.fixture(autouse=True)
def _main_bot_is_the_harness_bot(monkeypatch):
    """These tests model the main bot as the harness bot; the clone rule has its own tests (test_th_clones.py)."""
    import config as _config
    monkeypatch.setattr(_config, "TEST_CLONE_IDS", set(), raising=False)

def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", TEST_GUILD, raising=False)
    monkeypatch.setattr(config, "DISCORD_CLONE_ADMIN_IDS", {OWNER})


def buttons(view):
    return [b for b in view.walk_children() if isinstance(b, discord.ui.Button)]


def inter(guild_id=TEST_GUILD, user_id=OWNER, clone_id=None):
    i = SimpleNamespace(guild_id=guild_id, guild=SimpleNamespace(id=guild_id), user=SimpleNamespace(id=user_id),
                        client=SimpleNamespace(clone_id=clone_id), channel=SimpleNamespace(id=9, send=AsyncMock()))
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), defer=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


def make_cog():
    bot = MagicMock(clone_id=None)
    return TestHarnessCog(bot)


def test_panel_has_exactly_the_expected_buttons_and_no_text():
    v = HarnessPanel(make_cog(), OWNER)
    assert [b.label for b in buttons(v)] == ["Run all", "Snapshot", "Channel wave", "Role wave", "Simulate", "Status", "Stop"]
    assert not [t for t in v.walk_children() if isinstance(t, discord.ui.TextDisplay)]


def test_only_the_opener_can_press():
    v = HarnessPanel(make_cog(), OWNER)
    i = inter(user_id=STRANGER)
    assert run(v.interaction_check(i)) is False
    assert run(v.interaction_check(inter())) is True


def test_every_button_is_refused_outside_the_gates():
    cog = make_cog()
    for kw in (dict(guild_id=OTHER_GUILD), dict(user_id=STRANGER), dict(clone_id=7)):
        v = HarnessPanel(cog, OWNER)
        for b in buttons(v):
            i = inter(**kw)
            cog._wave = AsyncMock(); cog.ai_snapshot = AsyncMock()
            run(b.callback(i))
            cog._wave.assert_not_awaited(); cog.ai_snapshot.assert_not_awaited()
            assert i.response.send_message.await_count == 1


def test_wave_buttons_only_offer_a_confirm_they_do_not_run_the_wave(monkeypatch):
    cog = make_cog()
    cog._armed_run = AsyncMock(return_value={"id": 5, "deadline_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)})
    cog._wave = AsyncMock()
    v = HarnessPanel(cog, OWNER)
    by = {b.label: b for b in buttons(v)}
    for label in ("Channel wave", "Role wave", "Snapshot"):
        run(by[label].callback(inter()))
    cog._wave.assert_not_awaited()


def test_run_all_asks_for_one_confirm_first():
    cog = make_cog()
    cog.ai_snapshot_prompt = AsyncMock(return_value=(True, "x"))
    cog.ai_snapshot = AsyncMock(); cog._wave = AsyncMock(); cog._armed_run = AsyncMock(return_value={"id": 1})
    i = inter()
    run(cog.harness_run_all(i))
    cog.ai_snapshot.assert_not_awaited(); cog._wave.assert_not_awaited()
    assert "view" in i.response.send_message.await_args.kwargs


def confirm_and_go(cog, i):
    """Press Confirm on the view that run-all offered."""
    run(cog.harness_run_all(i))
    view = i.response.send_message.await_args.kwargs["view"]
    ci = inter()
    confirm = [c for c in view.children if getattr(c, "label", "") == "Run all"][0]
    run(confirm.callback(ci))
    return ci


def test_run_all_runs_snapshot_channel_snapshot_role_in_order():
    cog = make_cog()
    calls = []
    cog.ai_snapshot_prompt = AsyncMock(return_value=(True, "x"))
    cog.ai_snapshot = AsyncMock(side_effect=lambda *a, **k: calls.append("snapshot"))
    cog._armed_run = AsyncMock(return_value={"id": 1})
    cog._wave = AsyncMock(side_effect=lambda ci, kind, n: calls.append(kind))
    confirm_and_go(cog, inter())
    assert calls == ["snapshot", "channel", "snapshot", "role"]


def test_run_all_stops_if_no_snapshot_is_armed():
    cog = make_cog()
    cog.ai_snapshot_prompt = AsyncMock(return_value=(True, "x"))
    cog.ai_snapshot = AsyncMock()
    cog._armed_run = AsyncMock(return_value=None)
    cog._wave = AsyncMock()
    confirm_and_go(cog, inter())
    cog._wave.assert_not_awaited()


def test_run_all_refused_when_a_run_is_already_armed():
    cog = make_cog()
    cog.ai_snapshot_prompt = AsyncMock(return_value=(False, "already armed"))
    cog._wave = AsyncMock()
    i = inter()
    run(cog.harness_run_all(i))
    cog._wave.assert_not_awaited()
    assert "view" not in i.response.send_message.await_args.kwargs


# ── router hook ──────────────────────────────────────────────────────────
def message(content, guild_id=TEST_GUILD, uid=OWNER):
    m = SimpleNamespace(content=content, author=SimpleNamespace(id=uid, bot=False), guild=SimpleNamespace(id=guild_id),
                        channel=SimpleNamespace(permissions_for=lambda u: discord.Permissions(8)))
    m.reply = AsyncMock(return_value=SimpleNamespace())
    return m


def router(with_harness=True, clone_id=None):
    bot = MagicMock(clone_id=clone_id)
    bot.user = SimpleNamespace(id=BOT)
    bot.get_command = lambda w: None
    bot.tree.walk_commands = lambda: iter([])
    bot.test_harness_cog = make_cog() if with_harness else None
    return wrcog.WordRouterCog(bot)


@pytest.mark.parametrize("word", sorted(wr.HARNESS_WORDS))
def test_owner_in_test_guild_gets_the_panel(word):
    c, m = router(), message(f"<@{BOT}> {word}")
    run(c.on_message(m))
    view = m.reply.await_args.kwargs["view"]
    assert isinstance(view, HarnessPanel)


@pytest.mark.parametrize("kw", [dict(guild_id=OTHER_GUILD), dict(uid=STRANGER)])
def test_everyone_else_never_gets_the_harness_panel(kw):
    c, m = router(), message(f"<@{BOT}> nuke", **kw)
    run(c.on_message(m))
    for call in m.reply.await_args_list:
        assert not isinstance(call.kwargs["view"], HarnessPanel)


def test_clone_bot_never_shows_it():
    c, m = router(with_harness=False, clone_id=3), message(f"<@{BOT}> nuke")
    run(c.on_message(m))
    for call in m.reply.await_args_list:
        assert not isinstance(call.kwargs["view"], HarnessPanel)


def test_no_panel_when_id_unset(monkeypatch):
    monkeypatch.setattr(config, "TEST_GUILD_ID", 0, raising=False)
    c, m = router(), message(f"<@{BOT}> nuke")
    run(c.on_message(m))
    for call in m.reply.await_args_list:
        assert not isinstance(call.kwargs["view"], HarnessPanel)
