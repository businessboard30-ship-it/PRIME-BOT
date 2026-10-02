"""Honeypot Phase 6: staff alert role ping, catch stats, test alert, panel wiring."""
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
GUILD, CHAN, LOGCH, ROLE = 5000, 6000, 7000, 8000


def run(c):
    return asyncio.run(c)


class FakeDB:
    def __init__(self):
        self.premium = False
        self.cfg = {"guild_id": GUILD, "clone_id": None, "channel_id": CHAN, "enabled": True,
                    "action": "ban", "delete_seconds": 86400, "log_channel_id": LOGCH,
                    "warning_message_id": None, "channel_auto_created": False,
                    "triggered_count": 3, "last_triggered_at": None, "created_by": None,
                    "alert_role_id": ROLE}
        self.catches, self.writes = [], []
        self.stats = {"day": 1, "week": 4, "month": 9, "failed_month": 0, "total": 12,
                      "last_at": datetime(2026, 10, 1, tzinfo=timezone.utc)}

    async def is_guild_premium_active(self, g, c): return self.premium
    async def get_honeypot_config(self, g, clone_id=None): return dict(self.cfg)
    async def set_honeypot_config(self, g, clone_id=None, **f):
        self.writes.append(f); self.cfg.update(f); return dict(self.cfg)
    async def bump_honeypot_triggers(self, g, clone_id=None): self.cfg["triggered_count"] += 1
    async def get_automod_config(self, g, clone_id=None): return {"log_channel_id": LOGCH}
    async def record_honeypot_catch(self, g, c, uid, action, ok): self.catches.append((uid, action, ok))
    async def get_honeypot_stats(self, g, clone_id=None): return dict(self.stats)


@pytest.fixture()
def hp(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database"); dbm.db = fake
    monkeypatch.setitem(sys.modules, "database", dbm)
    sys.modules.pop(HP, None)
    mod = importlib.import_module(HP)
    mod._cache.clear(); mod._tripping.clear(); mod._last_test.clear()
    mod.fake = fake
    yield mod
    sys.modules.pop(HP, None)


def make_role(mentionable=True, default=False):
    r = MagicMock(); r.id = ROLE; r.mention = f"<@&{ROLE}>"; r.mentionable = mentionable
    r.is_default.return_value = default
    return r


def make_guild(role=None, log_ok=True):
    g = MagicMock(); g.id = GUILD; g.name = "Srv"
    log = MagicMock(); log.id = LOGCH
    log.send = AsyncMock() if log_ok else AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    g.get_channel = MagicMock(side_effect=lambda cid: log if cid == LOGCH else MagicMock())
    g.get_role = MagicMock(side_effect=lambda rid: role if rid == ROLE else None)
    g.log = log
    return g


def make_message(guild, uid=111):
    m = MagicMock(); m.guild = guild; m.delete = AsyncMock(); m.content = "free nitro"; m.attachments = []
    a = MagicMock(); a.id = uid; a.mention = f"<@{uid}>"; a.send = AsyncMock()
    a.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc); a.joined_at = None
    a.display_avatar.url = "http://x/a.png"
    m.author = a
    m.channel.id = CHAN
    return m


def bot():
    b = MagicMock(); b.clone_id = None; return b


# ── alert role ping ──────────────────────────────────────────────────────

def test_catch_pings_only_the_alert_role_and_logs_it(hp):
    g = make_guild(make_role())
    hp._apply_action = AsyncMock(return_value=(True, "Banned"))
    run(hp.trip(bot(), make_message(g), dict(hp.fake.cfg)))
    kw = g.log.send.await_args.kwargs
    assert f"<@&{ROLE}>" in kw["content"] and "could NOT" not in kw["content"]
    am = kw["allowed_mentions"]
    assert am.everyone is False and am.users is False and [r.id for r in am.roles] == [ROLE]
    assert hp.fake.catches == [(111, "ban", True)]


def test_failed_action_pings_with_needs_a_human(hp):
    g = make_guild(make_role())
    hp._apply_action = AsyncMock(return_value=(False, "missing perms"))
    run(hp.trip(bot(), make_message(g), dict(hp.fake.cfg)))
    assert "could NOT act" in g.log.send.await_args.kwargs["content"]
    assert hp.fake.catches[0][2] is False


def test_no_alert_role_means_no_ping_but_still_logged(hp):
    g = make_guild(None)
    cfg = dict(hp.fake.cfg, alert_role_id=None)
    hp._apply_action = AsyncMock(return_value=(True, "Banned"))
    run(hp.trip(bot(), make_message(g), cfg))
    kw = g.log.send.await_args.kwargs
    assert kw["content"] is None and kw["allowed_mentions"].roles is False


def test_deleted_or_everyone_role_is_ignored(hp):
    assert hp.alert_role_of(make_guild(None), {"alert_role_id": ROLE}) is None
    assert hp.alert_role_of(make_guild(make_role(default=True)), {"alert_role_id": ROLE}) is None


def test_catch_log_failure_never_breaks_the_ban(hp):
    g = make_guild(make_role())
    hp.fake.record_honeypot_catch = AsyncMock(side_effect=RuntimeError("db"))
    hp._apply_action = AsyncMock(return_value=(True, "Banned"))
    run(hp.trip(bot(), make_message(g), dict(hp.fake.cfg)))
    assert hp.fake.cfg["triggered_count"] == 4
    g.log.send.assert_awaited()


# ── test alert ───────────────────────────────────────────────────────────

def make_tester():
    t = MagicMock(); t.mention = "<@1>"; t.display_avatar.url = "http://x/a.png"; return t


def test_test_alert_pings_but_changes_nothing(hp):
    g = make_guild(make_role())
    ok, msg = run(hp.send_test_alert(bot(), g, make_tester()))
    assert ok and "pinging" in msg
    assert f"<@&{ROLE}>" in g.log.send.await_args.kwargs["content"]
    assert hp.fake.catches == [] and hp.fake.cfg["triggered_count"] == 3 and hp.fake.writes == []


def test_test_alert_without_role_says_nobody_pinged(hp):
    g = make_guild(None)
    ok, msg = run(hp.send_test_alert(bot(), g, make_tester()))
    assert ok and "nobody was pinged" in msg


def test_test_alert_requires_setup_and_has_cooldown(hp):
    hp.fake.cfg["channel_id"] = None
    ok, msg = run(hp.send_test_alert(bot(), make_guild(make_role()), make_tester()))
    assert not ok and "Set up" in msg
    hp.fake.cfg["channel_id"] = CHAN
    assert run(hp.send_test_alert(bot(), make_guild(make_role()), make_tester()))[0]
    ok, msg = run(hp.send_test_alert(bot(), make_guild(make_role()), make_tester()))
    assert not ok and "try again" in msg


def test_test_alert_reports_unpostable_log(hp):
    ok, msg = run(hp.send_test_alert(bot(), make_guild(make_role(), log_ok=False), make_tester()))
    assert not ok and "couldn't post" in msg


# ── stats + panel ────────────────────────────────────────────────────────

def test_stats_lines_show_windows_and_failures(hp):
    t = "\n".join(hp.stats_lines(dict(hp.fake.stats, failed_month=2)))
    assert "1 today" in t and "4 this week" in t and "9 this month" in t and "12 all time" in t
    assert "2" in t and "couldn't act" in t and "Last catch" in t


def test_panel_has_stats_alert_select_and_test_button(hp):
    g = make_guild(make_role())
    g.me.guild_permissions.manage_messages = True
    view = hp.build_panel(g, None, dict(hp.fake.cfg), False, stats=hp.fake.stats)
    kids = list(view.walk_children())
    text = "\n".join(c.content for c in kids if isinstance(c, discord.ui.TextDisplay))
    assert "4 this week" in text and f"<@&{ROLE}>" in text
    assert any(isinstance(c, hp.HoneypotAlertRoleSelect) or getattr(c, "item", None).__class__ is discord.ui.RoleSelect
               for c in kids)
    labels = {getattr(c, "label", None) or getattr(getattr(c, "item", None), "label", None) for c in kids}
    assert {"Send test alert", "Clear alert role"} <= labels
    assert len(kids) < 40


def test_alert_role_select_works_for_free_servers_and_rejects_everyone(hp):
    g = make_guild(make_role(default=True))
    g.me.guild_permissions.mention_everyone = False
    member = MagicMock(); member.guild_permissions.manage_guild = True
    g.get_member = MagicMock(return_value=member)
    i = MagicMock(); i.client.get_guild = MagicMock(return_value=g); i.client.clone_id = None
    i.user.id = 9; i.response.is_done = MagicMock(return_value=False)
    i.response.send_message = AsyncMock(); i.response.defer = AsyncMock()
    item = hp.HoneypotAlertRoleSelect(GUILD, None)
    item.item._values = [SimpleNamespace(id=ROLE)]
    run(item.callback(i))
    assert hp.fake.writes == []  # @everyone refused
    g.get_role = MagicMock(return_value=make_role(mentionable=False))
    i.response.is_done = MagicMock(return_value=False)
    i.edit_original_response = AsyncMock(); i.followup.send = AsyncMock()
    run(item.callback(i))
    assert hp.fake.writes[-1] == {"alert_role_id": ROLE}  # free: no premium gate
    assert "isn't mentionable" in i.followup.send.await_args.args[0]
