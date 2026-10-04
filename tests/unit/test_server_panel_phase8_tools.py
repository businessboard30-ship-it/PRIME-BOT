"""Server Owners Panel Phase 8: Tools hub (invites, scheduled messages, link buttons) and voice XP rate."""
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, MEMBER, OWNER, buttons, interaction, make_db, run, text_of,
)

SP = "modules.server_panel"
SPT = "modules.server_panel_tools"
V1 = "discord_bot.cogs._views_server_panel"
V2 = "discord_bot.cogs._views_server_panel_p2"
V4 = "discord_bot.cogs._views_server_panel_p4"
NAMES = (SP, SPT, V1, V2, V4)
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.get_invite_tracker_config = AsyncMock(return_value={
        "enabled": True, "channel_id": 61, "leaderboard_autopost_channel_id": None})
    db.set_invite_tracker_config = AsyncMock()
    db.set_invite_leaderboard_autopost_channel = AsyncMock()
    db.get_invite_leaderboard = AsyncMock(return_value=[(901, 9, 7), (902, 4, 4)])
    db.list_scheduled_messages = AsyncMock(return_value=[
        {"id": 3, "channel_id": 62, "content": "Daily standup reminder", "enabled": True,
         "interval_seconds": 86400, "next_run_at": NOW + timedelta(hours=3)}])
    db.create_scheduled_message = AsyncMock(side_effect=lambda g, ch, text, run_at, iv, by, clone_id=None: {
        "id": 8, "next_run_at": run_at})
    db.delete_scheduled_message = AsyncMock(return_value=True)
    db.list_link_buttons = AsyncMock(return_value=[{"label": "Website", "url": "https://example.com"}])
    db.add_link_button = AsyncMock(return_value=True)
    db.remove_link_button = AsyncMock(return_value=True)
    db.get_voice_xp_config = AsyncMock(return_value={"enabled": True, "xp_per_minute": 10, "afk_channel_excluded": True})
    db.set_voice_xp_config = AsyncMock()
    db.get_level_roles = AsyncMock(return_value=[])
    dbm.db = db
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no db"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS = 3
    cfg.PREMIUM_FEE_USD = 2
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    for n in NAMES:
        sys.modules.pop(n, None)
    sp = importlib.import_module(SP)
    spt = importlib.import_module(SPT)
    v1 = importlib.import_module(V1)
    v2 = importlib.import_module(V2)
    v4 = importlib.import_module(V4)
    audit = AsyncMock()
    monkeypatch.setattr(sp, "record_change", audit)
    monkeypatch.setattr(spt, "record_change", audit)
    yield SimpleNamespace(sp=sp, spt=spt, v1=v1, v2=v2, v4=v4, db=db, audit=audit)
    for n in NAMES:
        sys.modules.pop(n, None)


def ok_channel(monkeypatch, problem=None):
    from discord_bot import perm_check
    monkeypatch.setattr(perm_check, "channel_problem", lambda *a, **k: problem)


# ── parsing ──────────────────────────────────────────────────────────────

def test_plan_schedule_kinds(env):
    now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    run_at, iv, err = env.spt.plan_schedule("once", "2h", now)
    assert run_at == now + timedelta(hours=2) and iv is None and err is None
    run_at, iv, err = env.spt.plan_schedule("recurring", "30m", now)
    assert iv == 1800 and err is None
    run_at, iv, err = env.spt.plan_schedule("daily", "09:00", now)
    assert run_at == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc) and iv == 86400
    run_at, iv, err = env.spt.plan_schedule("daily", "13:30", now)
    assert run_at == datetime(2026, 10, 4, 13, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("kind,when", [("once", "soon"), ("recurring", "30s"), ("daily", "25:00"), ("daily", "9am"), ("bogus", "1h")])
def test_plan_schedule_rejects_bad_input(env, kind, when):
    run_at, iv, err = env.spt.plan_schedule(kind, when)
    assert run_at is None and err


def test_link_and_rate_validation(env):
    assert env.spt.validate_link("Site", "https://x.io") is None
    assert env.spt.validate_link("Site", "ftp://x.io")
    assert env.spt.validate_link("", "https://x.io")
    assert env.spt.validate_link("a" * 81, "https://x.io")
    assert env.spt.validate_voice_rate("25") == (25, None)
    for bad in ("0", "26", "abc", ""):
        assert env.spt.validate_voice_rate(bad)[0] is None


# ── hub and screens ──────────────────────────────────────────────────────

def test_home_has_tools_button_and_limits(env):
    v = run(env.v1.HomeView.create(interaction()))
    assert "Tools" in buttons(v)
    assert len(list(v.walk_children())) < 40


def test_tools_hub_summarises_everything(env):
    v = run(env.v4.ToolsView.create(interaction()))
    t = text_of(v)
    assert "<#61>" in t and "1" in t and "1/5" in t
    assert {"Invites", "Scheduled messages", "Link buttons", "Self-roles wizard", "Back"} <= set(buttons(v))


@pytest.mark.parametrize("cls", ["ToolsView", "InvitesView", "ScheduleView", "LinkButtonsView"])
def test_screens_respect_component_limit_and_hint(env, cls):
    v = run(getattr(env.v4, cls).create(interaction()))
    assert len(list(v.walk_children())) < 40
    assert "/serversetup" in text_of(v)


def test_invites_screen_shows_top_inviters(env):
    t = text_of(run(env.v4.InvitesView.create(interaction())))
    assert "<@901>" in t and "<@902>" in t and "<#61>" in t


def test_invites_toggle_writes_with_clone_and_audit(env):
    i = interaction(clone_id=7)
    v = run(env.v4.InvitesView.create(i))
    run(buttons(v)["Turn off"].callback(i))
    _, kw = env.db.set_invite_tracker_config.await_args
    assert kw["clone_id"] == 7 and kw["enabled"] is False
    assert env.audit.await_args.args[3] == "invites.enabled"


def test_leaderboard_off_and_default(env):
    i = interaction(clone_id=7)
    v = run(env.v4.InvitesView.create(i))
    run(buttons(v)["Leaderboard off"].callback(i))
    assert env.db.set_invite_tracker_config.await_count == 0
    assert env.db.set_invite_leaderboard_autopost_channel.await_args.args == (GUILD_ID, 7, -1)


def test_invites_db_failure_does_not_break_screen(env):
    env.db.get_invite_tracker_config = AsyncMock(side_effect=RuntimeError("down"))
    env.db.get_invite_leaderboard = AsyncMock(side_effect=RuntimeError("down"))
    t = text_of(run(env.v4.InvitesView.create(interaction())))
    assert "Invite tracker" in t


# ── scheduled messages ───────────────────────────────────────────────────

def test_schedule_lists_jobs_and_needs_channel_first(env):
    i = interaction()
    v = run(env.v4.ScheduleView.create(i))
    assert "#3" in text_of(v) and "daily" in text_of(v)
    run(buttons(v)["Post once"].callback(i))
    i.response.send_message.assert_awaited()
    i.response.send_modal.assert_not_awaited()


def test_schedule_add_creates_job_with_clone_and_audit(env):
    i = interaction(clone_id=7)
    job, err = run(env.spt.add_schedule(GUILD_ID, 7, OWNER, 62, "recurring", "1h", "hello"))
    assert err is None and job["id"] == 8
    args, kw = env.db.create_scheduled_message.await_args
    assert args[:2] == (GUILD_ID, 62) and args[4] == 3600 and kw["clone_id"] == 7
    assert env.audit.await_args.args[3] == "schedule.add"


def test_schedule_add_rejects_bad_empty_and_over_cap(env):
    assert run(env.spt.add_schedule(GUILD_ID, None, OWNER, 62, "once", "never", "x"))[1]
    assert run(env.spt.add_schedule(GUILD_ID, None, OWNER, 62, "once", "1h", "   "))[1]
    assert run(env.spt.add_schedule(GUILD_ID, None, OWNER, 62, "once", "1h", "x" * 2001))[1]
    env.db.list_scheduled_messages = AsyncMock(return_value=[{"id": n} for n in range(25)])
    assert "25" in run(env.spt.add_schedule(GUILD_ID, None, OWNER, 62, "once", "1h", "x"))[1]
    env.db.create_scheduled_message.assert_not_awaited()


def test_schedule_modal_submits_and_reloads(env, monkeypatch):
    ok_channel(monkeypatch)
    i = interaction(clone_id=7)
    modal = env.v4.ScheduleModal("once", 62, OWNER)
    modal.when._value, modal.text._value = "2h", "Event starts"
    run(modal.on_submit(i))
    env.db.create_scheduled_message.assert_awaited_once()
    i.edit_original_response.assert_awaited()


def test_schedule_modal_denies_non_manager_and_other_user(env, monkeypatch):
    ok_channel(monkeypatch)
    for i, opener in ((interaction(user=MEMBER, manage=False), MEMBER), (interaction(user=OWNER), MEMBER)):
        modal = env.v4.ScheduleModal("once", 62, opener)
        modal.when._value, modal.text._value = "2h", "x"
        run(modal.on_submit(i))
    env.db.create_scheduled_message.assert_not_awaited()


def test_schedule_cancel_is_two_step(env):
    i = interaction(clone_id=7)
    v = run(env.v4.ScheduleView.create(i))
    sel = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select))
    sel._values = ["3"]
    run(sel.callback(i))
    env.db.delete_scheduled_message.assert_not_awaited()
    confirm = i.edit_original_response.await_args.kwargs["view"]
    run(buttons(confirm)["Yes, do it"].callback(i))
    env.db.delete_scheduled_message.assert_awaited_once_with(GUILD_ID, 3, clone_id=7)
    assert env.audit.await_args.args[3] == "schedule.cancel"


def test_cancel_confirm_can_be_dismissed(env):
    i = interaction()
    v = run(env.v4.ScheduleView.create(i))
    sel = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select))
    sel._values = ["3"]
    run(sel.callback(i))
    confirm = i.edit_original_response.await_args.kwargs["view"]
    run(buttons(confirm)["Cancel"].callback(i))
    env.db.delete_scheduled_message.assert_not_awaited()


def test_only_opener_can_click_tools(env):
    v = run(env.v4.ToolsView.create(interaction()))
    other = interaction(user=MEMBER, manage=True)
    assert run(v.interaction_check(other)) is False


# ── link buttons ─────────────────────────────────────────────────────────

def test_link_add_validates_caps_and_audits(env):
    assert env.spt.validate_link("x", "javascript:1")
    assert run(env.spt.add_link(GUILD_ID, 7, OWNER, "Docs", "https://docs.io")) is None
    env.db.add_link_button.assert_awaited_once_with(GUILD_ID, "Docs", "https://docs.io", OWNER)
    assert env.audit.await_args.args[3] == "link_button.Docs"
    env.db.list_link_buttons = AsyncMock(return_value=[{"label": str(n), "url": "https://x"} for n in range(5)])
    assert "5" in run(env.spt.add_link(GUILD_ID, 7, OWNER, "Six", "https://six.io"))
    # updating an existing label at the cap is still allowed
    assert run(env.spt.add_link(GUILD_ID, 7, OWNER, "3", "https://new.io")) is None


def test_link_add_button_disabled_at_cap(env):
    env.db.list_link_buttons = AsyncMock(return_value=[{"label": str(n), "url": "https://x"} for n in range(5)])
    v = run(env.v4.LinkButtonsView.create(interaction()))
    assert buttons(v)["Add button"].disabled


def test_link_remove_is_two_step(env):
    i = interaction(clone_id=7)
    v = run(env.v4.LinkButtonsView.create(i))
    sel = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select))
    sel._values = ["0"]
    run(sel.callback(i))
    env.db.remove_link_button.assert_not_awaited()
    confirm = i.edit_original_response.await_args.kwargs["view"]
    run(buttons(confirm)["Yes, do it"].callback(i))
    env.db.remove_link_button.assert_awaited_once_with(GUILD_ID, "Website")


def test_link_modal_rejects_bad_url(env):
    i = interaction()
    modal = env.v4.LinkButtonModal(OWNER)
    modal.label._value, modal.url._value = "Site", "not a link"
    run(modal.on_submit(i))
    env.db.add_link_button.assert_not_awaited()
    assert "❌" in i.followup.send.await_args.args[0]


# ── voice xp rate ────────────────────────────────────────────────────────

def test_leveling_shows_rate_and_button(env):
    v = run(env.v2.LevelingView.create(interaction()))
    assert "10 XP/min" in text_of(v) and "Voice XP rate" in buttons(v)
    assert len(list(v.walk_children())) < 40


def test_voice_rate_modal_writes_with_clone_and_audit(env):
    i = interaction(clone_id=7)
    modal = env.v2.VoiceRateModal(10, OWNER)
    modal.rate._value = "15"
    run(modal.on_submit(i))
    _, kw = env.db.set_voice_xp_config.await_args
    assert kw["clone_id"] == 7 and kw["xp_per_minute"] == 15
    assert env.audit.await_args.args[3] == "voice_xp.xp_per_minute"


@pytest.mark.parametrize("bad", ["0", "26", "x"])
def test_voice_rate_modal_rejects_out_of_range(env, bad):
    i = interaction()
    modal = env.v2.VoiceRateModal(10, OWNER)
    modal.rate._value = bad
    run(modal.on_submit(i))
    env.db.set_voice_xp_config.assert_not_awaited()
    i.response.send_message.assert_awaited()
