"""Owner-panel Batch 6 tests: health dashboard, server inspector, user inspector."""
import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, HELPER, OTHER = 111, 333, 222
MAIN = "discord_bot.cogs._views_admin_panel"
CTRL = "discord_bot.cogs._views_admin_panel_controls"
OPS = "discord_bot.cogs._views_admin_panel_ops"
INSV = "discord_bot.cogs._views_admin_panel_inspect"
AC = "modules.admin_controls"
AO = "modules.admin_ops"
AI = "modules.admin_inspect"
NOW = datetime.now(timezone.utc)
MODULES = (MAIN, CTRL, OPS, INSV, AC, AO, AI)
GID = 555000000000000001
UID = 777000000000000002


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    cfg.PREMIUM_GRACE_DAYS = 3
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(INSV)
    mod.main = main
    mod.ac = importlib.import_module(AC)
    mod.ai = importlib.import_module(AI)
    mod.audit = MagicMock()
    mod.cfg = cfg
    yield mod
    for name in MODULES:
        sys.modules.pop(name, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = None
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer", "edit_original_response"):
        setattr(i.response, n, AsyncMock())
    i.edit_original_response = AsyncMock()
    return i


def cog(bot=None):
    c = MagicMock()
    c.bot = bot if bot is not None else MagicMock()
    return c


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def count(view):
    return sum(1 for _ in view.walk_children())


def texts(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def row(clone_id=None, owner=999000000000000001, left=False, name="Anime Hub"):
    return {"guild_id": GID, "clone_id": clone_id, "guild_name": name, "member_count": 120,
            "owner_id": owner, "joined_at": NOW - timedelta(days=30),
            "left_at": NOW if left else None, "bot_username": None if clone_id is None else "clonebot"}


def server_view(vp, rows=None, extras=None, bot=None):
    v = vp.ServerInspectView(cog(bot), OWNER, GID)
    v.rows = rows if rows is not None else [row()]
    v.extras = extras if extras is not None else {"blocked": None, "premium": [], "reports": {}}
    v._build()
    return v


def user_card(**over):
    card = {"user_id": UID, "username": "somebody", "blocked": None, "owned": [], "clones": [],
            "xp": [], "xp_servers": 0, "coins_total": 0, "coins_servers": 0, "payments": []}
    card.update(over)
    return card


def user_view(vp, **over):
    v = vp.UserInspectView(cog(), OWNER, UID)
    v.card = user_card(**over)
    v._build()
    return v


# ── access and Home ──────────────────────────────────────────────────────

def test_sections_owner_only_and_not_grantable(vp, monkeypatch):
    main = vp.main
    assert {"health", "inspect"} <= main.allowed_sections(OWNER)
    assert not ({"health", "inspect"} & main.allowed_sections(OTHER))
    assert "health" not in vp.ac.GRANTABLE and "inspect" not in vp.ac.GRANTABLE


def test_home_has_new_buttons_and_stays_under_component_limit(vp):
    async def go():
        v = vp.main.HomeView(cog(), OWNER)
        b = buttons(v)
        assert {"Health", "Inspect server", "Inspect user"} <= set(b)
        assert not b["Health"].disabled and not b["Inspect server"].disabled
        assert count(v) < 40
    run(go())


def test_home_buttons_disabled_without_access(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        v = vp.main.HomeView(cog(), OWNER)
        b = buttons(v)
        assert b["Health"].disabled and b["Inspect server"].disabled and b["Inspect user"].disabled
    run(go())


def test_home_inspect_buttons_open_lookup_modal(vp):
    async def go():
        v = vp.main.HomeView(cog(), OWNER)
        i = I()
        await buttons(v)["Inspect server"].callback(i)
        modal = i.response.send_modal.await_args.args[0]
        assert isinstance(modal, vp.LookupModal) and modal.kind == "server"
        i2 = I()
        await buttons(v)["Inspect user"].callback(i2)
        assert i2.response.send_modal.await_args.args[0].kind == "user"
    run(go())


def test_modals_recheck_access(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.main, "DISCORD_CLONE_ADMIN_IDS", set())
        monkeypatch.setattr(vp.main, "DISCORD_OWNER_BROADCAST_IDS", set())
        for modal in (vp.LookupModal(cog(), "server"), vp.CoinsModal(user_view(vp)),
                      vp.ResetXpModal(user_view(vp)), vp.BlockReasonModal(server_view(vp))):
            i = I(user=OTHER)
            for item in modal.children:
                item._value = "123456789012345678"
            await modal.on_submit(i)
            i.response.send_message.assert_awaited()
            assert "no longer authorized" in i.response.send_message.await_args.args[0]
    run(go())


# ── health ───────────────────────────────────────────────────────────────

def test_health_loads_and_reports_problems(vp, monkeypatch):
    async def go():
        ai = vp.ai
        stale = {"clone_id": 4, "bot_username": "slowbot", "last_heartbeat": NOW - timedelta(hours=2)}
        fresh = {"clone_id": 5, "bot_username": "okbot", "last_heartbeat": NOW}
        monkeypatch.setattr(ai, "db_ping", AsyncMock(return_value=12.0))
        monkeypatch.setattr(ai, "clone_heartbeats", AsyncMock(return_value=[stale, fresh]))
        monkeypatch.setattr(ai, "loop_report", lambda bot: (9, ["Bump.poll"]))
        monkeypatch.setattr(ai, "error_counts", lambda *a, **k: (3, 7))
        bot = MagicMock(); bot.latency = 0.123; bot.guilds = [1, 2]; bot.cogs = {"a": 1}
        v = vp.HealthView(cog(bot), OWNER)
        await v.load()
        text = texts(v)
        assert "123 ms" in text and "2 servers" in text
        assert "ok (12 ms)" in text
        assert "3 error(s), 7 warning(s)" in text
        assert "Bump.poll" in text and "9 running" in text
        assert "1 of 2 quiet" in text and "slowbot" in text and "okbot" not in text
    run(go())


def test_health_survives_everything_failing(vp, monkeypatch):
    async def go():
        ai = vp.ai
        monkeypatch.setattr(ai, "db_ping", AsyncMock(return_value=None))
        monkeypatch.setattr(ai, "clone_heartbeats", AsyncMock(return_value=None))
        bot = MagicMock(); bot.latency = float("nan")
        v = vp.HealthView(cog(bot), OWNER)
        await v.load()
        text = texts(v)
        assert "not reachable" in text and "latency unknown" in text and "couldn't be checked" in text
    run(go())


def test_health_refresh_defers_then_edits_and_back_goes_home(vp, monkeypatch):
    async def go():
        ai = vp.ai
        monkeypatch.setattr(ai, "db_ping", AsyncMock(return_value=5.0))
        monkeypatch.setattr(ai, "clone_heartbeats", AsyncMock(return_value=[]))
        v = vp.HealthView(cog(), OWNER)
        await v.load()
        i = I()
        await buttons(v)["Refresh"].callback(i)
        i.response.defer.assert_awaited()
        i.edit_original_response.assert_awaited()
        i2 = I()
        await buttons(v)["Back"].callback(i2)
        assert isinstance(i2.response.edit_message.await_args.kwargs["view"], vp.main.HomeView)
    run(go())


def test_health_log_tail_button_opens_the_log_view(vp, monkeypatch):
    async def go():
        ai = vp.ai
        monkeypatch.setattr(ai, "db_ping", AsyncMock(return_value=5.0))
        monkeypatch.setattr(ai, "clone_heartbeats", AsyncMock(return_value=[]))
        v = vp.HealthView(cog(), OWNER)
        await v.load()
        i = I()
        await buttons(v)["Log tail"].callback(i)
        nxt = i.response.edit_message.await_args.kwargs["view"]
        assert type(nxt).__name__ == "LogsView"
        i2 = I()
        await buttons(nxt)["Back"].callback(i2)
        assert isinstance(i2.response.edit_message.await_args.kwargs["view"], vp.main.HomeView)
    run(go())


def test_loop_report_counts_running_and_stopped(vp):
    from discord.ext import tasks

    class FakeLoop(tasks.Loop):
        def __init__(self, running):
            self._r = running
        def is_running(self):
            return self._r

    cog_a = types.SimpleNamespace(up=FakeLoop(True), down=FakeLoop(False), other=5)
    bot = types.SimpleNamespace(cogs={"Bump": cog_a})
    running, stopped = vp.ai.loop_report(bot)
    assert running == 1 and stopped == ["Bump.down"]


def test_quiet_clones_rules(vp):
    rows = [{"clone_id": 1, "last_heartbeat": None},
            {"clone_id": 2, "last_heartbeat": NOW - timedelta(minutes=30)},
            {"clone_id": 3, "last_heartbeat": NOW - timedelta(minutes=2)}]
    assert [r["clone_id"] for r in vp.ai.quiet_clones(rows, NOW)] == [1, 2]


# ── lookup ───────────────────────────────────────────────────────────────

def _modal(vp, kind, text):
    m = vp.LookupModal(cog(), kind)
    m.query._value = text
    return m


def test_lookup_by_id_opens_inspector(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ai, "server_rows", AsyncMock(return_value=[row()]))
        monkeypatch.setattr(vp.ai, "server_extras",
                            AsyncMock(return_value={"blocked": None, "premium": [], "reports": {}}))
        i = I()
        await _modal(vp, "server", str(GID)).on_submit(i)
        view = i.response.edit_message.await_args.kwargs["view"]
        assert isinstance(view, vp.ServerInspectView) and view.guild_id == GID
    run(go())


def test_lookup_rejects_bad_id(vp):
    async def go():
        i = I()
        await _modal(vp, "user", "12345").on_submit(i)
        assert "Discord ID" in i.response.send_message.await_args.args[0]
        i.response.edit_message.assert_not_awaited()
    run(go())


def test_lookup_by_name_one_many_none(vp, monkeypatch):
    async def go():
        db = vp.db
        monkeypatch.setattr(vp.ai, "user_card", AsyncMock(return_value=user_card()))
        db.search_cached_usernames = AsyncMock(return_value=[{"user_id": UID, "username": "somebody"}])
        i = I()
        await _modal(vp, "user", "some").on_submit(i)
        assert isinstance(i.response.edit_message.await_args.kwargs["view"], vp.UserInspectView)

        db.search_cached_usernames = AsyncMock(return_value=[
            {"user_id": UID, "username": "somebody"}, {"user_id": UID + 1, "username": "someone"}])
        i = I()
        await _modal(vp, "user", "some").on_submit(i)
        pick = i.response.send_message.await_args.kwargs["view"]
        assert isinstance(pick, vp.PickView) and len(selects(pick)[0].options) == 2
        assert i.response.send_message.await_args.kwargs["ephemeral"] is True

        db.search_cached_usernames = AsyncMock(return_value=[])
        i = I()
        await _modal(vp, "user", "zzz").on_submit(i)
        assert "Nothing matching" in i.response.send_message.await_args.args[0]
    run(go())


def test_pick_view_opens_the_chosen_one(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ai, "user_card", AsyncMock(return_value=user_card()))
        pv = vp.PickView(cog(), OWNER, "user", "some", [{"user_id": UID, "username": "somebody"}])
        i = I(values=[str(UID)])
        await selects(pv)[0].callback(i)
        view = i.response.edit_message.await_args.kwargs["view"]
        assert isinstance(view, vp.UserInspectView) and view.user_id == UID
    run(go())


# ── server inspector ─────────────────────────────────────────────────────

def test_server_card_shows_details(vp):
    extras = {"blocked": {"reason": "spam", "created_at": NOW},
              "premium": [{"clone_id": None, "expires_at": NOW + timedelta(days=10)}],
              "reports": {"new": 2}}
    text = texts(server_view(vp, extras=extras))
    assert "Anime Hub" in text and "120" in text and "Main bot" in text
    assert "💎" in text and "🚫" in text and "spam" in text and "2 new" in text


def test_server_not_in_database(vp):
    v = server_view(vp, rows=[])
    assert "never joined" in texts(v)
    b = buttons(v)
    assert b["Revoke premium"].disabled and b["Message owner"].disabled


def test_premium_lapsed_vs_active_controls(vp):
    lapsed = {"blocked": None, "premium": [{"clone_id": None, "expires_at": NOW - timedelta(days=30)}], "reports": {}}
    assert buttons(server_view(vp, extras=lapsed))["Revoke premium"].disabled
    active = {"blocked": None, "premium": [{"clone_id": None, "expires_at": NOW + timedelta(days=5)}], "reports": {}}
    assert not buttons(server_view(vp, extras=active))["Revoke premium"].disabled


def test_revoke_premium_is_two_step(vp, monkeypatch):
    async def go():
        revoke = AsyncMock(return_value=True)
        monkeypatch.setattr(vp.ac, "revoke_premium", revoke)
        monkeypatch.setattr(vp.ai, "server_rows", AsyncMock(return_value=[row()]))
        monkeypatch.setattr(vp.ai, "server_extras", AsyncMock(return_value={"blocked": None, "premium": [], "reports": {}}))
        extras = {"blocked": None, "premium": [{"clone_id": None, "expires_at": NOW + timedelta(days=5)}], "reports": {}}
        v = server_view(vp, extras=extras)
        i = I()
        await buttons(v)["Revoke premium"].callback(i)
        revoke.assert_not_awaited()
        assert v.confirm == "revoke" and "Confirm revoke" in buttons(v)
        i2 = I()
        await buttons(v)["Confirm revoke"].callback(i2)
        revoke.assert_awaited_once_with(GID, None)
        assert v.confirm is None
        vp.audit.assert_called()
    run(go())


def test_block_and_unblock_server(vp, monkeypatch):
    async def go():
        add, rem = AsyncMock(), AsyncMock(return_value=True)
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        monkeypatch.setattr(vp.ac, "remove_blacklist", rem)
        monkeypatch.setattr(vp.ai, "server_rows", AsyncMock(return_value=[row()]))
        monkeypatch.setattr(vp.ai, "server_extras", AsyncMock(return_value={"blocked": None, "premium": [], "reports": {}}))
        v = server_view(vp)
        i = I()
        await buttons(v)["Block server"].callback(i)
        modal = i.response.send_modal.await_args.args[0]
        assert isinstance(modal, vp.BlockReasonModal)
        modal.reason._value = "raid source"
        i2 = I()
        await modal.on_submit(i2)
        add.assert_awaited_once_with("guild", GID, "raid source", OWNER)

        v2 = server_view(vp, extras={"blocked": {"reason": "x", "created_at": NOW}, "premium": [], "reports": {}})
        i3 = I()
        await buttons(v2)["Unblock server"].callback(i3)
        rem.assert_awaited_once_with("guild", GID)
    run(go())


def test_force_leave_needs_main_bot_and_confirmation(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ai, "server_rows", AsyncMock(return_value=[row()]))
        monkeypatch.setattr(vp.ai, "server_extras", AsyncMock(return_value={"blocked": None, "premium": [], "reports": {}}))
        vp.db.mark_discord_guild_left = AsyncMock()
        # main bot not in the server -> disabled
        bot = MagicMock(); bot.get_guild = MagicMock(return_value=None)
        assert buttons(server_view(vp, bot=bot))["Force-leave"].disabled
        # main bot is in the server -> two-step
        guild = MagicMock(); guild.leave = AsyncMock()
        bot2 = MagicMock(); bot2.get_guild = MagicMock(return_value=guild)
        v = server_view(vp, bot=bot2)
        assert not buttons(v)["Force-leave"].disabled
        await buttons(v)["Force-leave"].callback(I())
        guild.leave.assert_not_awaited()
        await buttons(v)["Confirm leave"].callback(I())
        guild.leave.assert_awaited_once()
        vp.db.mark_discord_guild_left.assert_awaited_once_with(GID, None)
    run(go())


def test_force_leave_failure_is_reported(vp):
    async def go():
        guild = MagicMock(); guild.leave = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
        bot = MagicMock(); bot.get_guild = MagicMock(return_value=guild)
        v = server_view(vp, bot=bot)
        v.confirm = "leave"
        i = I()
        await v._leave(i)
        assert "Couldn't leave" in i.response.send_message.await_args.args[0]
    run(go())


def test_message_owner_sends_dm_and_handles_closed_dms(vp):
    async def go():
        user = MagicMock(); user.send = AsyncMock()
        bot = MagicMock(); bot.get_user = MagicMock(return_value=user)
        v = server_view(vp, bot=bot)
        m = vp.MessageOwnerModal(v)
        m.text._value = "Hello there"
        i = I()
        await m.on_submit(i)
        assert "Hello there" in user.send.await_args.args[0]
        assert "Sent to" in v.notice

        user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "closed"))
        i2 = I()
        await m.on_submit(i2)
        assert "Couldn't DM" in v.notice
    run(go())


def test_multiple_bots_show_picker_and_switch(vp):
    async def go():
        rows = [row(), row(clone_id=4)]
        v = server_view(vp, rows=rows)
        sel = selects(v)
        assert len(sel) == 1 and len(sel[0].options) == 2
        i = I(values=["4"])
        await sel[0].callback(i)
        assert v.current_row()["clone_id"] == 4
        assert "Clone #4" in texts(v)
    run(go())


# ── user inspector ───────────────────────────────────────────────────────

def test_user_card_content(vp):
    v = user_view(
        vp, blocked={"reason": "scam", "created_at": NOW},
        owned=[{"guild_id": GID, "clone_id": None, "guild_name": "Anime Hub"}],
        clones=[{"clone_id": 4, "bot_username": "clonebot", "status": "active"}],
        xp=[{"guild_id": GID, "clone_id": None, "total_xp": 12345, "level": 12, "guild_name": "Anime Hub"}],
        xp_servers=3, coins_total=5000, coins_servers=2,
        payments=[{"status": "completed", "n": 2, "total": 9.5}])
    text = texts(v)
    assert "somebody" in text and "🚫" in text and "scam" in text
    assert "Anime Hub" in text and "#4" in text and "level 12" in text and "3 server(s)" in text
    assert "5,000" in text and "completed 2" in text and "$9.50" in text


def test_user_card_empty_states(vp):
    text = texts(user_view(vp))
    assert "none recorded" in text and "Payments:** none" in text and "Blocked:** no" in text


def test_owner_cannot_be_blocked_from_inspector(vp, monkeypatch):
    async def go():
        add = AsyncMock()
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        v = vp.UserInspectView(cog(), OWNER, OWNER)
        v.card = user_card(user_id=OWNER)
        v._build()
        assert buttons(v)["Block"].disabled
        await v._block(I())      # even if forced
        add.assert_not_awaited()
    run(go())


def test_block_and_unblock_user(vp, monkeypatch):
    async def go():
        add, rem = AsyncMock(), AsyncMock(return_value=True)
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        monkeypatch.setattr(vp.ac, "remove_blacklist", rem)
        monkeypatch.setattr(vp.ai, "user_card", AsyncMock(return_value=user_card()))
        v = user_view(vp)
        await buttons(v)["Block"].callback(I())
        add.assert_awaited_once()
        assert add.await_args.args[:2] == ("user", UID)
        v2 = user_view(vp, blocked={"reason": "x", "created_at": NOW})
        await buttons(v2)["Unblock"].callback(I())
        rem.assert_awaited_once_with("user", UID)
    run(go())


def test_coins_modal_validates_and_applies(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ai, "user_card", AsyncMock(return_value=user_card()))
        adjust = AsyncMock(return_value=1500)
        vp.db.adjust_economy_balance = adjust
        v = user_view(vp)

        bad = vp.CoinsModal(v)
        bad.guild._value, bad.amount._value, bad.clone._value = str(GID), "abc", ""
        i = I()
        await bad.on_submit(i)
        adjust.assert_not_awaited()
        assert "whole number" in i.response.send_message.await_args.args[0]

        zero = vp.CoinsModal(v)
        zero.guild._value, zero.amount._value, zero.clone._value = str(GID), "0", ""
        await zero.on_submit(I())
        adjust.assert_not_awaited()

        good = vp.CoinsModal(v)
        good.guild._value, good.amount._value, good.clone._value = str(GID), "1,500", "4"
        i2 = I()
        await good.on_submit(i2)
        adjust.assert_awaited_once()
        assert adjust.await_args.args[:3] == (GID, UID, 1500)
        assert adjust.await_args.args[4] == 4
        assert "Gave 1,500" in v.notice

        take = vp.CoinsModal(v)
        take.guild._value, take.amount._value, take.clone._value = str(GID), "-200", ""
        await take.on_submit(I())
        assert "Took 200" in v.notice
    run(go())


def test_reset_xp_is_two_step_with_cancel(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ai, "user_card", AsyncMock(return_value=user_card()))
        reset = AsyncMock(return_value=True)
        monkeypatch.setattr(vp.ai, "reset_xp", reset)
        v = user_view(vp)
        i = I()
        await buttons(v)["Reset XP"].callback(i)
        modal = i.response.send_modal.await_args.args[0]
        assert isinstance(modal, vp.ResetXpModal)
        modal.guild._value, modal.clone._value = str(GID), ""
        await modal.on_submit(I())
        reset.assert_not_awaited()
        assert v.pending_reset == (GID, None)
        assert {"Confirm XP reset", "Cancel"} <= set(buttons(v))
        assert "Press Confirm" in texts(v)

        await buttons(v)["Cancel"].callback(I())
        assert v.pending_reset is None and reset.await_count == 0

        v.pending_reset = (GID, 4)
        v._build()
        await buttons(v)["Confirm XP reset"].callback(I())
        reset.assert_awaited_once_with(UID, GID, 4)
        assert v.pending_reset is None
    run(go())


def test_database_failure_shows_message_not_crash(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ai, "user_card", AsyncMock(side_effect=RuntimeError("db down")))
        v = vp.UserInspectView(cog(), OWNER, UID)
        await v.load()
        assert "Couldn't load" in texts(v)
        monkeypatch.setattr(vp.ai, "server_rows", AsyncMock(side_effect=RuntimeError("db down")))
        s = vp.ServerInspectView(cog(), OWNER, GID)
        await s.load()
        assert "Couldn't load" in texts(s)
    run(go())


def test_component_limits(vp):
    s = server_view(vp, rows=[row(), row(clone_id=4)])
    u = user_view(vp)
    u.pending_reset = (GID, None)
    u._build()
    for view in (s, u):
        assert count(view) < 40
        assert all(len(r.children) <= 5 for r in view.walk_children() if isinstance(r, discord.ui.ActionRow))


# ── pure helpers ─────────────────────────────────────────────────────────

def test_parsers(vp):
    ai = vp.ai
    assert ai.parse_snowflake("123456789012345678") == 123456789012345678
    assert ai.parse_snowflake("123") is None and ai.parse_snowflake("abc") is None
    assert ai.parse_clone("") == (True, None) and ai.parse_clone("7") == (True, 7) and ai.parse_clone("x")[0] is False
    assert ai.parse_coins("500") == 500 and ai.parse_coins("-20") == -20 and ai.parse_coins("+3") == 3
    assert ai.parse_coins("0") is None and ai.parse_coins("1.5") is None and ai.parse_coins("") is None
    assert ai.parse_coins(str(ai.MAX_COINS + 1)) is None
    assert ai.format_duration(90061) == "1d 1h" and ai.format_duration(3700) == "1h 1m" and ai.format_duration(30) == "0m"
    assert ai.age_text(None) == "never"
    assert ai.premium_active(NOW + timedelta(days=1)) and not ai.premium_active(NOW - timedelta(days=1))
    assert ai.premium_active(NOW - timedelta(days=1), grace_days=3)
