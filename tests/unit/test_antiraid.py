"""Anti-raid: spike detection, lockdown + restore, joiner actions, wizard wiring."""
import asyncio
import importlib
import sys
import types
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

AR = "discord_bot.cogs.antiraid"
GUILD, LOG, ROLE, OWNER, MANAGER = 7000, 7100, 7200, 1, 2


def run(c):
    return asyncio.run(c)


class FakeDB:
    def __init__(self, **cfg):
        self.cfg = {
            "guild_id": GUILD, "clone_id": None, "enabled": True, "sensitivity": "strict",
            "response": "lockdown", "joiner_action": "none", "lockdown_minutes": 15,
            "log_channel_id": LOG, "alert_role_id": ROLE, "active_until": None,
            "prev_verification": None, "triggered_count": 0, "last_triggered_at": None,
            "created_by": None,
        }
        self.cfg.update(cfg)
        self.writes = []
        self.bumps = 0
        self.deleted = False

    async def get_antiraid_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_antiraid_config(self, g, clone_id=None, **f):
        self.writes.append(f)
        self.cfg.update(f)
        return dict(self.cfg)

    async def bump_antiraid_triggers(self, g, clone_id=None):
        self.bumps += 1

    async def delete_antiraid_config(self, g, clone_id=None):
        self.deleted = True

    async def get_automod_config(self, g, clone_id=None):
        return {"log_channel_id": None}


def make_member(uid, staff=False):
    perms = SimpleNamespace(administrator=False, manage_guild=staff, manage_messages=False,
                            ban_members=False, kick_members=False, moderate_members=False)
    m = SimpleNamespace(id=uid, mention=f"<@{uid}>", top_role=1, guild_permissions=perms,
                        timeout=AsyncMock())
    return m


def make_guild(members=(), level=discord.VerificationLevel.medium, with_log=True):
    g = MagicMock()
    g.id = GUILD
    g.owner_id = OWNER
    g.verification_level = level
    g.edit = AsyncMock()
    g.kick = AsyncMock()
    roster = {m.id: m for m in members}
    for m in members:
        m.guild = g                                   # a real Member always has .guild
    g.get_member = MagicMock(side_effect=lambda uid: roster.get(uid))
    all_perms = SimpleNamespace(manage_guild=True, moderate_members=True, kick_members=True,
                                mention_everyone=False)
    g.me = SimpleNamespace(guild_permissions=all_perms, top_role=10)
    role = SimpleNamespace(id=ROLE, mention="<@&7200>", is_default=lambda: False, mentionable=True)
    g.get_role = MagicMock(side_effect=lambda rid: role if rid == ROLE else None)
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.mention = "<#7100>"
    g.log = channel
    g.get_channel = MagicMock(side_effect=lambda cid: channel if (with_log and cid == LOG) else None)
    return g


@pytest.fixture()
def ar(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database")
    dbm.db = fake
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in (AR, "modules.server_panel"):
        sys.modules.pop(name, None)
    mod = importlib.import_module(AR)
    mod._cache.clear(); mod._joins.clear(); mod._starting.clear()
    mod._last_persist.clear(); mod._last_test.clear()
    bot = SimpleNamespace(clone_id=None)
    yield SimpleNamespace(mod=mod, db=fake, bot=bot)
    for name in (AR, "modules.server_panel"):
        sys.modules.pop(name, None)


# ── detection ─────────────────────────────────────────────────────────────

def test_record_join_fires_at_threshold_then_resets(ar):
    d = deque()
    assert ar.mod.record_join(d, 0.0, 1, 15, 4) is None
    assert ar.mod.record_join(d, 1.0, 2, 15, 4) is None
    assert ar.mod.record_join(d, 2.0, 3, 15, 4) is None
    assert ar.mod.record_join(d, 3.0, 4, 15, 4) == [1, 2, 3, 4]
    assert len(d) == 0                                  # same burst can't fire twice


def test_record_join_ignores_slow_trickle(ar):
    d = deque()
    hits = [ar.mod.record_join(d, t * 10.0, t, 15, 4) for t in range(12)]
    assert not any(hits)                                # 1 join / 10s never reaches 4 in 15s


def test_presets_map_to_numbers(ar):
    assert ar.mod.thresholds({"sensitivity": "relaxed"}) == (12, 60)
    assert ar.mod.thresholds({"sensitivity": "balanced"}) == (7, 30)
    assert ar.mod.thresholds({"sensitivity": "strict"}) == (4, 15)
    assert ar.mod.thresholds({"sensitivity": "garbage"}) == (7, 30)   # falls back to balanced


# ── lockdown lifecycle ────────────────────────────────────────────────────

def test_start_raid_locks_down_saves_previous_level_and_pings_role(ar):
    g = make_guild()
    ok, msg = run(ar.mod.start_raid(ar.bot, g, joined_ids=[10, 11, 12, 13]))
    assert ok
    g.edit.assert_awaited_once()
    assert g.edit.await_args.kwargs["verification_level"] == discord.VerificationLevel.highest
    assert ar.db.cfg["prev_verification"] == discord.VerificationLevel.medium.value
    assert ar.db.cfg["active_until"] > datetime.now(timezone.utc)
    assert ar.db.bumps == 1
    sent = g.log.send.await_args.kwargs
    assert "<@&7200>" in sent["content"]
    assert sent["allowed_mentions"].roles and not sent["allowed_mentions"].everyone


def test_alert_only_changes_nothing_but_still_enters_raid_mode(ar):
    ar.db.cfg["response"] = "alert"
    g = make_guild()
    ok, _ = run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    assert ok
    g.edit.assert_not_awaited()
    assert ar.db.cfg["prev_verification"] is None
    assert ar.db.cfg["active_until"] is not None        # so the alert doesn't repeat every join
    g.log.send.assert_awaited()


def test_second_start_while_active_is_refused(ar):
    g = make_guild()
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    ok, msg = run(ar.mod.start_raid(ar.bot, g, joined_ids=[11]))
    assert not ok and "already active" in msg
    assert g.edit.await_count == 1 and ar.db.bumps == 1


def test_already_highest_is_not_touched_or_restored(ar):
    g = make_guild(level=discord.VerificationLevel.highest)
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    g.edit.assert_not_awaited()
    assert ar.db.cfg["prev_verification"] is None
    run(ar.mod.end_raid(ar.bot, g))
    g.edit.assert_not_awaited()                          # nothing of ours to undo


def test_end_raid_restores_level_and_clears_state(ar):
    g = make_guild()
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    g.verification_level = discord.VerificationLevel.highest   # what Discord now reports
    g.edit.reset_mock()
    ok, msg = run(ar.mod.end_raid(ar.bot, g))
    assert ok and "Medium" in msg
    assert g.edit.await_args.kwargs["verification_level"] == discord.VerificationLevel.medium
    assert ar.db.cfg["active_until"] is None and ar.db.cfg["prev_verification"] is None


def test_end_raid_respects_a_manual_level_change(ar):
    g = make_guild()
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    g.verification_level = discord.VerificationLevel.low       # admin changed it by hand meanwhile
    g.edit.reset_mock()
    run(ar.mod.end_raid(ar.bot, g))
    g.edit.assert_not_awaited()
    assert ar.db.cfg["active_until"] is None


def test_end_raid_when_not_active(ar):
    ok, msg = run(ar.mod.end_raid(ar.bot, make_guild()))
    assert not ok and "isn't active" in msg


def test_missing_manage_server_is_reported_not_raised(ar):
    g = make_guild()
    g.edit.side_effect = discord.Forbidden(MagicMock(status=403), "nope")
    ok, msg = run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    assert ok and "couldn't raise verification" in msg
    assert ar.db.cfg["prev_verification"] is None              # nothing to restore later


# ── joiner actions ────────────────────────────────────────────────────────

def test_timeout_action_hits_joiners_but_never_staff(ar):
    ar.db.cfg["joiner_action"] = "timeout"
    a, b, staff = make_member(10), make_member(11), make_member(12, staff=True)
    g = make_guild([a, b, staff])
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10, 11, 12]))
    a.timeout.assert_awaited_once()
    b.timeout.assert_awaited_once()
    staff.timeout.assert_not_awaited()
    assert a.timeout.await_args.args[0] == timedelta(minutes=ar.mod.RAID_TIMEOUT_MINUTES)


def test_kick_action_and_failure_count(ar):
    ar.db.cfg["joiner_action"] = "kick"
    a, b = make_member(10), make_member(11)
    class _Top(int):
        mention = "@Admin"
    b.top_role = _Top(99)                                      # above the bot: can't be kicked
    g = make_guild([a, b])
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10, 11]))
    g.kick.assert_awaited_once()
    embed = g.log.send.await_args.kwargs["embed"]
    assert "1 account(s) kicked" in embed.description and "1** I couldn't" in embed.description


# ── the join listener ─────────────────────────────────────────────────────

def test_disabled_feature_ignores_joins(ar):
    ar.db.cfg["enabled"] = False
    g = make_guild()
    for uid in range(10, 20):
        run(ar.mod.handle_join(ar.bot, SimpleNamespace(guild=g, id=uid)))
    g.edit.assert_not_awaited()


def test_burst_of_joins_triggers_once_and_later_joiners_are_actioned(ar):
    ar.db.cfg["joiner_action"] = "timeout"
    members = [make_member(uid) for uid in range(10, 16)]
    g = make_guild(members)

    async def go():
        for m in members[:4]:                                  # strict = 4 joins in 15s
            await ar.mod.handle_join(ar.bot, SimpleNamespace(guild=g, id=m.id))
        assert g.edit.await_count == 1 and ar.db.bumps == 1
        await ar.mod.handle_join(ar.bot, members[4])           # joins DURING the raid
    run(go())
    members[4].timeout.assert_awaited_once()
    assert ar.db.bumps == 1                                    # no second alert for the same raid
    assert g.edit.await_count == 1


# ── wizard wiring ─────────────────────────────────────────────────────────

def test_every_dynamic_item_roundtrips_its_custom_id(ar):
    import re
    items = [ar.mod.AntiRaidSelect(k, GUILD, 3, "x") for k in ("sens", "resp", "joiner", "dur")]
    items += [ar.mod.AntiRaidLogSelect(GUILD, 3), ar.mod.AntiRaidRoleSelect(GUILD, None)]
    items += [ar.mod.AntiRaidButton(k, GUILD, None) for k in ("toggle", "lockdown", "unlock", "test", "panel")]
    for it in items:
        cid = it.item.custom_id
        matches = [c for c in ar.mod.DYNAMIC_ITEMS if re.match(c.__discord_ui_compiled_template__, cid)]
        assert len(matches) == 1, cid                          # exactly one class claims each id
        assert isinstance(it, matches[0])


def test_panel_fits_component_limit_and_shows_steps(ar):
    g = make_guild()
    cfg = dict(ar.db.cfg)
    view = ar.mod.build_panel(g, None, cfg, g.log)
    assert len(list(view.walk_children())) < 40
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    for needle in ("1. Sensitivity", "2. When a raid hits", "3. New joiners", "4. Where alerts go", "Watching"):
        assert needle in text


def test_panel_switches_buttons_when_raid_active(ar):
    g = make_guild()
    cfg = dict(ar.db.cfg, active_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    view = ar.mod.build_panel(g, None, cfg, g.log)
    labels = {c.item.label for c in view.walk_children() if isinstance(c, ar.mod.AntiRaidButton)}
    assert "End lockdown" in labels and "Lockdown now" not in labels and "RAID MODE ACTIVE" in "\n".join(
        c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def _click_interaction(user_perm_manage=True):
    i = MagicMock()
    i.user.id = MANAGER
    i.guild = None
    i.response.is_done = MagicMock(return_value=False)
    i.response.defer = AsyncMock()
    i.response.send_message = AsyncMock()
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    return i


def test_non_managers_cannot_use_the_wizard(ar):
    g = make_guild()
    member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=False))
    g.owner = object()
    g.get_member = MagicMock(return_value=member)
    i = _click_interaction()
    i.client.get_guild = MagicMock(return_value=g)
    assert run(ar.mod._authorize(i, GUILD)) is None
    assert "Manage Server" in i.response.send_message.await_args.args[0]


def test_turning_on_requires_a_log_channel(ar):
    ar.db.cfg.update(enabled=False, log_channel_id=None)
    g = make_guild(with_log=False)
    member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True))
    g.get_member = MagicMock(return_value=member)
    i = _click_interaction()
    i.client.get_guild = MagicMock(return_value=g)
    btn = ar.mod.AntiRaidButton("toggle", GUILD, None)
    run(btn.callback(i))
    assert ar.db.cfg["enabled"] is False
    assert "log channel" in i.followup.send.await_args.args[0]


def test_selecting_a_preset_saves_through_the_audited_setter(ar, monkeypatch):
    g = make_guild()
    member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True))
    g.get_member = MagicMock(return_value=member)
    i = _click_interaction()
    i.client.get_guild = MagicMock(return_value=g)
    audit = AsyncMock()
    import modules.server_panel as sp
    monkeypatch.setattr(sp, "record_change", audit)
    sel = ar.mod.AntiRaidSelect("sens", GUILD, None, "strict")
    sel.item._values = ["relaxed"]                     # what Discord sends back on a click
    run(sel.callback(i))
    assert ar.db.cfg["sensitivity"] == "relaxed"
    keys = [c.args[3] for c in audit.await_args_list]
    assert "antiraid.sensitivity" in keys


# ── server panel integration ──────────────────────────────────────────────

def test_moderation_screen_has_an_antiraid_button_and_status_line(ar):
    for name in ("discord_bot.cogs._views_server_panel",):
        sys.modules.pop(name, None)
    views = importlib.import_module("discord_bot.cogs._views_server_panel")
    data = {"am": {}, "hp": {}, "hp_stats": {}, "ar": {"enabled": True, "sensitivity": "strict", "response": "lockdown"}}
    view = views.ModerationView(GUILD, None, MANAGER, data)
    labels = {c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)}
    assert "Anti-raid settings" in labels
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "Anti-raid: ✅ on · strict · lockdown" in text
    assert len(list(view.walk_children())) < 40

    off = views.ModerationView(GUILD, None, MANAGER, {"am": {}, "hp": {}, "ar": {}})
    assert "Anti-raid" in {c.label for c in off.walk_children() if isinstance(c, discord.ui.Button)}
    sys.modules.pop("discord_bot.cogs._views_server_panel", None)


def test_reset_group_ends_active_lockdown_before_deleting(ar):
    import modules.server_panel as sp
    assert "antiraid" in sp.RESET_GROUPS
    g = make_guild()
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[10]))
    g.verification_level = discord.VerificationLevel.highest
    g.edit.reset_mock()
    bot = SimpleNamespace(clone_id=None, get_guild=lambda gid: g)
    run(sp.reset_feature(GUILD, None, MANAGER, "antiraid", bot=bot))
    assert g.edit.await_args.kwargs["verification_level"] == discord.VerificationLevel.medium
    assert ar.db.deleted


def test_sweeper_query_and_cog_registers_one_command(ar):
    cog = ar.mod.AntiRaidCog(ar.bot)
    cmds = [c for c in cog.get_app_commands()]
    assert [c.name for c in cmds] == ["antiraid"]


def test_a_joiner_that_blows_up_never_stops_the_alert(ar):
    ar.db.cfg["joiner_action"] = "timeout"
    bad, good = make_member(10), make_member(11)
    bad.timeout = AsyncMock(side_effect=RuntimeError("boom"))
    g = make_guild([bad, good])
    ok, _ = run(ar.mod.start_raid(ar.bot, g, joined_ids=[10, 11]))
    assert ok
    good.timeout.assert_awaited_once()
    g.log.send.assert_awaited()                                # staff still got told
