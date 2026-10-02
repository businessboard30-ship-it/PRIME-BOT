"""Server Owners Panel Phase 5: goodbye message, auto-roles, test post."""
import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, MEMBER, OWNER, buttons, interaction, make_db, run, text_of,
)

SP = "modules.server_panel"
V1 = "discord_bot.cogs._views_server_panel"
VX = "discord_bot.cogs._views_server_panel_extras"
WE = "discord_bot.cogs.welcome_extras"
NAMES = (SP, V1, VX, WE)


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.get_welcome_extras = AsyncMock(return_value={
        "goodbye_enabled": True, "goodbye_channel_id": 55,
        "goodbye_message": "Bye {name} from {guild} ({count})",
        "member_role_id": 71, "bot_role_id": None})
    db.set_welcome_extras = AsyncMock()
    dbm.db = db
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no db"))
    cfg = types.ModuleType("config")
    cfg.PREMIUM_GRACE_DAYS = 3
    cfg.PREMIUM_FEE_USD = 2
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "config", cfg)
    wc = types.ModuleType("modules.welcome_card")
    wc.render_welcome_card = MagicMock(return_value=(b"x", "PNG"))
    monkeypatch.setitem(sys.modules, "modules.welcome_card", wc)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = AsyncMock(return_value=[])
    monkeypatch.setitem(sys.modules, "discord_bot.cogs.setup_channels", sc)
    for n in NAMES:
        sys.modules.pop(n, None)
    sp = importlib.import_module(SP)
    v1 = importlib.import_module(V1)
    vx = importlib.import_module(VX)
    we = importlib.import_module(WE)
    audit = AsyncMock()
    monkeypatch.setattr(sp, "record_change", audit)
    we._last_test.clear()
    yield SimpleNamespace(sp=sp, v1=v1, vx=vx, we=we, db=db, audit=audit)
    for n in NAMES:
        sys.modules.pop(n, None)


def member(name="Sam", bot=False, mid=900):
    m = MagicMock()
    m.id, m.bot, m.display_name, m.mention = mid, bot, name, f"<@{mid}>"
    m.display_avatar.url = "http://x/a.png"
    m.guild = MagicMock()
    m.guild.id, m.guild.name, m.guild.member_count = GUILD_ID, "Test Server", 42
    m.guild.me = MagicMock()
    return m


def test_goodbye_placeholders(env):
    out = env.we.apply_goodbye("Bye {name} {guild} {count}", member("Sam"), member().guild)
    assert out == "Bye Sam Test Server 42"


def test_goodbye_name_cannot_ping(env):
    out = env.we.apply_goodbye("{name}", member("@everyone"), member().guild)
    assert "@everyone" not in out.replace("@\u200beveryone", "")


def test_extras_screen_shows_settings_and_limits(env):
    v = run(env.vx.WelcomeExtrasView.create(interaction()))
    t = text_of(v)
    assert "Goodbye message" in t and "<#55>" in t and "<@&71>" in t
    assert len(list(v.walk_children())) < 40
    assert {"Test goodbye", "Edit message", "Clear member role"} <= set(buttons(v))


def test_toggle_requires_channel(env):
    env.db.get_welcome_extras = AsyncMock(return_value={
        "goodbye_enabled": False, "goodbye_channel_id": None,
        "goodbye_message": "x", "member_role_id": None, "bot_role_id": None})
    v = run(env.vx.WelcomeExtrasView.create(interaction()))
    i = interaction()
    run(buttons(v)["Turn goodbye on"].callback(i))
    i.response.send_message.assert_awaited()
    env.db.set_welcome_extras.assert_not_awaited()


def test_clear_role_writes_none_with_clone_and_audit(env):
    i = interaction(clone_id=7)
    v = run(env.vx.WelcomeExtrasView.create(i))
    run(buttons(v)["Clear member role"].callback(i))
    args, kwargs = env.db.set_welcome_extras.await_args
    assert kwargs["clone_id"] == 7 and kwargs["member_role_id"] is None
    assert env.audit.await_args.args[3] == "welcome_extras.member_role_id"


def test_non_manager_denied_on_modal(env):
    i = interaction(user=MEMBER, manage=False)
    modal = env.vx.GoodbyeMessageModal("x", MEMBER)
    modal.template._value = "hello"
    run(modal.on_submit(i))
    env.db.set_welcome_extras.assert_not_awaited()


def test_other_user_cannot_submit_modal(env):
    i = interaction(user=OWNER)
    modal = env.vx.GoodbyeMessageModal("x", opener_id=MEMBER)
    modal.template._value = "hello"
    run(modal.on_submit(i))
    env.db.set_welcome_extras.assert_not_awaited()


def test_welcome_screen_has_test_and_extras_buttons(env):
    v = run(env.v1.WelcomeView.create(interaction()))
    b = buttons(v)
    assert "Send test post" in b and "Goodbye & roles" in b


def test_auto_role_member_vs_bot(env):
    cog = env.we.WelcomeExtrasCog(MagicMock(clone_id=None))
    role = MagicMock(); role.is_default.return_value = False; role.managed = False
    role.__ge__ = lambda s, o: False
    env.db.get_welcome_extras = AsyncMock(return_value={"member_role_id": 71, "bot_role_id": 72})
    for is_bot, expected in ((False, 71), (True, 72)):
        m = member(bot=is_bot)
        m.guild.get_role = MagicMock(return_value=role)
        m.guild.me.guild_permissions.manage_roles = True
        m.guild.me.top_role = MagicMock()
        m.add_roles = AsyncMock()
        role.__ge__ = lambda s, o: False
        run(cog.on_member_join(m))
        m.guild.get_role.assert_called_with(expected)
        m.add_roles.assert_awaited_once()


def test_auto_role_missing_role_flags_instead_of_crashing(env):
    cog = env.we.WelcomeExtrasCog(MagicMock(clone_id=None))
    env.db.get_welcome_extras = AsyncMock(return_value={"member_role_id": 71, "bot_role_id": None})
    m = member()
    m.guild.get_role = MagicMock(return_value=None)
    m.add_roles = AsyncMock()
    run(cog.on_member_join(m))
    m.add_roles.assert_not_awaited()


def test_goodbye_skips_bots_and_disabled(env):
    cog = env.we.WelcomeExtrasCog(MagicMock(clone_id=None))
    ch = MagicMock(); ch.send = AsyncMock()
    m = member(bot=True); m.guild.get_channel = MagicMock(return_value=ch)
    run(cog.on_member_remove(m))
    ch.send.assert_not_awaited()
    env.db.get_welcome_extras = AsyncMock(return_value={"goodbye_enabled": False, "goodbye_channel_id": 55})
    m2 = member(); m2.guild.get_channel = MagicMock(return_value=ch)
    run(cog.on_member_remove(m2))
    ch.send.assert_not_awaited()


def test_goodbye_posts_without_mentions(env):
    cog = env.we.WelcomeExtrasCog(MagicMock(clone_id=None))
    ch = MagicMock(); ch.send = AsyncMock()
    m = member(); m.guild.get_channel = MagicMock(return_value=ch)
    run(cog.on_member_remove(m))
    ch.send.assert_awaited_once()
    assert ch.send.await_args.kwargs["allowed_mentions"].users is False


def test_test_goodbye_needs_channel_and_has_cooldown(env):
    env.db.get_welcome_extras = AsyncMock(return_value={"goodbye_channel_id": None})
    ok, msg = run(env.we.send_test_goodbye(MagicMock(clone_id=None), member().guild, member()))
    assert not ok and "channel" in msg
    assert env.we.cooldown_left(GUILD_ID, None) == 0


def test_test_welcome_needs_channel(env):
    env.db.get_welcome_config = AsyncMock(return_value={"channel_id": None, "delivery_mode": "channel"})
    ok, msg = run(env.we.send_test_welcome(MagicMock(clone_id=None), member().guild, member()))
    assert not ok and "welcome channel" in msg


def test_moderation_screen_shows_honeypot_stats_alert_and_test(env):
    env.db.get_honeypot_config = AsyncMock(return_value={
        "channel_id": 5, "enabled": True, "alert_role_id": 71, "triggered_count": 12})
    env.db.get_honeypot_stats = AsyncMock(return_value={"day": 1, "week": 4, "month": 9, "total": 12})
    v = run(env.v1.ModerationView.create(interaction()))
    t = text_of(v)
    assert "4 this week" in t and "12 all time" in t and "<@&71>" in t
    assert "Test honeypot" in buttons(v)
    assert any(isinstance(c, discord.ui.RoleSelect) for c in v.walk_children())
    assert len(list(v.walk_children())) < 40


def test_moderation_screen_survives_stats_failure(env):
    env.db.get_honeypot_config = AsyncMock(return_value={"channel_id": 5, "enabled": True})
    env.db.get_honeypot_stats = AsyncMock(side_effect=RuntimeError("db"))
    v = run(env.v1.ModerationView.create(interaction()))
    assert "0 today" in text_of(v)
