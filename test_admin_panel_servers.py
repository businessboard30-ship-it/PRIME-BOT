"""Owner-panel Phase 3 tests: Servers, Find and Clones (real discord.py components, stubbed DB/config)."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

OWNER, OTHER = 111, 222
MAIN = "discord_bot.cogs._views_admin_panel"
MOD = "discord_bot.cogs._views_admin_panel_servers"

CLONES = [{"clone_id": 7, "bot_username": "CloneSeven", "owner_id": 501},
          {"clone_id": 9, "bot_username": "CloneNine", "owner_id": 502}]


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    dbm.db.list_active_discord_clones = AsyncMock(return_value=list(CLONES))
    dbm.db.get_discord_guild_count = AsyncMock(return_value=4)
    dbm.db.search_discord_guilds = AsyncMock(return_value=[
        {"guild_id": 1001, "guild_name": "Anime Hub", "clone_id": None, "bot_username": None, "member_count": 50}])
    dbm.db.search_cached_usernames = AsyncMock(return_value=[{"user_id": 42, "username": "someone"}])
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    sys.modules.pop(MAIN, None)
    sys.modules.pop(MOD, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(MOD)
    mod.main = main
    yield mod
    sys.modules.pop(MAIN, None)
    sys.modules.pop(MOD, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, guild=None, values=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = guild
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    i.response.is_done = MagicMock(return_value=False)
    i.followup.edit_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def cog():
    c = MagicMock()
    c.clone_admin.allservers = AsyncMock()
    c.clone_admin.ownermonetize = AsyncMock()
    c.admin_cog.commissions = AsyncMock()
    c.admin_cog.subscribers = AsyncMock()
    c.admin_cog.clones = AsyncMock()
    c.lookup.find = AsyncMock()
    return c


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def count(view):
    return sum(1 for _ in view.walk_children())


# ── access / navigation ──────────────────────────────────────────────────

def test_servers_section_follows_clone_admin_allowlist(vp):
    assert "servers" in vp.main.allowed_sections(OWNER)
    assert "servers" not in vp.main.allowed_sections(OTHER)


def test_home_has_servers_button_and_it_is_gated(vp, monkeypatch):
    async def go():
        assert not buttons(vp.main.HomeView(cog(), OWNER))["Servers & clones"].disabled
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        assert buttons(vp.main.HomeView(cog(), OWNER))["Servers & clones"].disabled
    run(go())


def test_home_to_hub_and_back(vp):
    async def go():
        c = cog(); home = vp.main.HomeView(c, OWNER); i = I()
        await buttons(home)["Servers & clones"].callback(i)
        hub = i.response.edit_message.await_args.kwargs["view"]
        assert isinstance(hub, vp.ServersHubView)
        i2 = I()
        await buttons(hub)["Back"].callback(i2)
        assert isinstance(i2.response.edit_message.await_args.kwargs["view"], vp.main.HomeView)
    run(go())


def test_hub_render_within_discord_limits(vp):
    async def go():
        v = vp.ServersHubView(cog(), OWNER)
        assert {"All servers", "Find server / person", "Clones", "Commissions", "Subscribers", "Back"} <= set(buttons(v))
        assert count(v) < 40
    run(go())


def test_hub_blocks_non_opener_and_revoked(vp, monkeypatch):
    async def go():
        v = vp.ServersHubView(cog(), OWNER)
        assert await v.interaction_check(I(user=OTHER)) is False
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", set())
        assert await v.interaction_check(I(user=OWNER)) is False
    run(go())


# ── hub actions ──────────────────────────────────────────────────────────

def test_server_list_respects_include_left_toggle(vp):
    async def go():
        c = cog(); v = vp.ServersHubView(c, OWNER)
        await buttons(v)["All servers"].callback(I())
        assert c.clone_admin.allservers.await_args.kwargs == {"include_left": False}
        i = I()
        await buttons(v)["Include left: off"].callback(i)
        i.response.edit_message.assert_awaited()
        assert "Include left: on" in buttons(v)
        await buttons(v)["All servers"].callback(I())
        assert c.clone_admin.allservers.await_args.kwargs == {"include_left": True}
    run(go())


def test_commissions_and_subscribers_call_the_slash_command_code(vp):
    async def go():
        c = cog(); v = vp.ServersHubView(c, OWNER)
        i = I(); await buttons(v)["Commissions"].callback(i)
        c.admin_cog.commissions.assert_awaited_once_with(i)
        i2 = I(); await buttons(v)["Subscribers"].callback(i2)
        c.admin_cog.subscribers.assert_awaited_once_with(i2)
    run(go())


def test_missing_module_is_reported_not_crashed(vp):
    async def go():
        c = cog(); c.admin_cog = None
        v = vp.ServersHubView(c, OWNER); i = I()
        await buttons(v)["Commissions"].callback(i)
        assert "isn't loaded" in i.response.send_message.await_args.args[0]
    run(go())


def test_call_cmd_handles_app_command_objects(vp):
    async def go():
        seen = {}

        class Owner:
            async def thing(self, interaction: discord.Interaction, limit: int = 1):
                seen["args"] = (self, interaction, limit)

        owner = Owner()
        # Mirror how `@admin.command(...)` leaves a Command object on the cog.
        owner.thing = app_commands.Command(name="thing", description="d", callback=Owner.thing)
        i = I()
        await vp.main.call_cmd(owner, "thing", i, limit=5)
        assert seen["args"] == (owner, i, 5)
    run(go())


# ── find ─────────────────────────────────────────────────────────────────

def _find_modal(vp, c, text):
    m = vp.FindModal(c)
    m.query._value = text
    return m


def test_find_with_id_goes_straight_to_find(vp):
    async def go():
        c = cog(); i = I()
        await _find_modal(vp, c, " 123456789012345678 ").on_submit(i)
        c.lookup.find.assert_awaited_once_with(i, query="123456789012345678")
    run(go())


def test_find_with_name_shows_pick_list(vp):
    async def go():
        c = cog(); i = I()
        await _find_modal(vp, c, "anime").on_submit(i)
        view = i.response.send_message.await_args.kwargs["view"]
        assert isinstance(view, vp.FindResultsView)
        opts = selects(view)[0].options
        assert [o.value for o in opts] == ["g:1001", "u:42"]
        c.lookup.find.assert_not_awaited()
    run(go())


def test_find_results_pick_opens_card(vp):
    async def go():
        c = cog()
        v = vp.FindResultsView(c, OWNER, "servers", "anime",
                               [{"guild_id": 1001, "guild_name": "Anime Hub", "clone_id": 3,
                                 "bot_username": "x", "member_count": 9}], [])
        i = I(values=["g:1001"])
        await selects(v)[0].callback(i)
        c.lookup.find.assert_awaited_once_with(i, query="g:1001")
    run(go())


def test_find_results_never_exceed_select_limit(vp):
    async def go():
        guilds = [{"guild_id": n, "guild_name": f"g{n}", "clone_id": None, "member_count": 1} for n in range(40)]
        people = [{"user_id": n, "username": f"u{n}"} for n in range(40)]
        v = vp.FindResultsView(cog(), OWNER, "servers", "q", guilds, people)
        assert len(selects(v)[0].options) <= 25
    run(go())


def test_find_with_no_hits_says_so(vp):
    async def go():
        vp.db.search_discord_guilds.return_value = []
        vp.db.search_cached_usernames.return_value = []
        c = cog(); i = I()
        await _find_modal(vp, c, "zzz").on_submit(i)
        assert "Nothing matching" in i.response.send_message.await_args.args[0]
    run(go())


def test_find_modal_rechecks_access(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        c = cog(); i = I()
        await _find_modal(vp, c, "anime").on_submit(i)
        assert "no longer authorized" in i.response.send_message.await_args.args[0]
        vp.db.search_discord_guilds.assert_not_awaited()
        c.lookup.find.assert_not_awaited()
    run(go())


# ── clones / force-monetize ──────────────────────────────────────────────

def test_clones_pick_shows_details(vp):
    async def go():
        v = vp.ClonesView(cog(), OWNER, "servers", list(CLONES)); i = I(values=["7"])
        await selects(v)[0].callback(i)
        assert v.selected == 7 and v.server_count == 4
        i.response.edit_message.assert_awaited()
    run(go())


def test_force_monetize_is_two_step_and_needs_a_selection(vp):
    async def go():
        c = cog(); v = vp.ClonesView(c, OWNER, "servers", list(CLONES))
        assert buttons(v)["Force-activate monetization"].disabled
        await selects(v)[0].callback(I(values=["9"]))
        i = I(); await buttons(v)["Force-activate monetization"].callback(i)   # step 1: arm only
        c.clone_admin.ownermonetize.assert_not_awaited()
        assert "Confirm activation" in buttons(v)
        i2 = I(); await buttons(v)["Confirm activation"].callback(i2)          # step 2: act
        c.clone_admin.ownermonetize.assert_awaited_once_with(i2, clone_id=9)
        assert "Force-activate monetization" in buttons(v)                      # re-armed back to step 1
    run(go())


def test_changing_clone_disarms_confirm(vp):
    async def go():
        c = cog(); v = vp.ClonesView(c, OWNER, "servers", list(CLONES))
        await selects(v)[0].callback(I(values=["7"]))
        await buttons(v)["Force-activate monetization"].callback(I())
        await selects(v)[0].callback(I(values=["9"]))
        assert "Force-activate monetization" in buttons(v)
        c.clone_admin.ownermonetize.assert_not_awaited()
    run(go())


def test_manage_opens_existing_clone_wizard(vp):
    async def go():
        c = cog(); v = vp.ClonesView(c, OWNER, "servers", list(CLONES)); i = I()
        await buttons(v)["Manage / deactivate"].callback(i)
        c.admin_cog.clones.assert_awaited_once_with(i)
    run(go())


def test_clones_view_with_no_clones_and_with_many(vp):
    async def go():
        empty = vp.ClonesView(cog(), OWNER, "servers", [])
        assert not selects(empty) and buttons(empty)["Manage / deactivate"].disabled
        many = [{"clone_id": n, "bot_username": f"b{n}", "owner_id": 1} for n in range(40)]
        v = vp.ClonesView(cog(), OWNER, "servers", many)
        assert len(selects(v)[0].options) == 25 and v.total == 40
    run(go())
