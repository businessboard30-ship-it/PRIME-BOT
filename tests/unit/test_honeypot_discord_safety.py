"""Honeypot Discord-safety: no scam text in staff logs, optional silent actions, hide from member roles."""
import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

HP = "discord_bot.cogs.honeypot"
GUILD, CHAN, LOGCH = 5000, 6000, 7000
SCAM = "FREE NITRO http://evil.example/claim-now"


def run(c):
    return asyncio.run(c)


class FakeDB:
    def __init__(self):
        self.cfg = {"guild_id": GUILD, "clone_id": None, "channel_id": CHAN, "enabled": True, "action": "ban",
                    "delete_seconds": 86400, "log_channel_id": LOGCH, "warning_message_id": None,
                    "channel_auto_created": False, "triggered_count": 0, "last_triggered_at": None,
                    "created_by": None, "alert_role_id": None, "dm_offender": True, "hide_from_members": False}

    async def is_guild_premium_active(self, g, c): return False
    async def get_honeypot_config(self, g, clone_id=None): return dict(self.cfg)
    async def set_honeypot_config(self, g, clone_id=None, **f): self.cfg.update(f); return dict(self.cfg)
    async def bump_honeypot_triggers(self, g, clone_id=None): pass
    async def get_automod_config(self, g, clone_id=None): return {"log_channel_id": LOGCH}
    async def record_honeypot_catch(self, *a): pass


@pytest.fixture()
def hp(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database"); dbm.db = fake
    monkeypatch.setitem(sys.modules, "database", dbm)
    sys.modules.pop(HP, None)
    mod = importlib.import_module(HP)
    mod._cache.clear(); mod._tripping.clear()
    mod.fake = fake
    yield mod
    sys.modules.pop(HP, None)


def make_guild():
    g = MagicMock(); g.id = GUILD; g.name = "Srv"
    log = MagicMock(); log.id = LOGCH; log.send = AsyncMock()
    g.get_channel = MagicMock(side_effect=lambda cid: log if cid == LOGCH else MagicMock())
    g.get_role = MagicMock(return_value=None)
    g.log = log
    return g


def make_message(guild):
    m = MagicMock(); m.guild = guild; m.delete = AsyncMock(); m.content = SCAM; m.attachments = []
    a = MagicMock(); a.id = 111; a.mention = "<@111>"; a.send = AsyncMock()
    a.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc); a.joined_at = None
    a.display_avatar.url = "http://x/a.png"
    m.author = a
    return m


def bot():
    b = MagicMock(); b.clone_id = None; return b


def embed_text(g):
    e = g.log.send.await_args.kwargs["embed"]
    return (e.title or "") + (e.description or "") + "".join(f.name + f.value for f in e.fields)


def test_staff_log_never_contains_the_trapped_message(hp):
    g = make_guild(); msg = make_message(g)
    hp._apply_action = AsyncMock(return_value=(True, "Banned"))
    run(hp.trip(bot(), msg, dict(hp.fake.cfg)))
    text = embed_text(g)
    assert "NITRO" not in text and "evil.example" not in text and "http://evil" not in text
    assert "111" in text   # the account is still identified


def test_staff_warning_log_never_contains_the_message(hp):
    g = make_guild(); msg = make_message(g)
    msg.channel.send = AsyncMock()
    run(hp.warn_staff(bot(), msg, dict(hp.fake.cfg)))
    assert "NITRO" not in embed_text(g) and "evil.example" not in embed_text(g)


def test_dm_is_sent_by_default_and_skipped_when_silent(hp):
    g = make_guild(); msg = make_message(g)
    hp._apply_action = AsyncMock(return_value=(True, "Banned"))
    run(hp.trip(bot(), msg, dict(hp.fake.cfg)))
    msg.author.send.assert_awaited()
    hp._tripping.clear()
    g2 = make_guild(); msg2 = make_message(g2)
    run(hp.trip(bot(), msg2, dict(hp.fake.cfg, dm_offender=False)))
    msg2.author.send.assert_not_awaited()
    g2.log.send.assert_awaited()


def role(name, staff=False, managed=False, default=False):
    perms = SimpleNamespace(administrator=False, manage_guild=False, manage_messages=staff, ban_members=False,
                            kick_members=False, moderate_members=False)
    r = MagicMock(); r.name = name; r.managed = managed; r.permissions = perms
    r.is_default.return_value = default
    return r


def channel_with(overwrites=None):
    ch = MagicMock(); ch.overwrites = overwrites or {}; ch.edit = AsyncMock()
    return ch


def test_hide_denies_view_for_member_roles_only(hp):
    everyone, member, mod, botrole = role("@everyone", default=True), role("Member"), role("Mod", staff=True), \
        role("Bot", managed=True)
    g = make_guild(); g.roles = [everyone, member, mod, botrole]
    ch = channel_with()
    ok, _ = run(hp.apply_member_visibility(g, ch, True))
    assert ok
    ow = ch.edit.await_args.kwargs["overwrites"]
    assert set(ow) == {member} and ow[member].view_channel is False


def test_hide_is_idempotent_and_show_undoes_it(hp):
    member = role("Member")
    g = make_guild(); g.roles = [member]
    denied = discord.PermissionOverwrite(view_channel=False)
    ch = channel_with({member: denied})
    ok, msg = run(hp.apply_member_visibility(g, ch, True))
    assert ok and msg == "Nothing to change." and not ch.edit.await_count
    ok, _ = run(hp.apply_member_visibility(g, ch, False))
    assert ok and member not in ch.edit.await_args.kwargs["overwrites"]


def test_show_keeps_other_overwrites_on_the_role(hp):
    member = role("Member")
    g = make_guild(); g.roles = [member]
    ch = channel_with({member: discord.PermissionOverwrite(view_channel=False, send_messages=False)})
    run(hp.apply_member_visibility(g, ch, False))
    ow = ch.edit.await_args.kwargs["overwrites"][member]
    assert ow.view_channel is None and ow.send_messages is False


def test_missing_permission_reports_instead_of_raising(hp):
    member = role("Member")
    g = make_guild(); g.roles = [member]
    ch = channel_with(); ch.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    ok, msg = run(hp.apply_member_visibility(g, ch, True))
    assert not ok and "Manage" in msg


def test_panel_shows_both_toggles(hp):
    g = make_guild()
    view = hp.build_panel(g, None, dict(hp.fake.cfg, dm_offender=False, hide_from_members=True), False)
    labels = []
    def walk(items):
        for it in items:
            lab = getattr(getattr(it, "item", it), "label", None)
            if lab: labels.append(lab)
            walk(getattr(it, "children", []) or [])
    walk(view.children)
    assert "DM caught account: Off" in labels and "Hide from member roles: On" in labels
