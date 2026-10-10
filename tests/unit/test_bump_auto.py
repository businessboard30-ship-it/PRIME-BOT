"""Premium Auto Bump: the 🤖 button next to 🔁 Bump, the premium gate, 4 a day, and the worker."""
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from discord_bot.cogs import bump

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


LISTING = dict(id=7, guild_id=77, clone_id=None, name="Cool", listing_type="server", status="approved",
               auto_bump_enabled=False, invite_url="https://discord.gg/x")
CONFIG = dict(bump_channel_id=999, receives_bumps=True, language="any")


def make_db(**over):
    base = dict(
        bump_get_listing=AsyncMock(return_value=dict(LISTING)),
        bump_get_guild_config=AsyncMock(return_value=dict(CONFIG)),
        is_guild_premium_active=AsyncMock(return_value=True),
        bump_set_auto=AsyncMock(return_value={"id": 7}),
        bump_check_cooldown=AsyncMock(return_value=(True, 0)),
        bump_mark_auto_checked=AsyncMock(),
        bump_get_auto_due=AsyncMock(return_value=[]),
    )
    base.update(over)
    return SimpleNamespace(**base)


def interaction(manage=True, guild_id=77):
    i = MagicMock()
    i.guild_id, i.user.id = guild_id, 5
    i.permissions = SimpleNamespace(manage_guild=manage)
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    i.message.edit = AsyncMock()
    return i


@pytest.fixture()
def env(monkeypatch):
    db = make_db()
    monkeypatch.setattr(bump, "db", db)
    ac = importlib.import_module("modules.admin_controls")      # re-imported by other test modules: patch the live one
    monkeypatch.setattr(ac, "current_switches", AsyncMock(return_value=set()))
    return SimpleNamespace(db=db, ac=ac)


def said(i):
    return i.followup.send.await_args.args[0]


# ------------------------------------------------------------------ 4 a day

def test_four_times_a_day_is_every_six_hours():
    assert bump.AUTO_BUMP_PER_DAY == 4
    assert bump.AUTO_BUMP_INTERVAL_SECONDS == 6 * 3600
    assert (24 * 3600) // bump.AUTO_BUMP_INTERVAL_SECONDS == 4


def test_due_query_spaces_from_the_last_bump_of_any_kind():
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def bump_get_auto_due"):src.index("async def bump_mark_auto_checked")]
    assert "auto_bump_enabled" in body and "last_bump_at <= NOW() - ($2" in body
    assert "auto_bump_checked_at <= NOW() - ($3" in body and "status = 'approved'" in body


# ------------------------------------------------------------------ the button row

def test_prompt_row_has_bump_then_auto_bump_next_to_it():
    ids = [c.custom_id for c in bump._prompt_view(7, False).children if getattr(c, "custom_id", None)]
    assert ids == ["bump:prompt:7", "bump:auto:7"]


def test_label_shows_state():
    off = bump._prompt_view(7, False).children[1]
    on = bump._prompt_view(7, True).children[1]
    assert off.item.label == "Auto Bump" and on.item.label == "Auto Bump: ON"


def test_button_is_registered_for_old_messages():
    src = (ROOT / "discord_bot/cogs/bump.py").read_text()
    assert "DynamicBumpAutoButton, DynamicBumpApproveButton" in src


# ------------------------------------------------------------------ pressing it

def press(env, **kw):
    i = interaction(**kw)
    run(bump.DynamicBumpAutoButton(7).callback(i))
    return i


def test_needs_manage_server(env):
    i = press(env, manage=False)
    assert "Manage Server" in said(i)
    env.db.bump_set_auto.assert_not_awaited()


def test_free_server_gets_the_premium_message_and_nothing_is_switched_on(env):
    env.db.is_guild_premium_active.return_value = False
    i = press(env)
    msg = said(i)
    assert "Premium" in msg and "/premium" in msg and "4" in msg
    env.db.bump_set_auto.assert_not_awaited()


def test_premium_server_turns_it_on_and_refreshes_the_row(env):
    i = press(env)
    env.db.bump_set_auto.assert_awaited_once_with(7, True, 5)
    assert "Auto Bump is on" in said(i)
    row = i.message.edit.await_args.kwargs["view"].children[1]
    assert row.item.label == "Auto Bump: ON"


def test_turning_off_never_needs_premium(env):
    env.db.bump_get_listing.return_value = dict(LISTING, auto_bump_enabled=True)
    env.db.is_guild_premium_active.return_value = False          # lapsed Premium must not trap it on
    i = press(env)
    env.db.bump_set_auto.assert_awaited_once_with(7, False, 5)
    assert "off" in said(i)


def test_other_server_cannot_press_it(env):
    i = press(env, guild_id=1)
    assert "different server" in said(i)
    env.db.bump_set_auto.assert_not_awaited()


def test_needs_bump_setup_first(env):
    env.db.bump_get_guild_config.return_value = dict(bump_channel_id=None)
    i = press(env)
    assert "bumpsetup" in said(i)
    env.db.bump_set_auto.assert_not_awaited()


def test_kill_switch_blocks_turning_it_on(env):
    env.ac.current_switches.return_value = {"bump"}
    i = press(env)
    assert "paused" in said(i)
    env.db.bump_set_auto.assert_not_awaited()


# ------------------------------------------------------------------ the worker

@pytest.fixture()
def cog(env):
    c = bump.BumpCog.__new__(bump.BumpCog)
    guild = MagicMock()
    chan = MagicMock()
    chan.send = AsyncMock()
    chan.guild.me = MagicMock()
    chan.permissions_for.return_value = SimpleNamespace(send_messages=True)
    bot = MagicMock()
    bot.get_guild.return_value = guild
    bot.get_channel.return_value = chan
    bot.clone_id = None
    c.bot = bot
    c._cooldown_seconds = AsyncMock(return_value=3600)
    c._send_bump = AsyncMock(return_value={"refreshed": dict(LISTING, auto_bump_enabled=True)})
    c._post_bump_prompt = AsyncMock()
    c.chan = chan
    return c


def on_listing(env, **kw):
    env.db.bump_get_listing.return_value = dict(LISTING, auto_bump_enabled=True, **kw)


def test_due_listing_is_bumped_and_prompt_marks_it_auto(env, cog):
    on_listing(env)
    assert run(cog._auto_bump_one({"id": 7, "guild_id": 77, "clone_id": None})) == "bumped"
    cog._send_bump.assert_awaited_once()
    assert cog._post_bump_prompt.await_args.kwargs["auto"] is True


def test_premium_ending_switches_it_off_and_says_so_once(env, cog):
    on_listing(env)
    env.db.is_guild_premium_active.return_value = False
    assert run(cog._auto_bump_one({"id": 7, "guild_id": 77, "clone_id": None})) == "premium_ended"
    env.db.bump_set_auto.assert_awaited_once_with(7, False)
    cog._send_bump.assert_not_awaited()
    assert "Premium has ended" in cog.chan.send.await_args.args[0]


def test_cooldown_or_missing_channel_or_switch_off_means_no_bump(env, cog):
    on_listing(env)
    env.db.bump_check_cooldown.return_value = (False, 100)
    assert run(cog._auto_bump_one({"id": 7, "guild_id": 77, "clone_id": None})) == "skipped"
    env.db.bump_check_cooldown.return_value = (True, 0)
    env.db.bump_get_guild_config.return_value = dict(bump_channel_id=None)
    assert run(cog._auto_bump_one({"id": 7, "guild_id": 77, "clone_id": None})) == "skipped"
    env.db.bump_get_guild_config.return_value = dict(CONFIG)
    env.db.bump_get_listing.return_value = dict(LISTING, auto_bump_enabled=False)
    assert run(cog._auto_bump_one({"id": 7, "guild_id": 77, "clone_id": None})) == "skipped"
    cog._send_bump.assert_not_awaited()


def test_left_guild_is_skipped(env, cog):
    cog.bot.get_guild.return_value = None
    assert run(cog._auto_bump_one({"id": 7, "guild_id": 77, "clone_id": None})) == "gone"


def test_worker_stamps_before_bumping_and_caps_the_batch(env, cog):
    env.db.bump_get_auto_due.return_value = [{"id": 7, "guild_id": 77, "clone_id": None}]
    order = []
    env.db.bump_mark_auto_checked.side_effect = lambda i: order.append("stamp")
    cog._auto_bump_one = AsyncMock(side_effect=lambda s: order.append("bump") or "bumped")
    run(cog.auto_bump_worker.coro(cog))
    assert order == ["stamp", "bump"]
    assert env.db.bump_get_auto_due.await_args.args == (None, 6 * 3600, bump.AUTO_BUMP_RETRY_SECONDS, bump.MAX_AUTO_BUMPS_PER_TICK)


def test_worker_does_nothing_while_bump_is_switched_off(env, cog):
    env.ac.current_switches.return_value = {"bump"}
    run(cog.auto_bump_worker.coro(cog))
    env.db.bump_get_auto_due.assert_not_awaited()


def test_one_bad_listing_does_not_stop_the_rest(env, cog):
    env.db.bump_get_auto_due.return_value = [{"id": 1, "guild_id": 1, "clone_id": None},
                                             {"id": 2, "guild_id": 2, "clone_id": None}]
    cog._auto_bump_one = AsyncMock(side_effect=[RuntimeError("boom"), "bumped"])
    run(cog.auto_bump_worker.coro(cog))
    assert cog._auto_bump_one.await_count == 2
