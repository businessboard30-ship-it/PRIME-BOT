"""Server Owners Panel Phase 2 tests: Community, Tickets, Channels & logs.
Covers access, component limits, clone scoping, two-step removal, and failures."""
import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, MANAGER, OWNER, buttons, guild, interaction, make_db, run, text_of,
)

SP = "modules.server_panel"
V1 = "discord_bot.cogs._views_server_panel"
V2 = "discord_bot.cogs._views_server_panel_p2"


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.get_voice_xp_config = AsyncMock(return_value={"enabled": True, "xp_per_minute": 10, "afk_channel_excluded": True})
    db.set_voice_xp_config = AsyncMock()
    db.get_starboard_config = AsyncMock(return_value={"channel_id": None, "threshold": 5, "emoji": "⭐"})
    db.set_starboard_config = AsyncMock()
    db.get_suggestion_config = AsyncMock(return_value={"approved_log_channel_id": None})
    db.set_suggestion_config = AsyncMock()
    db.get_reaction_role_panels_for_guild = AsyncMock(return_value=[])
    db.get_level_roles = AsyncMock(return_value=[{"level": 5, "role_id": 77}, {"level": 10, "role_id": 78}])
    db.add_level_role = AsyncMock(return_value=True)
    db.remove_level_role = AsyncMock(return_value=True)
    db.get_ticket_config = AsyncMock(return_value={
        "support_role_id": None, "category_id": None, "panel_channel_id": None, "welcome_message": None})
    db.set_ticket_config = AsyncMock()
    db.get_leveling_config = AsyncMock(return_value={
        "announce_channel_id": None, "xp_rate": "default", "leaderboard_autopost_channel_id": 55})
    db.set_leveling_config = AsyncMock()
    dbm.db = db
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS = 3
    cfg.PREMIUM_FEE_USD = 2
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    for name in (SP, V1, V2):
        sys.modules.pop(name, None)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    sc.SetupSuggestView = MagicMock()
    sc.build_suggestions_embed = MagicMock(return_value=discord.Embed(title="x"))
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    sp = importlib.import_module(SP)
    v1 = importlib.import_module(V1)
    v2 = importlib.import_module(V2)
    yield SimpleNamespace(sp=sp, v1=v1, v2=v2, db=db)
    for name in (SP, V1, V2):
        sys.modules.pop(name, None)


def screens(v2):
    return [v2.CommunityView, v2.LevelingView, v2.LevelRolesView, v2.StarboardView, v2.SuggestionsView,
            v2.GiveawaysView, v2.ReactionRolesView, v2.TicketsView, v2.ChannelsLogsView]


def selects(view):
    return [c for c in view.walk_children()
            if isinstance(c, (discord.ui.Select, discord.ui.ChannelSelect, discord.ui.RoleSelect))]


# ── shared rules ─────────────────────────────────────────────────────────

def test_component_and_row_limits(env):
    async def go():
        for cls in screens(env.v2):
            v = await cls.create(interaction())
            assert len(list(v.walk_children())) < env.v1.MAX_COMPONENTS, cls.__name__
            for row in (c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)):
                assert len(row.children) <= 5, cls.__name__
            for s in selects(v):
                if isinstance(s, discord.ui.Select):
                    assert len(s.options) <= 25, cls.__name__
    run(go())


def test_every_screen_has_hint_and_back(env):
    async def go():
        for cls in screens(env.v2):
            v = await cls.create(interaction())
            assert "/serversetup" in text_of(v), cls.__name__
            assert "Back" in buttons(v), cls.__name__
    run(go())


def test_access_revoked_mid_session(env):
    async def go():
        for cls in screens(env.v2):
            v = await cls.create(interaction(user=MANAGER))
            assert await v.interaction_check(interaction(user=MANAGER)) is True
            bad = interaction(user=MANAGER, manage=False)
            assert await v.interaction_check(bad) is False, cls.__name__
    run(go())


def test_home_links_to_phase2_hubs(env):
    async def go():
        v = await env.v1.HomeView.create(interaction())
        b = buttons(v)
        for label in ("Community", "Tickets", "Channels & logs"):
            assert label in b
        assert len(list(v.walk_children())) < env.v1.MAX_COMPONENTS
    run(go())


def test_modals_recheck_access(env):
    async def go():
        revoked = interaction(user=OWNER, manage=False)
        revoked.guild.owner_id = 999
        m = env.v2.StarEmojiModal("Starboard emoji", OWNER)
        m.emoji._value = "🔥"
        await m.on_submit(revoked)
        env.db.set_starboard_config.assert_not_awaited()
        t = env.v2.TicketGreetingModal("", OWNER)
        t.text._value = "hello"
        await t.on_submit(revoked)
        env.db.set_ticket_config.assert_not_awaited()
        other = interaction(user=MANAGER)
        lm = env.v2.LevelModal(77, OWNER)   # opened by someone else
        lm.level._value = "5"
        await lm.on_submit(other)
        env.db.add_level_role.assert_not_awaited()
    run(go())


# ── clone isolation ──────────────────────────────────────────────────────

def test_reads_and_writes_carry_clone_id(env):
    async def go():
        i = interaction(clone_id=9)
        v = await env.v2.StarboardView.create(i)
        await env.v2.StarboardView._threshold(v, i, 10)
        env.db.get_starboard_config.assert_any_await(GUILD_ID, 9)
        assert env.db.set_starboard_config.await_args.kwargs["clone_id"] == 9
        assert env.db.set_starboard_config.await_args.kwargs["threshold"] == 10

        v = await env.v2.TicketsView.create(i)
        await v._role(i, 321)
        assert env.db.set_ticket_config.await_args.kwargs == {"clone_id": 9, "support_role_id": 321}

        v = await env.v2.LevelingView.create(i)
        await v._voice(i)
        assert env.db.set_voice_xp_config.await_args.kwargs["clone_id"] == 9
        env.db.get_level_roles.assert_not_awaited()
        v = await env.v2.LevelRolesView.create(i)
        env.db.get_level_roles.assert_awaited_with(GUILD_ID, 9)
    run(go())


# ── level roles: two-step removal and validation ─────────────────────────

def test_remove_level_role_is_two_step(env):
    async def go():
        i = interaction()
        v = await env.v2.LevelRolesView.create(i)
        await v._pick_remove(i, 5)           # first step
        env.db.remove_level_role.assert_not_awaited()
        assert "Confirm remove Lv 5" in buttons(v)
        await v._confirm_remove(i)           # second step
        env.db.remove_level_role.assert_awaited_once_with(GUILD_ID, 5, clone_id=None)
    run(go())


def test_remove_cancel_does_nothing(env):
    async def go():
        i = interaction()
        v = await env.v2.LevelRolesView.create(i)
        await v._pick_remove(i, 10)
        await v._cancel(i)
        env.db.remove_level_role.assert_not_awaited()
        assert "Confirm remove Lv 10" not in buttons(v)
    run(go())


def test_add_requires_role_and_blocks_high_roles(env):
    async def go():
        i = interaction()
        v = await env.v2.LevelRolesView.create(i)
        assert buttons(v)["Add at level…"].disabled is True
        g = guild()
        role = MagicMock()
        role.is_default.return_value = False
        role.managed = False
        role.__ge__ = lambda self, other: True      # at/above bot's top role
        g.me = MagicMock()
        assert "above" in env.sp.role_blocked_reason(g, role)
        managed = MagicMock(); managed.is_default.return_value = False; managed.managed = True
        assert env.sp.role_blocked_reason(g, managed)
        assert env.sp.role_blocked_reason(g, None)
    run(go())


def test_level_modal_validates_number_and_saves(env):
    async def go():
        i = interaction()
        role = MagicMock(); role.id = 77
        role.is_default.return_value = False; role.managed = False
        role.__ge__ = lambda self, other: False
        i.guild.get_role = MagicMock(return_value=role)
        i.guild.me = MagicMock()
        bad = env.v2.LevelModal(77, OWNER); bad.level._value = "abc"
        await bad.on_submit(i)
        env.db.add_level_role.assert_not_awaited()
        zero = env.v2.LevelModal(77, OWNER); zero.level._value = "0"
        await zero.on_submit(i)
        env.db.add_level_role.assert_not_awaited()
        ok = env.v2.LevelModal(77, OWNER); ok.level._value = "15"
        await ok.on_submit(i)
        env.db.add_level_role.assert_awaited_once_with(GUILD_ID, 15, 77, clone_id=None)
    run(go())


# ── behaviour ────────────────────────────────────────────────────────────

def test_starboard_disable_clears_channel(env):
    async def go():
        env.db.get_starboard_config.return_value = {"channel_id": 12, "threshold": 5, "emoji": "⭐"}
        i = interaction()
        v = await env.v2.StarboardView.create(i)
        await buttons(v)["Disable"].callback(i)
        assert env.db.set_starboard_config.await_args.kwargs["channel_id"] is None
    run(go())


def test_log_categories_write_all_flags_in_one_call(env):
    async def go():
        i = interaction()
        v = await env.v2.ChannelsLogsView.create(i)
        await v._categories(i, {"moderation", "voice"})
        kwargs = env.db.set_automod_config.await_args.kwargs
        assert kwargs["log_moderation_enabled"] is True and kwargs["log_voice_enabled"] is True
        assert kwargs["log_server_enabled"] is False and len(
            [k for k in kwargs if k.startswith("log_")]) == 7
        assert env.db.set_automod_config.await_count == 1
    run(go())


def test_autopost_off_button_only_when_set(env):
    async def go():
        v = await env.v2.ChannelsLogsView.create(interaction())
        assert "Autopost off" in buttons(v)
        env.db.get_leveling_config.return_value["leaderboard_autopost_channel_id"] = None
        v2 = await env.v2.ChannelsLogsView.create(interaction())
        assert "Autopost off" not in buttons(v2)
    run(go())


def test_missing_channels_needs_manage_channels(env):
    async def go():
        i = interaction(manage=False)
        i.permissions = SimpleNamespace(manage_guild=True, administrator=False, manage_channels=False)
        v = await env.v2.ChannelsLogsView.create(i)
        await buttons(v)["Create missing channels"].callback(i)
        i.response.send_message.assert_awaited()
        i.response.defer.assert_not_awaited()
    run(go())


def test_giveaway_button_runs_existing_wizard(env):
    async def go():
        i = interaction()
        cog = MagicMock()
        cog.setup_wizard.callback = AsyncMock()
        i.client.get_cog = MagicMock(return_value=cog)
        v = await env.v2.GiveawaysView.create(i)
        await buttons(v)["Open giveaway wizard"].callback(i)
        cog.setup_wizard.callback.assert_awaited_once_with(cog, i)
        i.client.get_cog = MagicMock(return_value=None)
        j = interaction()
        j.client.get_cog = MagicMock(return_value=None)
        await buttons(v)["Open giveaway wizard"].callback(j)
        j.response.send_message.assert_awaited()
    run(go())


def test_reaction_panel_modal_calls_existing_create(env):
    async def go():
        i = interaction()
        cog = MagicMock()
        cog.create.callback = AsyncMock()
        i.client.get_cog = MagicMock(return_value=cog)
        m = env.v2.ReactionPanelModal("New", OWNER)
        m.panel_title._value = "Pick roles"
        m.panel_desc._value = ""
        await m.on_submit(i)
        kwargs = cog.create.callback.await_args.kwargs
        assert kwargs["title"] == "Pick roles" and kwargs["description"]
    run(go())


# ── failure paths ────────────────────────────────────────────────────────

def test_audit_failure_does_not_block_write(env):
    async def go():
        await env.sp.set_starboard(GUILD_ID, None, OWNER, threshold=3)   # get_pool raises inside audit
        env.db.set_starboard_config.assert_awaited_once()
    run(go())


def test_database_down_on_open_raises_for_caller_to_handle(env):
    async def go():
        env.db.get_ticket_config = AsyncMock(side_effect=RuntimeError("db down"))
        with pytest.raises(RuntimeError):
            await env.v2.TicketsView.create(interaction())
    run(go())
