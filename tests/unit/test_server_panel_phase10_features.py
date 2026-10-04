"""Server Owners Panel Phase 10: Features hub (bump, custom role, autopost), permission check, Fix next."""
import importlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, MEMBER, OWNER, buttons, guild, interaction, make_db, run, text_of,
)

SP, SPF = "modules.server_panel", "modules.server_panel_features"
V1, V2, V6 = (f"discord_bot.cogs._views_server_panel{x}" for x in ("", "_p2", "_p6"))
NAMES = (SP, SPF, V1, V2, V6)

BUMP_CFG = {"bump_channel_id": 71, "language": "any", "nsfw_opt_in": False, "intensity_level": 3,
            "receives_bumps": True, "channel_recreate_declined": False}
FULL_PERMS = SimpleNamespace(kick_members=True, ban_members=True, moderate_members=True, manage_roles=True,
                             manage_guild=True, manage_messages=True, manage_channels=True)


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.bump_get_guild_config = AsyncMock(return_value=dict(BUMP_CFG))
    db.bump_set_guild_config = AsyncMock()
    db.is_custom_role_feature_disabled = AsyncMock(return_value=False)
    db.set_custom_role_feature_disabled = AsyncMock()
    db.get_custom_role_panel = AsyncMock(return_value={"panel_channel_id": 72, "panel_message_id": 1})
    db.get_discord_autopost = AsyncMock(return_value={
        "enabled": False, "channel_id": None, "interval_hours": 24, "last_posted_at": None})
    db.set_discord_autopost = AsyncMock()
    db.disable_discord_autopost = AsyncMock(return_value=True)
    db.list_discord_autopost_content = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    db.get_invite_tracker_config = AsyncMock(return_value={"enabled": True, "channel_id": 61})
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
    mods = dict(sp=importlib.import_module(SP), spf=importlib.import_module(SPF),
                v1=importlib.import_module(V1), v2=importlib.import_module(V2), v6=importlib.import_module(V6))
    audit = AsyncMock()
    monkeypatch.setattr(mods["sp"], "record_change", audit)
    monkeypatch.setattr(mods["spf"], "record_change", audit)
    yield SimpleNamespace(db=db, audit=audit, **mods)
    for n in NAMES:
        sys.modules.pop(n, None)


def ok_channel(monkeypatch, problem=None):
    from discord_bot import perm_check
    monkeypatch.setattr(perm_check, "channel_problem", lambda *a, **k: problem)


def perm_guild(me_perms=FULL_PERMS, channels=None):
    """A guild whose bot has me_perms server-wide and whose channels each report what the bot may do."""
    g = guild()
    g.me = SimpleNamespace(guild_permissions=me_perms)
    chans = channels or {}
    g.get_channel = MagicMock(side_effect=lambda cid: chans.get(cid))
    return g


def chan(cid, **perms):
    base = dict(view_channel=True, send_messages=True, embed_links=True, attach_files=True)
    base.update(perms)
    c = MagicMock()
    c.id = cid
    c.mention = f"<#{cid}>"
    c.permissions_for = MagicMock(return_value=SimpleNamespace(**base))
    return c


# ── home and hub ─────────────────────────────────────────────────────────

def test_home_links_to_features(env):
    v = run(env.v1.HomeView.create(interaction()))
    assert "More features" in buttons(v)


def test_features_hub_summarises_all_three(env):
    t = text_of(run(env.v6.FeaturesView.create(interaction())))
    assert "<#71>" in t and "receiving bumps" in t
    assert "Custom role perk" in t and "Autopost" in t


def test_features_hub_survives_db_failure(env):
    for name in ("bump_get_guild_config", "is_custom_role_feature_disabled", "get_custom_role_panel",
                 "get_discord_autopost", "list_discord_autopost_content"):
        setattr(env.db, name, AsyncMock(side_effect=RuntimeError("down")))
    t = text_of(run(env.v6.FeaturesView.create(interaction())))
    assert "Bump: not set up" in t


def test_component_limits_and_hint(env):
    async def go():
        for cls in (env.v1.HomeView, env.v6.FeaturesView, env.v6.BumpView, env.v6.CustomRoleView,
                    env.v6.AutopostView, env.v6.HealthView):
            v = await cls.create(interaction(g=perm_guild()))
            assert len(list(v.walk_children())) < env.v1.MAX_COMPONENTS, cls.__name__
            for row in (c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)):
                assert len(row.children) <= 5, cls.__name__
            assert "/serversetup" in text_of(v), cls.__name__
    run(go())


def test_read_only_inspect_keeps_navigation_but_drops_writes(env):
    ctx = env.v1.InspectContext(guild=guild(), back=AsyncMock())
    v = env.v6.CustomRoleView(GUILD_ID, None, OWNER, {"disabled": False, "panel": {}}, inspect=ctx)
    labels = set(buttons(v))
    assert "Turn off" not in labels and "Back" in labels


# ── bump ─────────────────────────────────────────────────────────────────

def test_bump_screen_shows_channel_and_state(env):
    t = text_of(run(env.v6.BumpView.create(interaction())))
    assert "<#71>" in t and "Receiving bumps" in t


def test_bump_pause_writes_with_clone_and_audit(env):
    i = interaction(clone_id=7)
    v = run(env.v6.BumpView.create(i))
    run(buttons(v)["Pause bumps"].callback(i))
    args, kw = env.db.bump_set_guild_config.await_args
    assert args[:2] == (GUILD_ID, 7) and kw == {"receives_bumps": False}
    assert env.audit.await_args.args[3] == "bump.receives_bumps"


def test_bump_resume_after_pause(env):
    env.db.bump_get_guild_config = AsyncMock(return_value=dict(BUMP_CFG, receives_bumps=False))
    i = interaction()
    v = run(env.v6.BumpView.create(i))
    run(buttons(v)["Resume bumps"].callback(i))
    assert env.db.bump_set_guild_config.await_args.kwargs == {"receives_bumps": True}


def test_bump_never_set_up_cannot_toggle_and_writes_nothing(env):
    env.db.bump_get_guild_config = AsyncMock(return_value=None)
    v = run(env.v6.BumpView.create(interaction()))
    assert "Pause bumps" not in buttons(v) and "Resume bumps" not in buttons(v)
    assert run(env.spf.set_bump_receiving(GUILD_ID, None, OWNER, True)) is False
    env.db.bump_set_guild_config.assert_not_awaited()


def test_bump_resume_blocked_while_recreate_declined(env):
    env.db.bump_get_guild_config = AsyncMock(return_value=dict(
        BUMP_CFG, receives_bumps=False, channel_recreate_declined=True))
    v = run(env.v6.BumpView.create(interaction()))
    assert buttons(v)["Resume bumps"].disabled is True
    assert "declined" in text_of(v)


# ── custom role ──────────────────────────────────────────────────────────

def test_custom_role_turn_off_uses_disable_switch(env):
    i = interaction(clone_id=7)
    v = run(env.v6.CustomRoleView.create(i))
    run(buttons(v)["Turn off"].callback(i))
    args, kw = env.db.set_custom_role_feature_disabled.await_args
    assert args == (GUILD_ID, True) and kw == {"clone_id": 7}
    assert env.audit.await_args.args[3] == "custom_role.disabled"


def test_custom_role_turn_on_when_disabled(env):
    env.db.is_custom_role_feature_disabled = AsyncMock(return_value=True)
    i = interaction()
    v = run(env.v6.CustomRoleView.create(i))
    run(buttons(v)["Turn on"].callback(i))
    assert env.db.set_custom_role_feature_disabled.await_args.args == (GUILD_ID, False)


# ── autopost ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,ok", [("1", True), ("720", True), (" 12 ", True),
                                     ("0", False), ("721", False), ("abc", False), ("", False), ("1.5", False)])
def test_interval_validation(env, text, ok):
    value, err = env.spf.validate_interval(text)
    assert (err is None) is ok
    assert (value is not None) is ok


def test_autopost_channel_pick_turns_on_with_default_interval(env, monkeypatch):
    ok_channel(monkeypatch)
    i = interaction(clone_id=7)
    v = run(env.v6.AutopostView.create(i))
    run(v._channel(i, 62))
    assert env.db.set_discord_autopost.await_args.args == (GUILD_ID, 7, 62, 24, OWNER)
    keys = [c.args[3] for c in env.audit.await_args_list]
    assert {"autopost.enabled", "autopost.channel_id", "autopost.interval_hours"} <= set(keys)


def test_autopost_keeps_existing_interval_when_channel_changes(env, monkeypatch):
    ok_channel(monkeypatch)
    env.db.get_discord_autopost = AsyncMock(return_value={
        "enabled": True, "channel_id": 62, "interval_hours": 6, "last_posted_at": None})
    i = interaction()
    v = run(env.v6.AutopostView.create(i))
    run(v._channel(i, 63))
    assert env.db.set_discord_autopost.await_args.args[2:4] == (63, 6)


def test_autopost_refuses_channel_the_bot_cannot_post_in(env, monkeypatch):
    ok_channel(monkeypatch, problem="I'm missing **Embed Links** in <#62>.")
    i = interaction()
    v = run(env.v6.AutopostView.create(i))
    run(v._channel(i, 62))
    env.db.set_discord_autopost.assert_not_awaited()
    i.response.send_message.assert_awaited()


def test_autopost_without_content_cannot_be_turned_on(env):
    env.db.list_discord_autopost_content = AsyncMock(return_value=[])
    err = run(env.spf.set_autopost(GUILD_ID, None, OWNER, 62, 24))
    assert err and "content" in err
    env.db.set_discord_autopost.assert_not_awaited()
    assert "no autopost content" in text_of(run(env.v6.AutopostView.create(interaction())))


def test_autopost_rejects_bad_interval_without_writing(env):
    err = run(env.spf.set_autopost(GUILD_ID, None, OWNER, 62, 0))
    assert err and "from 1 to 720" in err
    env.db.set_discord_autopost.assert_not_awaited()


def test_autopost_off_audits_only_when_it_was_on(env):
    assert run(env.spf.disable_autopost(GUILD_ID, 7, OWNER)) is True
    assert env.audit.await_args.args[3] == "autopost.enabled"
    env.audit.reset_mock()
    env.db.disable_discord_autopost = AsyncMock(return_value=False)
    assert run(env.spf.disable_autopost(GUILD_ID, 7, OWNER)) is False
    env.audit.assert_not_awaited()


def test_autopost_off_button_only_when_on(env):
    assert "Turn off" not in buttons(run(env.v6.AutopostView.create(interaction())))
    env.db.get_discord_autopost = AsyncMock(return_value={
        "enabled": True, "channel_id": 62, "interval_hours": 6, "last_posted_at": None})
    assert "Turn off" in buttons(run(env.v6.AutopostView.create(interaction())))


def test_interval_modal_denies_other_user_and_lost_access(env):
    m = env.v6.AutopostIntervalModal(24, OWNER)
    m.hours._value = "12"
    stranger = interaction(user=MEMBER, manage=False)
    run(m.on_submit(stranger))
    env.db.set_discord_autopost.assert_not_awaited()
    demoted = interaction(manage=False)
    run(m.on_submit(demoted))
    env.db.set_discord_autopost.assert_not_awaited()


def test_interval_modal_updates_interval_for_current_channel(env, monkeypatch):
    ok_channel(monkeypatch)
    env.db.get_discord_autopost = AsyncMock(return_value={
        "enabled": True, "channel_id": 62, "interval_hours": 24, "last_posted_at": None})
    m = env.v6.AutopostIntervalModal(24, OWNER)
    m.hours._value = "12"
    i = interaction(clone_id=7)
    run(m.on_submit(i))
    assert env.db.set_discord_autopost.await_args.args == (GUILD_ID, 7, 62, 12, OWNER)


# ── permission check ─────────────────────────────────────────────────────

def test_health_all_good(env):
    g = perm_guild(channels={61: chan(61), 71: chan(71)})
    v = run(env.v6.HealthView.create(interaction(g=g)))
    assert "every permission" in text_of(v)


def test_health_lists_server_wide_gaps_by_feature(env):
    perms = SimpleNamespace(**dict(vars(FULL_PERMS), ban_members=False, manage_roles=False))
    g = perm_guild(me_perms=perms)
    t = text_of(run(env.v6.HealthView.create(interaction(g=g))))
    assert "Ban Members" in t and "Manage Roles" in t
    assert "Kick Members" not in t


def test_health_flags_channel_gaps_and_deleted_channels(env):
    g = perm_guild(channels={61: chan(61, embed_links=False)})  # 71 (bump) no longer exists
    t = text_of(run(env.v6.HealthView.create(interaction(g=g))))
    assert "<#61>" in t and "Embed Links" in t
    assert "Bump channel" in t and "can't see" in t


def test_health_ignores_disabled_autopost_channel(env):
    env.db.get_discord_autopost = AsyncMock(return_value={"enabled": False, "channel_id": 99})
    targets = run(env.spf.channel_targets(GUILD_ID, None))
    assert all(label != "Autopost" for label, _ in targets)


def test_health_is_read_only(env):
    perms = SimpleNamespace(**dict(vars(FULL_PERMS), ban_members=False))
    run(env.v6.HealthView.create(interaction(g=perm_guild(me_perms=perms))))
    for w in (env.db.bump_set_guild_config, env.db.set_discord_autopost, env.db.set_custom_role_feature_disabled):
        w.assert_not_awaited()
    env.audit.assert_not_awaited()


def test_health_survives_failed_reads(env):
    env.db.get_welcome_config = AsyncMock(side_effect=RuntimeError("down"))
    env.db.bump_get_guild_config = AsyncMock(side_effect=RuntimeError("down"))
    rep = run(env.spf.health_report(perm_guild(channels={61: chan(61)}), None))
    assert env.spf.health_ok(rep)  # the failed reads are skipped, the channel that still resolves is fine


# ── fix next ─────────────────────────────────────────────────────────────

def test_next_incomplete_picks_first_undone_in_order(env):
    items = [SimpleNamespace(key="welcome", done=True), SimpleNamespace(key="automod", done=False),
             SimpleNamespace(key="tickets", done=False)]
    assert env.spf.next_incomplete(items) == "automod"
    assert env.spf.next_incomplete([SimpleNamespace(key="x", done=True)]) is None
    assert env.spf.next_incomplete([]) is None


def test_setup_screen_offers_fix_next_first(env):
    v = run(env.v1.SetupView.create(interaction()))
    labels = list(buttons(v))
    assert labels[0] == "Fix next"
    assert "Welcome" in labels  # the per-item buttons are still there


def test_setup_screen_has_no_fix_next_when_everything_is_done(env):
    env.db.get_welcome_config = AsyncMock(return_value={"enabled": True, "channel_id": 1})
    env.db.get_verification_config = AsyncMock(return_value={"enabled": True})
    env.db.get_automod_config = AsyncMock(return_value={
        "spam_enabled": True, "log_channel_id": 2, "action": "delete"})
    env.db.get_leveling_config = AsyncMock(return_value={"announce_channel_id": 3})
    env.db.get_ticket_config = AsyncMock(return_value={"panel_channel_id": 4})
    env.db.is_guild_premium_active = AsyncMock(return_value=True)
    v = run(env.v1.SetupView.create(interaction()))
    assert "Fix next" not in buttons(v)
