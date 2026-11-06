"""Anti-nuke v1: detection is per actor, windowed, one-shot per wave; the owner/bot are exempt; only editable dangerous
roles are removed; the cog is inert while disabled."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import antinuke as cog
from modules import antinuke as an

G, ACTOR, OWNER, BOT = 1, 50, 10, 99


@pytest.fixture(autouse=True)
def _clean():
    an.reset()
    yield
    an.reset()


def run(c):
    return asyncio.run(c)


def test_trips_once_at_threshold_and_counts_kinds():
    assert an.record(G, ACTOR, "channel_delete", now=0) is None
    assert an.record(G, ACTOR, "role_delete", now=1) is None
    trip = an.record(G, ACTOR, "member_ban", now=2)
    assert trip["count"] == 3 and trip["kinds"] == {"channel_delete": 1, "role_delete": 1, "member_ban": 1}
    assert an.record(G, ACTOR, "channel_delete", now=3) is None          # same wave: cooldown, no second trip


def test_slow_actions_never_trip():
    for i in range(10):
        assert an.record(G, ACTOR, "channel_delete", now=i * 11) is None


def test_actors_and_guilds_are_counted_separately():
    for who in (1, 2, 3, 4):
        assert an.record(G, who, "channel_delete", now=0) is None
    assert an.record(2, ACTOR, "channel_delete", now=0) is None
    assert an.record(G, ACTOR, "channel_delete", now=0) is None


def test_unwatched_kind_ignored_and_settings_clamped():
    assert an.record(G, ACTOR, "message_delete", now=0) is None
    assert an.clamp_threshold(0) == an.MIN_THRESHOLD and an.clamp_threshold(10**6) == an.MAX_THRESHOLD
    assert an.clamp_threshold("x") == an.DEFAULT_THRESHOLD and an.clamp_window(1) == an.MIN_WINDOW


def test_threshold_is_honoured():
    assert an.record(G, ACTOR, "member_kick", threshold=2, now=0) is None
    assert an.record(G, ACTOR, "member_kick", threshold=2, now=1) is not None


def test_exempt_owner_bot_and_unknown():
    assert an.exempt(OWNER, OWNER, BOT) and an.exempt(OWNER, BOT, BOT) and an.exempt(OWNER, None, BOT)
    assert not an.exempt(OWNER, ACTOR, BOT)


def _role(pos, perms=None, managed=False, default=False):
    return SimpleNamespace(name=f"role{pos}", position=pos, managed=managed, is_default=lambda: default,
                           permissions=discord.Permissions(**(perms or {})))


def test_strippable_roles_only_dangerous_editable_ones():
    admin_low = _role(2, {"administrator": True})
    admin_high = _role(9, {"administrator": True})          # at/above the bot: Discord would refuse
    managed = _role(3, {"administrator": True}, managed=True)
    everyone = _role(0, {"manage_roles": True}, default=True)
    harmless = _role(1, {"send_messages": True})
    chan = _role(4, {"manage_channels": True})
    got = an.strippable_roles([admin_low, admin_high, managed, everyone, harmless, chan], bot_top_position=9)
    assert got == [admin_low, chan]


# ── cog ──────────────────────────────────────────────────────────────────
def make_cog(enabled=True, action="contain", threshold=2):
    bot = MagicMock(clone_id=None)
    bot.user = SimpleNamespace(id=BOT)
    c = cog.AntiNukeCog(bot)
    c._cfg[G] = (10**12, {"enabled": enabled, "threshold": threshold, "window_seconds": 10, "action": action,
                          "log_channel_id": 5})
    return c


def make_guild(member_roles):
    member = SimpleNamespace(id=ACTOR, roles=member_roles, remove_roles=AsyncMock())
    ch = SimpleNamespace(send=AsyncMock())
    me = SimpleNamespace(top_role=SimpleNamespace(position=9), guild_permissions=discord.Permissions(manage_roles=True))
    return SimpleNamespace(id=G, owner_id=OWNER, me=me, get_member=lambda i: member if i == ACTOR else None,
                           get_channel=lambda i: ch, system_channel=None), member, ch


def entry(guild, actor_id=ACTOR, action=discord.AuditLogAction.channel_delete):
    return SimpleNamespace(action=action, guild=guild, user=SimpleNamespace(id=actor_id))


@pytest.fixture()
def patched(monkeypatch):
    monkeypatch.setattr(cog.db, "antinuke_mark_triggered", AsyncMock(), raising=False)
    monkeypatch.setattr(cog.modx, "log_action", AsyncMock())


def test_wave_removes_dangerous_roles_once_and_alerts(patched):
    c = make_cog()
    admin = _role(2, {"administrator": True})
    g, member, ch = make_guild([admin, _role(1, {"send_messages": True})])
    for _ in range(4):
        run(c.on_audit_log_entry_create(entry(g)))
    member.remove_roles.assert_awaited_once()
    assert member.remove_roles.await_args.args[0] is admin
    ch.send.assert_awaited_once()
    assert ch.send.await_args.kwargs["allowed_mentions"].everyone is False


def test_alert_mode_changes_no_roles(patched):
    c = make_cog(action="alert")
    g, member, ch = make_guild([_role(2, {"administrator": True})])
    for _ in range(3):
        run(c.on_audit_log_entry_create(entry(g)))
    member.remove_roles.assert_not_awaited()
    ch.send.assert_awaited_once()


def test_disabled_does_nothing(patched):
    c = make_cog(enabled=False)
    g, member, ch = make_guild([_role(2, {"administrator": True})])
    for _ in range(5):
        run(c.on_audit_log_entry_create(entry(g)))
    member.remove_roles.assert_not_awaited(); ch.send.assert_not_awaited()


def test_owner_and_bot_never_actioned(patched):
    c = make_cog()
    g, member, ch = make_guild([_role(2, {"administrator": True})])
    for who in (OWNER, BOT):
        for _ in range(5):
            run(c.on_audit_log_entry_create(entry(g, actor_id=who)))
    ch.send.assert_not_awaited()


def test_handler_never_raises(patched):
    c = make_cog()
    run(c.on_audit_log_entry_create(SimpleNamespace(action=discord.AuditLogAction.channel_delete, guild=None, user=None)))
    run(c.on_audit_log_entry_create(object()))
