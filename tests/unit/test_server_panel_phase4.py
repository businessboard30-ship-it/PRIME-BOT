"""Server Owners Panel Phase 4 tests: owner read-only inspection, kill-switch
locking, panel usage tracking and the Health line."""
import importlib
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, MANAGER, MEMBER, buttons, guild, interaction, make_db, run, text_of,
)

BOT_OWNER = 111
TARGET_ID = 7777
CLONE = 7
SP = "modules.server_panel"
V1 = "discord_bot.cogs._views_server_panel"
V2 = "discord_bot.cogs._views_server_panel_p2"
V3 = "discord_bot.cogs._views_server_panel_p3"
MAIN = "discord_bot.cogs._views_admin_panel"
INSV = "discord_bot.cogs._views_admin_panel_inspect"
AC = "modules.admin_controls"
MODULES = (SP, V1, V2, V3, MAIN, INSV, AC, "modules.admin_inspect")
WRITE_PREFIXES = ("set_", "add_", "remove_", "delete_", "mark_", "update_", "create_", "clear_")


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.get_voice_xp_config = AsyncMock(return_value={"enabled": True, "xp_per_minute": 10, "afk_channel_excluded": True})
    db.get_starboard_config = AsyncMock(return_value={"channel_id": None, "threshold": 5, "emoji": "⭐"})
    db.get_suggestion_config = AsyncMock(return_value={"approved_log_channel_id": None})
    db.get_reaction_role_panels_for_guild = AsyncMock(return_value=[])
    db.get_level_roles = AsyncMock(return_value=[{"level": 5, "role_id": 77}])
    db.get_ticket_config = AsyncMock(return_value={
        "support_role_id": None, "category_id": None, "panel_channel_id": None, "welcome_message": None})
    db.get_leveling_config = AsyncMock(return_value={
        "announce_channel_id": None, "xp_rate": "default", "leaderboard_autopost_channel_id": None})
    db.count_active_members = AsyncMock(return_value=3)
    dbm.db = db
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS = 3
    cfg.PREMIUM_FEE_USD = 2
    cfg.DISCORD_CLONE_ADMIN_IDS = {BOT_OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {BOT_OWNER}
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    sc.SetupSuggestView = MagicMock()
    sc.build_suggestions_embed = MagicMock(return_value=discord.Embed(title="x"))
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    for name in MODULES:
        sys.modules.pop(name, None)
    sp = importlib.import_module(SP)
    v1, v2, v3 = (importlib.import_module(n) for n in (V1, V2, V3))
    ac = importlib.import_module(AC)
    insv = importlib.import_module(INSV)
    audit = AsyncMock()
    monkeypatch.setattr(sp, "record_change", audit)
    insv.audit = MagicMock()
    ac._snapshot.update(ts=0.0, ok=False, switches=set(), users=set(), guilds=set())
    yield SimpleNamespace(sp=sp, v1=v1, v2=v2, v3=v3, ac=ac, insv=insv, db=db, dbm=dbm, audit=audit)
    for name in MODULES:
        sys.modules.pop(name, None)


def engage(env, *keys):
    env.ac._snapshot.update(ts=time.monotonic(), ok=True, switches=set(keys))


def all_screens(env):
    v1, v2, v3 = env.v1, env.v2, env.v3
    return [v1.HomeView, v1.PremiumView, v1.SetupView, v1.WelcomeView, v1.VerificationView, v1.ModerationView,
            v2.CommunityView, v2.LevelingView, v2.LevelRolesView, v2.StarboardView, v2.SuggestionsView,
            v2.GiveawaysView, v2.ReactionRolesView, v2.TicketsView, v2.ChannelsLogsView,
            v3.StatsView, v3.HistoryView, v3.HelpToolsView, v3.ResetView, v3.CopyView]


def target_guild():
    g = guild()
    g.id, g.name = TARGET_ID, "Target Server"
    return g


def inspect_ctx(env, back=None):
    return env.v1.InspectContext(guild=target_guild(), back=back or AsyncMock())


def owner_click(env, ctx, custom_id=None):
    """The owner's real interaction (in some other place), answering as the target server."""
    real = interaction(user=BOT_OWNER, manage=False)
    real.guild_id = 1
    real.data = {"custom_id": custom_id}
    return real, env.v1.InspectInteraction(real, ctx)


def writes(db):
    return [c[0] for c in db.method_calls if c[0].startswith(WRITE_PREFIXES)]


# ── kill switches ────────────────────────────────────────────────────────

def test_current_switches_is_cached_and_fails_open(env):
    async def go():
        assert await env.ac.current_switches() == set()           # DB down, nothing known
        engage(env, "leveling", "economy")
        assert await env.ac.current_switches() == {"leveling", "economy"}
    run(go())


def test_leveling_switch_greys_hub_buttons_and_locks_screens(env):
    async def go():
        engage(env, "leveling")
        hub = await env.v2.CommunityView.create(interaction(user=MANAGER))
        b = buttons(hub)
        assert b["Leveling"].disabled and b["Level roles"].disabled
        assert not b["Starboard"].disabled and not b["Suggestions"].disabled
        assert "Leveling & cards" in text_of(hub) and "turned off by the bot owner" in text_of(hub)
        for cls in (env.v2.LevelingView, env.v2.LevelRolesView):
            v = await cls.create(interaction(user=MANAGER))
            labels = set(buttons(v))
            assert labels <= {"Back", "Level roles"}, (cls.__name__, labels)
            assert not [c for c in v.walk_children()
                        if isinstance(c, (discord.ui.Select, discord.ui.ChannelSelect, discord.ui.RoleSelect))]
            assert "read-only" in text_of(v)
    run(go())


def test_locked_screen_rejects_any_control_it_does_not_show(env):
    async def go():
        engage(env, "leveling")
        v = await env.v2.LevelingView.create(interaction(user=MANAGER))
        back_id = buttons(v)["Back"].custom_id
        ok = interaction(user=MANAGER); ok.data = {"custom_id": back_id}
        assert await v.interaction_check(ok) is True
        sneaky = interaction(user=MANAGER); sneaky.data = {"custom_id": "xp_rate_select"}
        assert await v.interaction_check(sneaky) is False
        assert "read-only" in sneaky.response.send_message.await_args.args[0]
    run(go())


def test_bot_owners_and_other_switches_do_not_lock_the_panel(env):
    async def go():
        engage(env, "leveling")
        v = await env.v2.CommunityView.create(interaction(user=BOT_OWNER))
        assert not buttons(v)["Leveling"].disabled
        engage(env, "economy", "ai")
        v = await env.v2.CommunityView.create(interaction(user=MANAGER))
        assert not buttons(v)["Leveling"].disabled and "turned off" not in text_of(v)
        lv = await env.v2.LevelingView.create(interaction(user=MANAGER))
        assert len(buttons(lv)) > 2
    run(go())


def test_unreadable_switches_fail_open(env, monkeypatch):
    async def go():
        monkeypatch.setattr(env.ac, "current_switches", AsyncMock(side_effect=RuntimeError("x")))
        v = await env.v2.CommunityView.create(interaction(user=MANAGER))
        assert not buttons(v)["Leveling"].disabled
    run(go())


# ── owner read-only inspection ───────────────────────────────────────────

def test_every_screen_is_read_only_in_inspect_mode(env):
    async def go():
        ctx = inspect_ctx(env)
        _, proxy = owner_click(env, ctx)
        for cls in all_screens(env):
            v = await cls.create(proxy)
            assert v.inspect is ctx, cls.__name__
            assert not [c for c in v.walk_children()
                        if isinstance(c, (discord.ui.Select, discord.ui.ChannelSelect, discord.ui.RoleSelect))], cls.__name__
            for lbl, btn in buttons(v).items():
                assert getattr(btn.callback, "read_only_ok", False), (cls.__name__, lbl)
            t = text_of(v)
            assert "Read-only view of **Target Server**" in t and "/serversetup" not in t, cls.__name__
            assert len(list(v.walk_children())) < env.v1.MAX_COMPONENTS, cls.__name__
    run(go())


def test_inspect_reads_the_target_server_and_changes_nothing(env):
    async def go():
        ctx = inspect_ctx(env)
        for cls in all_screens(env):
            _, proxy = owner_click(env, ctx)
            v = await cls.create(proxy)
            for lbl, btn in list(buttons(v).items()):
                real, _ = owner_click(env, ctx)
                await btn.callback(real)                  # click every control that is left
                real.response.send_modal.assert_not_awaited()
        # every settings read used the inspected server's id, never the owner's own place
        for name, mock in vars(env.db).items():
            if name.startswith("get_") and isinstance(mock, AsyncMock):
                for c in mock.await_args_list:
                    assert c.args[0] == TARGET_ID, (name, c.args)
        assert writes(env.db) == []
        env.audit.assert_not_awaited()
    run(go())


def test_inspect_navigation_and_refresh_keep_inspect_mode(env):
    async def go():
        ctx = inspect_ctx(env)
        _, proxy = owner_click(env, ctx)
        home = await env.v1.HomeView.create(proxy)
        real, _ = owner_click(env, ctx)
        await buttons(home)["Moderation"].callback(real)
        nxt = real.edit_original_response.await_args.kwargs["view"]
        assert isinstance(nxt, env.v1.ModerationView) and nxt.inspect is ctx and nxt.guild_id == TARGET_ID
        stats = await env.v3.StatsView.create(proxy)
        real, _ = owner_click(env, ctx)
        await buttons(stats)["Refresh"].callback(real)
        assert real.edit_original_response.await_args.kwargs["view"].inspect is ctx
    run(go())


def test_inspect_home_has_exit_and_no_write_entry_points(env):
    async def go():
        back = AsyncMock()
        ctx = inspect_ctx(env, back)
        _, proxy = owner_click(env, ctx)
        home = await env.v1.HomeView.create(proxy)
        b = buttons(home)
        assert "Back to owner panel" in b
        assert "Help & tools" not in b and "Quick enable" not in b
        real, _ = owner_click(env, ctx)
        await b["Back to owner panel"].callback(real)
        back.assert_awaited_once_with(real)
    run(go())


def test_inspect_access_rules(env):
    async def go():
        ctx = inspect_ctx(env)
        _, proxy = owner_click(env, ctx)
        v = await env.v3.StatsView.create(proxy)
        ok_id = buttons(v)["Back"].custom_id
        real, _ = owner_click(env, ctx, ok_id)
        assert await v.interaction_check(real) is True           # works with no Manage Server in the target
        bad, _ = owner_click(env, ctx, "not_on_screen")
        assert await v.interaction_check(bad) is False
        stranger, _ = owner_click(env, ctx, ok_id)
        stranger.user.id = MEMBER
        assert await v.interaction_check(stranger) is False      # not the opener
        # opener who is no longer a bot owner
        v2 = env.v3.StatsView(TARGET_ID, None, MEMBER, {}, inspect=ctx)
        gone, _ = owner_click(env, ctx, buttons(v2)["Back"].custom_id)
        gone.user.id = MEMBER
        assert await v2.interaction_check(gone) is False
        assert "no longer authorized" in gone.response.send_message.await_args.args[0]
    run(go())


def test_open_inspect_swaps_in_the_target_home_screen(env):
    async def go():
        real = interaction(user=BOT_OWNER, manage=False)
        back = AsyncMock()
        await env.v1.open_inspect(real, target_guild(), back)
        real.response.defer.assert_awaited_once()
        view = real.edit_original_response.await_args.kwargs["view"]
        assert isinstance(view, env.v1.HomeView) and view.inspect.back is back and view.guild_id == TARGET_ID
    run(go())


# ── owner Server inspector button ────────────────────────────────────────

def inspector(env, rows, in_main_bot=True):
    guild_obj = target_guild() if in_main_bot else None
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild_obj)
    cog = MagicMock(); cog.bot = bot
    v = env.insv.ServerInspectView(cog, BOT_OWNER, TARGET_ID)
    v.rows, v.extras = rows, {"blocked": None, "premium": [], "reports": {}}
    v._build()
    return v


def srow(clone_id=None):
    return {"guild_id": TARGET_ID, "clone_id": clone_id, "guild_name": "Anime Hub", "member_count": 9,
            "owner_id": 5, "joined_at": datetime.now(timezone.utc), "left_at": None, "bot_username": None}


def test_inspector_button_enabled_only_for_the_main_bot(env):
    v = inspector(env, [srow()])
    assert not buttons(v)["Open server panel"].disabled
    assert buttons(inspector(env, [srow(clone_id=4)]))["Open server panel"].disabled
    assert buttons(inspector(env, [srow()], in_main_bot=False))["Open server panel"].disabled
    assert buttons(inspector(env, []))["Open server panel"].disabled


def test_inspector_button_opens_panel_audits_and_can_return(env):
    async def go():
        v = inspector(env, [srow()])
        v.load = AsyncMock()
        i = interaction(user=BOT_OWNER, manage=False)
        await v._open_panel(i)
        env.insv.audit.assert_called_once()
        assert env.insv.audit.call_args.args[1] == "inspect.server_panel"
        view = i.edit_original_response.await_args.kwargs["view"]
        assert isinstance(view, env.v1.HomeView) and view.inspect is not None
        back_i = interaction(user=BOT_OWNER, manage=False)
        await view.inspect.back(back_i)
        v.load.assert_awaited_once()
        assert back_i.edit_original_response.await_args.kwargs["view"] is v
    run(go())


def test_inspector_button_refuses_clone_servers(env):
    async def go():
        v = inspector(env, [srow(clone_id=4)])
        i = interaction(user=BOT_OWNER, manage=False)
        await v._open_panel(i)
        i.response.send_message.assert_awaited_once()
        i.edit_original_response.assert_not_awaited()
        env.insv.audit.assert_not_called()
    run(go())


# ── usage tracking and Health ────────────────────────────────────────────

def fake_pool(fetchrow=None, fetchval=0, boom=False):
    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db") if boom else None)
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetchval = AsyncMock(return_value=fetchval)
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool, conn


def test_record_open_upserts_and_never_raises(env):
    async def go():
        pool, conn = fake_pool()
        env.dbm.get_pool = AsyncMock(return_value=pool)
        await env.sp.record_open(GUILD_ID, CLONE)
        sql, g, c = conn.execute.await_args.args
        assert "server_panel_usage" in sql and "opens + 1" in sql and (g, c) == (GUILD_ID, CLONE)
        pool, conn = fake_pool(boom=True)
        env.dbm.get_pool = AsyncMock(return_value=pool)
        await env.sp.record_open(GUILD_ID, None)                 # swallowed
        env.dbm.get_pool = AsyncMock(side_effect=RuntimeError("down"))
        await env.sp.record_open(GUILD_ID, None)
    run(go())


def test_open_home_records_the_open(env, monkeypatch):
    async def go():
        rec = AsyncMock()
        monkeypatch.setattr(env.sp, "record_open", rec)
        await env.v1.open_home(interaction(clone_id=CLONE))
        rec.assert_awaited_once_with(GUILD_ID, CLONE)
    run(go())


def test_usage_stats_values_and_failure(env):
    async def go():
        pool, conn = fake_pool(fetchrow={"opened": 12, "week": 5}, fetchval=4)
        env.dbm.get_pool = AsyncMock(return_value=pool)
        assert await env.sp.usage_stats() == {"opened": 12, "week": 5, "changed": 4}
        assert "report.%" in conn.fetchval.await_args.args[0]    # reports are not "changes"
        env.dbm.get_pool = AsyncMock(side_effect=RuntimeError("down"))
        assert await env.sp.usage_stats() is None
    run(go())


def health_view(env, monkeypatch, usage):
    ai = importlib.import_module("modules.admin_inspect")
    monkeypatch.setattr(ai, "db_ping", AsyncMock(return_value=5.0))
    monkeypatch.setattr(ai, "clone_heartbeats", AsyncMock(return_value=[]))
    monkeypatch.setattr(ai, "loop_report", lambda bot: (3, []))
    monkeypatch.setattr(ai, "error_counts", lambda *a, **k: (0, 0))
    monkeypatch.setattr(env.sp, "usage_stats", AsyncMock(return_value=usage))
    bot = MagicMock(); bot.latency = 0.05; bot.guilds = []; bot.cogs = {}
    cog = MagicMock(); cog.bot = bot
    return env.insv.HealthView(cog, BOT_OWNER)


def test_health_shows_panel_usage_or_that_it_could_not_be_checked(env, monkeypatch):
    async def go():
        v = health_view(env, monkeypatch, {"opened": 12, "week": 5, "changed": 4})
        await v.load()
        assert "12 server(s) opened it (5 in the last 7 days) · 4 changed a setting" in text_of(v)
        v = health_view(env, monkeypatch, None)
        await v.load()
        assert "Server panel:** couldn't be checked" in text_of(v)
    run(go())


# ── schema ───────────────────────────────────────────────────────────────

def test_migration_is_wired_and_schema_version_bumped():
    root = Path(__file__).resolve().parents[2]
    src = (root / "database.py").read_text()
    assert 'SCHEMA_VERSION = "43"' in src
    assert "025_server_panel_usage.sql" in src
    sql = (root / "database" / "migrations" / "025_server_panel_usage.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS server_panel_usage" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql and "COALESCE(clone_id, -1)" in sql
