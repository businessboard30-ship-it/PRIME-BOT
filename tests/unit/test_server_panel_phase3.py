"""Server Owners Panel Phase 3 tests: stats, history, reset, copy settings,
reports. Covers access, component limits, clone scoping, two-step actions,
click-time re-verification and failure paths."""
import importlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, MANAGER, MEMBER, OWNER, buttons, interaction, make_db, run, text_of,
)

SP = "modules.server_panel"
V1 = "discord_bot.cogs._views_server_panel"
V3 = "discord_bot.cogs._views_server_panel_p3"
CLONE = 7
SRC_ID = 9001
TXT = discord.ChannelType.text


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.count_active_members = AsyncMock(return_value=12)
    db.get_voice_xp_config = AsyncMock(return_value={"enabled": False, "xp_per_minute": 20, "afk_channel_excluded": False})
    db.set_voice_xp_config = AsyncMock()
    db.get_starboard_config = AsyncMock(return_value={"channel_id": 31, "threshold": 3, "emoji": "🌟"})
    db.set_starboard_config = AsyncMock()
    db.get_suggestion_config = AsyncMock(return_value={"approved_log_channel_id": None})
    db.set_suggestion_config = AsyncMock()
    db.get_level_roles = AsyncMock(return_value=[{"level": 5, "role_id": 41}, {"level": 9, "role_id": 42}])
    db.add_level_role = AsyncMock(return_value=True)
    db.set_ticket_config = AsyncMock()
    db.get_ticket_config = AsyncMock(return_value={"support_role_id": 1, "category_id": 2, "panel_channel_id": 3})
    db.get_leveling_config = AsyncMock(return_value={
        "announce_channel_id": 30, "xp_rate": "fast", "card_style": "card", "leaderboard_autopost_channel_id": 99})
    db.set_leveling_config = AsyncMock()
    db.delete_honeypot_config = AsyncMock()
    db.add_discord_user_feedback = AsyncMock()
    db.get_welcome_config = AsyncMock(return_value={
        "enabled": True, "channel_id": 30, "message_template": "Hi {member}", "card_style": "gif",
        "avatar_shape": "circle", "delivery_mode": "channel", "card_theme": "wolf"})
    db.get_automod_config = AsyncMock(return_value={
        "action": "warn", "timeout_minutes": 5, "log_channel_id": 30, "banned_words": ["x"],
        "word_filter_enabled": True, "anti_invite_enabled": True, "anti_mention_enabled": False,
        "spam_enabled": False, "anti_mention_threshold": 5, "spam_flood_threshold": 10,
        "spam_flood_window_seconds": 10, "min_account_age_hours": 0, "log_members_enabled": True})
    dbm.db = db
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS = 3
    cfg.PREMIUM_FEE_USD = 2
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    for name in (SP, V1, V3):
        sys.modules.pop(name, None)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    sp = importlib.import_module(SP)
    v1 = importlib.import_module(V1)
    v3 = importlib.import_module(V3)
    audit = AsyncMock()
    monkeypatch.setattr(sp, "record_change", audit)
    yield SimpleNamespace(sp=sp, v1=v1, v3=v3, db=db, audit=audit)
    for name in (SP, V1, V3):
        sys.modules.pop(name, None)


def chan(cid, name, kind=TXT):
    return SimpleNamespace(id=cid, name=name, type=kind)


def role(rid, name):
    return SimpleNamespace(id=rid, name=name, managed=False, is_default=lambda: False)


def fguild(gid, name, owner=OWNER, channels=(), roles=(), members=None):
    g = SimpleNamespace(id=gid, name=name, owner_id=owner, channels=list(channels), roles=list(roles),
                        member_count=42, created_at=datetime(2024, 1, 1, tzinfo=timezone.utc))
    g.get_channel = lambda cid: next((c for c in g.channels if c.id == cid), None)
    g.get_role = lambda rid: next((r for r in g.roles if r.id == rid), None)
    g.get_member = lambda uid: (members or {}).get(uid)
    g.fetch_member = AsyncMock(side_effect=RuntimeError("not a member"))
    return g


def screens(v3):
    return [v3.StatsView, v3.HistoryView, v3.HelpToolsView, v3.ResetView, v3.CopyView]


# ── shared rules ─────────────────────────────────────────────────────────

def test_component_and_row_limits(env):
    async def go():
        for cls in screens(env.v3):
            v = await cls.create(interaction())
            assert len(list(v.walk_children())) < env.v1.MAX_COMPONENTS, cls.__name__
            for row in (c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)):
                assert len(row.children) <= 5, cls.__name__
            for s in (c for c in v.walk_children() if type(c) is discord.ui.Select):
                assert len(s.options) <= 25, cls.__name__
    run(go())


def test_every_screen_has_hint_and_back(env):
    async def go():
        for cls in screens(env.v3):
            v = await cls.create(interaction())
            assert "/serversetup" in text_of(v), cls.__name__
            assert "Back" in buttons(v), cls.__name__
    run(go())


def test_access_revoked_and_foreign_clicks_blocked(env):
    async def go():
        for cls in screens(env.v3):
            v = await cls.create(interaction(user=MANAGER))
            assert await v.interaction_check(interaction(user=MANAGER)) is True
            assert await v.interaction_check(interaction(user=MANAGER, manage=False)) is False, cls.__name__
            other = interaction(user=OWNER)
            assert await v.interaction_check(other) is False, cls.__name__   # not the opener
    run(go())


def test_home_links_to_phase3(env):
    async def go():
        v = await env.v1.HomeView.create(interaction())
        b = buttons(v)
        for label in ("Stats", "Change history", "Help & tools"):
            assert label in b
        assert len(list(v.walk_children())) < env.v1.MAX_COMPONENTS
    run(go())


# ── stats ────────────────────────────────────────────────────────────────

def test_stats_show_numbers_and_survive_a_failed_read(env):
    async def go():
        g = fguild(GUILD_ID, "Test", channels=[chan(1, "a"), chan(2, "v", discord.ChannelType.voice)],
                   roles=[role(1, "@everyone"), role(2, "mod")])
        v = await env.v3.StatsView.create(interaction(g=g))
        t = text_of(v)
        assert "**42**" in t and "**12**" in t and "Text channels: 1" in t and "Roles: 1" in t
        env.db.count_active_members.side_effect = RuntimeError("db down")
        v = await env.v3.StatsView.create(interaction(g=g))
        assert "Active (7d): N/A" in text_of(v)
    run(go())


# ── history ──────────────────────────────────────────────────────────────

def test_history_lists_rows_and_handles_errors(env, monkeypatch):
    async def go():
        row = {"actor_id": OWNER, "setting_key": "welcome.enabled", "old_value": "False",
               "new_value": "True", "created_at": datetime.now(timezone.utc)}
        monkeypatch.setattr(env.sp, "recent_changes", AsyncMock(return_value=[row]))
        v = await env.v3.HistoryView.create(interaction(clone_id=CLONE))
        t = text_of(v)
        assert "welcome.enabled" in t and f"<@{OWNER}>" in t and "False → True" in t
        env.sp.recent_changes.assert_awaited_once_with(GUILD_ID, CLONE)
        monkeypatch.setattr(env.sp, "recent_changes", AsyncMock(return_value=[]))
        assert "No changes" in text_of(await env.v3.HistoryView.create(interaction()))
        monkeypatch.setattr(env.sp, "recent_changes", AsyncMock(side_effect=RuntimeError("x")))
        assert "unavailable" in text_of(await env.v3.HistoryView.create(interaction()))
    run(go())


# ── reset (two-step) ─────────────────────────────────────────────────────

def test_reset_first_step_changes_nothing_then_confirm_resets(env):
    async def go():
        v = await env.v3.ResetView.create(interaction(clone_id=CLONE))
        assert "Confirm reset" not in buttons(v)
        await v._pick(interaction(clone_id=CLONE), "starboard")
        assert "Confirm reset" in buttons(v) and "Cancel" in buttons(v)
        env.db.set_starboard_config.assert_not_awaited()
        await v._confirm(interaction(clone_id=CLONE))
        env.db.set_starboard_config.assert_awaited_once_with(
            GUILD_ID, clone_id=CLONE, channel_id=None, threshold=5, emoji="⭐")
        assert any(c.args[3] == "reset.starboard" for c in env.audit.await_args_list)
        assert "back to defaults" in text_of(v) and v.pending_key is None
    run(go())


def test_reset_cancel_and_leveling_keeps_level_roles(env):
    async def go():
        v = await env.v3.ResetView.create(interaction())
        await v._pick(interaction(), "leveling")
        assert "Level-role rewards are kept" in text_of(v)
        await v._cancel(interaction())
        assert v.pending_key is None
        await v._pick(interaction(), "leveling")
        await v._confirm(interaction(clone_id=CLONE))
        assert env.db.set_leveling_config.await_args.kwargs["xp_rate"] == "default"
        assert env.db.set_voice_xp_config.await_args.kwargs["xp_per_minute"] == 10
        env.db.get_level_roles.assert_not_awaited()
    run(go())


def test_reset_covers_every_group_and_honeypot_deletes(env):
    async def go():
        for key in env.sp.RESET_GROUPS:
            await env.sp.reset_feature(GUILD_ID, CLONE, OWNER, key)
        env.db.delete_honeypot_config.assert_awaited_once_with(GUILD_ID, clone_id=CLONE)
        env.db.set_welcome_config.assert_awaited()
        with pytest.raises(ValueError):
            await env.sp.reset_feature(GUILD_ID, CLONE, OWNER, "economy")
    run(go())


def test_reset_failure_is_reported_and_clears_pending(env):
    async def go():
        env.db.set_starboard_config.side_effect = RuntimeError("write failed")
        v = await env.v3.ResetView.create(interaction())
        await v._pick(interaction(), "starboard")
        await v._confirm(interaction())
        assert "Couldn't reset" in text_of(v) and v.pending_key is None
    run(go())


# ── copy settings ────────────────────────────────────────────────────────

def make_pair(src_owner=OWNER):
    src = fguild(SRC_ID, "Source", owner=src_owner,
                 channels=[chan(30, "general"), chan(31, "stars"), chan(99, "board")],
                 roles=[role(41, "Regular"), role(42, "Veteran")])
    dst = fguild(GUILD_ID, "Target", channels=[chan(130, "general"), chan(131, "stars")],
                 roles=[role(141, "Regular")])
    return src, dst


def copy_interaction(src, dst, user=OWNER, clone_id=CLONE, manage=True):
    i = interaction(user=user, clone_id=clone_id, g=dst, manage=manage)
    i.client.guilds = [src, dst]
    i.client.get_guild = lambda gid: {src.id: src, dst.id: dst}.get(gid)
    return i


def test_copy_lists_only_other_servers_the_user_manages(env):
    async def go():
        src, dst = make_pair()
        stranger = fguild(7777, "Not mine", owner=999)
        i = copy_interaction(src, dst)
        i.client.guilds = [src, dst, stranger]
        v = await env.v3.CopyView.create(i)
        assert env.sp.copyable_guilds(i.client, dst, OWNER) == [(SRC_ID, "Source")]
        assert "Confirm copy" not in buttons(v)
    run(go())


def test_copy_maps_by_name_and_never_copies_ids(env):
    async def go():
        src, dst = make_pair()
        i = copy_interaction(src, dst)
        v = await env.v3.CopyView.create(i)
        await v._pick(copy_interaction(src, dst), SRC_ID)
        env.db.set_automod_config.assert_not_awaited()          # picking changes nothing
        await v._confirm(copy_interaction(src, dst))
        # every write targets the destination guild with the clone id
        for m in (env.db.set_automod_config, env.db.set_welcome_config, env.db.set_leveling_config,
                  env.db.set_starboard_config, env.db.set_voice_xp_config):
            assert m.await_count >= 1
            for c in m.await_args_list:
                assert c.args[0] == GUILD_ID and c.kwargs["clone_id"] == CLONE
        def own(call):
            return {k: v for k, v in call.kwargs.items() if k != "clone_id"}
        assert own(env.db.set_welcome_config.await_args_list[-1]) == {"channel_id": 130, "enabled": True}
        assert own(env.db.set_starboard_config.await_args) == {"channel_id": 131}
        assert {c.kwargs.get("log_channel_id") for c in env.db.set_automod_config.await_args_list} >= {130}
        # source ids never reach the destination
        for m in (env.db.set_welcome_config, env.db.set_automod_config, env.db.set_leveling_config):
            for c in m.await_args_list:
                assert 30 not in c.kwargs.values() and 99 not in c.kwargs.values()
        env.db.add_level_role.assert_awaited_once_with(GUILD_ID, 5, 141, clone_id=CLONE)
        t = text_of(v)
        assert "Level 9 reward" in t and "Leaderboard channel" in t   # unmatched items listed for re-pick
    run(go())


def test_copy_reverifies_both_servers_at_click_time(env):
    async def go():
        src, dst = make_pair()
        v = await env.v3.CopyView.create(copy_interaction(src, dst))
        await v._pick(copy_interaction(src, dst), SRC_ID)
        src.owner_id = 999                                    # ownership of the source lost since the pick
        await v._confirm(copy_interaction(src, dst))
        assert "nothing was copied" in text_of(v)
        env.db.set_automod_config.assert_not_awaited()
        # destination side: user no longer allowed there
        src2, dst2 = make_pair()
        v = await env.v3.CopyView.create(copy_interaction(src2, dst2))
        await v._pick(copy_interaction(src2, dst2), SRC_ID)
        dst2.owner_id = 999
        await v._confirm(copy_interaction(src2, dst2, manage=False))
        assert "nothing was copied" in text_of(v)
        env.db.set_automod_config.assert_not_awaited()
    run(go())


def test_copy_one_failing_feature_does_not_stop_the_rest(env):
    async def go():
        src, dst = make_pair()
        env.db.get_welcome_config.side_effect = RuntimeError("boom")
        rep = await env.sp.copy_settings(src, dst, CLONE, OWNER)
        assert "Auto-mod filters" in rep.copied and "Starboard" in rep.copied
        assert any(r.startswith("welcome: copy failed") for r in rep.repick)
    run(go())


def test_map_helpers_match_by_name_only(env):
    src, dst = make_pair()
    assert env.sp.map_channel(src, dst, 30) == 130
    assert env.sp.map_channel(src, dst, 99) is None
    assert env.sp.map_channel(src, dst, None) is None
    assert env.sp.map_role(src, dst, 41) == 141
    assert env.sp.map_role(src, dst, 42) is None


# ── report / help ────────────────────────────────────────────────────────

def test_report_goes_through_feedback_cog_with_tag(env):
    async def go():
        i = interaction(clone_id=CLONE)
        cog = SimpleNamespace(ai_feedback=AsyncMock())
        i.client.get_cog = MagicMock(return_value=cog)
        m = env.v3.ReportModal("problem", OWNER)
        m.text._value = "Welcome card is blank"
        await m.on_submit(i)
        sent = cog.ai_feedback.await_args.args
        assert sent[0] is i and sent[1] == "[Server panel · Problem report] Welcome card is blank"
        assert any(c.args[3] == "report.problem" for c in env.audit.await_args_list)
    run(go())


def test_report_fallback_without_cog_and_foreign_submit_rejected(env):
    async def go():
        i = interaction()
        i.client.get_cog = MagicMock(return_value=None)
        m = env.v3.ReportModal("idea", OWNER)
        m.text._value = "More card themes"
        await m.on_submit(i)
        env.db.add_discord_user_feedback.assert_awaited_once()
        assert env.db.add_discord_user_feedback.await_args.args[2].startswith("[Server panel · Feature idea]")
        env.db.add_discord_user_feedback.reset_mock()
        stranger = interaction(user=MEMBER, manage=False)
        m2 = env.v3.ReportModal("idea", OWNER)
        m2.text._value = "hi"
        await m2.on_submit(stranger)
        env.db.add_discord_user_feedback.assert_not_awaited()
    run(go())


def test_report_text_limit_leaves_room_for_the_tag(env):
    tag = env.sp.format_report("problem", "")
    assert len(tag) + env.sp.REPORT_MAX <= 1000   # feedback.MAX_FEEDBACK_LENGTH
