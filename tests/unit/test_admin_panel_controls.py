"""Owner-panel Batch 1 tests: audit log, kill switches, blacklist, premium + the enforcement gate."""
import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, OTHER = 111, 222
MAIN = "discord_bot.cogs._views_admin_panel"
MOD = "discord_bot.cogs._views_admin_panel_controls"
AC = "modules.admin_controls"
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    cfg.PREMIUM_GRACE_DAYS = 3
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    dbm.db.activate_guild_premium = AsyncMock(return_value=NOW + timedelta(days=30))
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in (MAIN, MOD, AC):
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(MOD)
    mod.main = main
    mod.ac = importlib.import_module(AC)
    yield mod
    for name in (MAIN, MOD, AC):
        sys.modules.pop(name, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None, guild_id=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = guild_id
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    i.response.is_done = MagicMock(return_value=False)
    i.followup.edit_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def cog():
    return MagicMock()


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def audit_row(n=1, action="payments.approve", details="ref='x'", guild=None):
    return {"id": n, "admin_id": OWNER, "action": action, "guild_id": guild, "details": details, "created_at": NOW}


def premium_row(guild=1001, clone=None, days=10, name="Anime Hub"):
    return {"guild_id": guild, "clone_id": clone, "expires_at": NOW + timedelta(days=days), "guild_name": name}


# ── fake DB pool for the real admin_controls helpers ─────────────────────

class FakeConn:
    def __init__(self, switches=(), blacklist=(), raise_on_fetch=False):
        self.switches, self.blacklist, self.raise_on_fetch = list(switches), list(blacklist), raise_on_fetch
        self.executed = []

    async def fetch(self, sql, *a):
        if self.raise_on_fetch:
            raise RuntimeError("db down")
        if "bot_kill_switches" in sql:
            return [{"switch": s} for s in self.switches]
        if "bot_blacklist" in sql:
            return [{"kind": k, "target_id": t} for k, t in self.blacklist]
        return []

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        return "DELETE 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False
        return Ctx()


def gate(vp, conn, monkeypatch):
    ac = vp.ac
    monkeypatch.setattr(ac, "_pool", AsyncMock(return_value=FakePool(conn)))
    ac.invalidate()
    ac._snapshot["ok"] = False
    return ac


def slash(user=OTHER, guild_id=5000, qualified="aichat"):
    i = I(user=user, guild_id=guild_id)
    i.command = MagicMock()
    i.command.qualified_name = qualified
    return i


# ── enforcement gate ─────────────────────────────────────────────────────

def test_nothing_is_blocked_by_default(vp, monkeypatch):
    ac = gate(vp, FakeConn(), monkeypatch)
    assert run(ac.block_reason(slash())) is None


def test_blacklisted_user_is_blocked_everywhere_including_dms(vp, monkeypatch):
    ac = gate(vp, FakeConn(blacklist=[("user", OTHER)]), monkeypatch)
    assert run(ac.block_reason(slash())) == ac.MSG_USER
    assert run(ac.block_reason(slash(guild_id=None))) == ac.MSG_USER


def test_blacklisted_server_is_blocked_but_not_others(vp, monkeypatch):
    ac = gate(vp, FakeConn(blacklist=[("guild", 5000)]), monkeypatch)
    assert run(ac.block_reason(slash(guild_id=5000))) == ac.MSG_GUILD
    ac.invalidate()
    assert run(ac.block_reason(slash(guild_id=6000))) is None


def test_maintenance_blocks_everything_for_non_owners(vp, monkeypatch):
    ac = gate(vp, FakeConn(switches=["maintenance"]), monkeypatch)
    assert run(ac.block_reason(slash(qualified="rank"))) == ac.MSG_MAINTENANCE


def test_feature_switch_blocks_only_its_own_commands(vp, monkeypatch):
    ac = gate(vp, FakeConn(switches=["ai"]), monkeypatch)
    assert "AI chat & images" in run(ac.block_reason(slash(qualified="aichat")))
    assert "AI chat & images" in run(ac.block_reason(slash(qualified="aiimage generate")))
    assert run(ac.block_reason(slash(qualified="rank"))) is None
    assert run(ac.block_reason(slash(qualified="totally-unknown"))) is None


def test_owners_are_never_blocked(vp, monkeypatch):
    ac = gate(vp, FakeConn(switches=["maintenance", "ai"], blacklist=[("user", OWNER)]), monkeypatch)
    assert run(ac.block_reason(slash(user=OWNER))) is None


def test_gate_fails_open_when_the_database_is_down(vp, monkeypatch):
    ac = gate(vp, FakeConn(raise_on_fetch=True), monkeypatch)
    assert run(ac.block_reason(slash())) is None


def test_gate_keeps_last_good_snapshot_when_refresh_fails(vp, monkeypatch):
    conn = FakeConn(blacklist=[("user", OTHER)])
    ac = gate(vp, conn, monkeypatch)
    assert run(ac.block_reason(slash())) == ac.MSG_USER
    conn.raise_on_fetch = True
    ac._snapshot["ts"] = 0.0   # force a refresh attempt
    assert run(ac.block_reason(slash())) == ac.MSG_USER


def test_every_feature_has_a_label_and_at_least_one_command(vp):
    for key, (label, roots) in vp.ac.FEATURES.items():
        assert label and (roots or key in vp.ac.WEB_ONLY_FEATURES), key      # only named website-only switches may have no commands
    assert vp.ac.WEB_ONLY_FEATURES <= set(vp.ac.FEATURES)


def test_set_switch_rejects_unknown_names(vp, monkeypatch):
    ac = gate(vp, FakeConn(), monkeypatch)
    with pytest.raises(ValueError):
        run(ac.set_switch("nope", True, OWNER))


def test_set_switch_and_blacklist_changes_apply_immediately(vp, monkeypatch):
    conn = FakeConn()
    ac = gate(vp, conn, monkeypatch)
    run(ac.set_switch("ai", True, OWNER))
    assert ac._snapshot["ts"] == 0.0           # cache dropped so the change is seen at once
    run(ac.add_blacklist("user", 99, "spam", OWNER))
    assert any("bot_blacklist" in sql for sql, _ in conn.executed)
    with pytest.raises(ValueError):
        run(ac.add_blacklist("planet", 1, "", OWNER))


def test_global_check_raises_control_blocked(vp, monkeypatch):
    guard = importlib.import_module("discord_bot.cogs._perm_guard")
    monkeypatch.setattr(vp.ac, "block_reason", AsyncMock(return_value="nope"))
    with pytest.raises(guard.ControlBlocked) as e:
        run(guard.global_interaction_check(slash()))
    assert e.value.message == "nope"
    monkeypatch.setattr(vp.ac, "block_reason", AsyncMock(return_value=None))
    assert run(guard.global_interaction_check(slash(guild_id=None))) is True


# ── access / navigation ──────────────────────────────────────────────────

def test_new_sections_follow_the_clone_admin_allowlist(vp):
    new = {"controls", "blacklist", "premium", "audit"}
    assert new <= vp.main.allowed_sections(OWNER)
    assert not (new & vp.main.allowed_sections(OTHER))


@pytest.mark.parametrize("label,cls", [("Kill switches", "ControlsView"), ("Blacklist", "BlacklistView"),
                                        ("Premium", "PremiumView"), ("Audit log", "AuditView")])
def test_home_opens_each_new_screen(vp, monkeypatch, label, cls):
    ac = vp.ac
    monkeypatch.setattr(ac, "recent_audit", AsyncMock(return_value=[]))
    monkeypatch.setattr(ac, "get_engaged_switches", AsyncMock(return_value=set()))
    monkeypatch.setattr(ac, "list_blacklist", AsyncMock(return_value=[]))
    monkeypatch.setattr(ac, "list_premium", AsyncMock(return_value=[]))

    async def go():
        i = I()
        await buttons(vp.main.HomeView(cog(), OWNER))[label].callback(i)
        assert isinstance(i.response.edit_message.await_args.kwargs["view"], getattr(vp, cls))
    run(go())


def test_home_buttons_disabled_without_access(vp, monkeypatch):
    monkeypatch.setattr(vp.main, "allowed_sections", lambda uid: {"feedback"})
    b = buttons(vp.main.HomeView(cog(), OWNER))
    for label in ("Kill switches", "Blacklist", "Premium", "Audit log"):
        assert b[label].disabled


def test_every_new_screen_renders_within_discord_limits(vp):
    views = [vp.AuditView(cog(), OWNER), vp.ControlsView(cog(), OWNER), vp.BlacklistView(cog(), OWNER),
             vp.PremiumView(cog(), OWNER)]
    views[0].rows = [audit_row(n) for n in range(20)]
    views[2].rows = [{"kind": "user", "target_id": 10 ** 17 + n, "reason": "r" * 300, "added_by": OWNER,
                      "created_at": NOW} for n in range(25)]
    views[3].rows = [premium_row(10 ** 17 + n, name="N" * 200) for n in range(25)]
    for v in views:
        v._build()
        assert v.to_components()
        assert len(text_of(v)) < 4000
        for s in selects(v):
            assert len(s.options) <= 25
            assert all(len(o.label) <= 100 for o in s.options)


def test_blocks_non_opener_and_revoked_access(vp, monkeypatch):
    async def go():
        v = vp.ControlsView(cog(), OWNER)
        i = I(user=OTHER)
        assert await v.interaction_check(i) is False
        monkeypatch.setattr(vp.main, "allowed_sections", lambda uid: {"servers"})
        assert await v.interaction_check(I()) is False
    run(go())


# ── audit log ────────────────────────────────────────────────────────────

def test_audit_view_lists_rows_and_empty_state(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "recent_audit", AsyncMock(return_value=[audit_row(1, "premium.revoke", "guild=1")]))
        v = vp.AuditView(cog(), OWNER)
        await v.load()
        t = text_of(v)
        assert "premium.revoke" in t and f"<@{OWNER}>" in t
        monkeypatch.setattr(vp.ac, "recent_audit", AsyncMock(return_value=[]))
        await v.load()
        assert "No panel actions recorded yet" in text_of(v)
    run(go())


def test_audit_view_survives_a_database_error(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "recent_audit", AsyncMock(side_effect=RuntimeError("db")))
        v = vp.AuditView(cog(), OWNER)
        await v.load()
        assert "Couldn't load" in text_of(v)
    run(go())


def test_audit_limit_select_reloads_with_new_limit(vp, monkeypatch):
    async def go():
        rec = AsyncMock(return_value=[])
        monkeypatch.setattr(vp.ac, "recent_audit", rec)
        v = vp.AuditView(cog(), OWNER)
        i = I(values=["20"])
        await selects(v)[0].callback(i)
        rec.assert_awaited_with(20)
        i.response.edit_message.assert_awaited_once()
    run(go())


def test_audit_long_rows_are_cut_to_the_text_budget(vp):
    lines = ["x" * 400 for _ in range(30)]
    out = vp._fit(lines)
    assert sum(len(l) for l in out) < vp.TEXT_BUDGET + 100
    assert "more not shown" in out[-1]


def test_panel_actions_are_saved_to_the_database(vp, monkeypatch):
    async def go():
        rec = AsyncMock()
        monkeypatch.setattr(vp.ac, "record_audit", rec)
        vp.main.audit(I(guild_id=7), "premium.revoke", guild=1)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        rec.assert_awaited_once()
        assert rec.await_args.args[:3] == (OWNER, "premium.revoke", 7)
        assert "guild=1" in rec.await_args.args[3]
    run(go())


def test_audit_never_raises_without_a_running_loop(vp, monkeypatch):
    monkeypatch.setattr(vp.ac, "record_audit", AsyncMock())
    vp.main.audit(I(), "x.y")   # no loop running: must not raise


# ── kill switches ────────────────────────────────────────────────────────

def test_controls_shows_state_and_labels(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "get_engaged_switches", AsyncMock(return_value={"ai"}))
        v = vp.ControlsView(cog(), OWNER)
        await v.load()
        assert "AI chat & images: OFF" in buttons(v)
        assert "Ads & marketplace: on" in buttons(v)
        assert "⛔ OFF — AI chat & images" in text_of(v)
    run(go())


def test_feature_toggle_flips_the_switch_and_logs(vp, monkeypatch):
    async def go():
        engaged = set()
        async def get(): return set(engaged)
        async def setter(k, e, by):
            (engaged.add if e else engaged.discard)(k)
        monkeypatch.setattr(vp.ac, "get_engaged_switches", get)
        sw = AsyncMock(side_effect=setter)
        monkeypatch.setattr(vp.ac, "set_switch", sw)
        v = vp.ControlsView(cog(), OWNER)
        await v.load()
        i = I(); await buttons(v)["AI chat & images: on"].callback(i)
        sw.assert_awaited_with("ai", True, OWNER)
        assert "AI chat & images: OFF" in buttons(v)
        i2 = I(); await buttons(v)["AI chat & images: OFF"].callback(i2)
        sw.assert_awaited_with("ai", False, OWNER)
    run(go())


def test_maintenance_on_is_two_step_and_off_is_immediate(vp, monkeypatch):
    async def go():
        engaged = set()
        async def get(): return set(engaged)
        async def setter(k, e, by):
            (engaged.add if e else engaged.discard)(k)
        monkeypatch.setattr(vp.ac, "get_engaged_switches", get)
        sw = AsyncMock(side_effect=setter)
        monkeypatch.setattr(vp.ac, "set_switch", sw)
        v = vp.ControlsView(cog(), OWNER)
        await v.load()
        await buttons(v)["Maintenance mode"].callback(I())
        sw.assert_not_awaited()                          # first press only arms it
        assert "Press Confirm" in text_of(v)
        await buttons(v)["Confirm maintenance ON"].callback(I())
        sw.assert_awaited_with("maintenance", True, OWNER)
        assert "Maintenance mode is ON" in text_of(v)
        await buttons(v)["Turn maintenance OFF"].callback(I())
        sw.assert_awaited_with("maintenance", False, OWNER)
    run(go())


def test_switch_change_failure_is_reported_not_crashed(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "get_engaged_switches", AsyncMock(return_value=set()))
        monkeypatch.setattr(vp.ac, "set_switch", AsyncMock(side_effect=RuntimeError("db")))
        v = vp.ControlsView(cog(), OWNER)
        await v.load()
        i = I(); await buttons(v)["AI chat & images: on"].callback(i)
        assert "Nothing was changed" in i.response.send_message.await_args.args[0]
    run(go())


# ── blacklist ────────────────────────────────────────────────────────────

def _bl_modal(vp, kind, target, reason=""):
    m = vp.BlacklistAddModal(cog(), kind)
    m.target._value, m.reason._value = target, reason
    return m


def test_blacklist_add_user_saves_and_refreshes(vp, monkeypatch):
    async def go():
        add = AsyncMock()
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        monkeypatch.setattr(vp.ac, "list_blacklist", AsyncMock(return_value=[
            {"kind": "user", "target_id": 123456789012345678, "reason": "spam", "added_by": OWNER, "created_at": NOW}]))
        i = I()
        await _bl_modal(vp, "user", " 123456789012345678 ", "spam").on_submit(i)
        add.assert_awaited_once_with("user", 123456789012345678, "spam", OWNER)
        view = i.response.edit_message.await_args.kwargs["view"]
        assert "Blocked user" in text_of(view) and "123456789012345678" in text_of(view)
    run(go())


@pytest.mark.parametrize("bad", ["", "abc", "123", "12345678901234567890123456"])
def test_blacklist_rejects_bad_ids(vp, monkeypatch, bad):
    async def go():
        add = AsyncMock()
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        i = I(); await _bl_modal(vp, "guild", bad).on_submit(i)
        add.assert_not_awaited()
        assert "Discord ID" in i.response.send_message.await_args.args[0]
    run(go())


def test_blacklist_refuses_to_block_an_owner(vp, monkeypatch):
    async def go():
        add = AsyncMock()
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        monkeypatch.setattr(vp.ac, "list_blacklist", AsyncMock(return_value=[]))
        vp.DISCORD_CLONE_ADMIN_IDS.add(123456789012)
        i = I(); await _bl_modal(vp, "user", "123456789012").on_submit(i)
        add.assert_not_awaited()
        assert "owner" in i.response.send_message.await_args.args[0]
        i2 = I(); await _bl_modal(vp, "user", "999999999999").on_submit(i2)   # a non-owner id is fine
        add.assert_awaited_once()
    run(go())


def test_blacklist_modal_rechecks_access(vp, monkeypatch):
    async def go():
        add = AsyncMock()
        monkeypatch.setattr(vp.ac, "add_blacklist", add)
        i = I(user=OTHER); await _bl_modal(vp, "guild", "123456789012345678").on_submit(i)
        add.assert_not_awaited()
        assert "no longer authorized" in i.response.send_message.await_args.args[0]
    run(go())


def test_blacklist_pick_and_unblock(vp, monkeypatch):
    async def go():
        rows = [{"kind": "guild", "target_id": 555555555555, "reason": "abuse", "added_by": OWNER, "created_at": NOW}]
        monkeypatch.setattr(vp.ac, "list_blacklist", AsyncMock(side_effect=[rows, []]))
        rm = AsyncMock(return_value=True)
        monkeypatch.setattr(vp.ac, "remove_blacklist", rm)
        v = vp.BlacklistView(cog(), OWNER)
        await v.load()
        assert buttons(v)["Unblock selected"].disabled
        await selects(v)[0].callback(I(values=["guild:555555555555"]))
        assert not buttons(v)["Unblock selected"].disabled
        await buttons(v)["Unblock selected"].callback(I())
        rm.assert_awaited_once_with("guild", 555555555555)
        assert "Nobody is blocked" in text_of(v)
    run(go())


def test_blacklist_buttons_open_the_right_form(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_blacklist", AsyncMock(return_value=[]))
        v = vp.BlacklistView(cog(), OWNER); await v.load()
        i = I(); await buttons(v)["Block a person"].callback(i)
        assert i.response.send_modal.await_args.args[0].kind == "user"
        i = I(); await buttons(v)["Block a server"].callback(i)
        assert i.response.send_modal.await_args.args[0].kind == "guild"
    run(go())


# ── premium manager ──────────────────────────────────────────────────────

def test_premium_lists_servers_and_flags_grace(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(return_value=[
            premium_row(1001, None, 10), premium_row(1002, 7, -1, "Old Server")]))
        v = vp.PremiumView(cog(), OWNER); await v.load()
        t = text_of(v)
        assert "Anime Hub" in t and "clone #7" in t and "in grace period" in t
        assert buttons(v)["+30 days"].disabled and buttons(v)["Revoke"].disabled
    run(go())


def test_premium_extend_main_bot_uses_no_clone(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(return_value=[premium_row(1001, None, 10)]))
        v = vp.PremiumView(cog(), OWNER); await v.load()
        await selects(v)[0].callback(I(values=["1001:0"]))
        await buttons(v)["+30 days"].callback(I())
        vp.db.activate_guild_premium.assert_awaited_once_with(1001, OWNER, 30, None)
        assert "Added 30 days" in text_of(v)
    run(go())


def test_premium_extend_clone_keeps_clone_id(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(return_value=[premium_row(1002, 7, 10)]))
        v = vp.PremiumView(cog(), OWNER); await v.load()
        await selects(v)[0].callback(I(values=["1002:7"]))
        await buttons(v)["+90 days"].callback(I())
        vp.db.activate_guild_premium.assert_awaited_once_with(1002, OWNER, 90, 7)
    run(go())


def test_premium_revoke_is_two_step(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(side_effect=[[premium_row(1001, None, 10)], []]))
        rv = AsyncMock(return_value=True)
        monkeypatch.setattr(vp.ac, "revoke_premium", rv)
        v = vp.PremiumView(cog(), OWNER); await v.load()
        await selects(v)[0].callback(I(values=["1001:0"]))
        await buttons(v)["Revoke"].callback(I())
        rv.assert_not_awaited()
        assert "Press Confirm" in text_of(v)
        await buttons(v)["Confirm revoke"].callback(I())
        rv.assert_awaited_once_with(1001, None)
        assert "Ended premium" in text_of(v)
    run(go())


def test_changing_server_disarms_revoke(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(return_value=[premium_row(1001), premium_row(1002)]))
        v = vp.PremiumView(cog(), OWNER); await v.load()
        await selects(v)[0].callback(I(values=["1001:0"]))
        await buttons(v)["Revoke"].callback(I())
        await selects(v)[0].callback(I(values=["1002:0"]))
        assert "Confirm revoke" not in buttons(v)
    run(go())


def _grant(vp, g, d, c=""):
    m = vp.GrantPremiumModal(cog())
    m.guild._value, m.days._value, m.clone._value = g, d, c
    return m


def test_grant_modal_validates_and_grants(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(return_value=[]))
        for bad in (("abc", "30", ""), ("123456789012345678", "0", ""), ("123456789012345678", "99999", ""),
                    ("123456789012345678", "30", "x")):
            i = I(); await _grant(vp, *bad).on_submit(i)
            i.response.send_message.assert_awaited_once()
        vp.db.activate_guild_premium.assert_not_awaited()
        i = I(); await _grant(vp, "123456789012345678", "45", "").on_submit(i)
        vp.db.activate_guild_premium.assert_awaited_once_with(123456789012345678, OWNER, 45, None)
        assert "Added 45 day" in text_of(i.response.edit_message.await_args.kwargs["view"])
        vp.db.activate_guild_premium.reset_mock()
        i = I(); await _grant(vp, "123456789012345678", "7", "3").on_submit(i)
        vp.db.activate_guild_premium.assert_awaited_once_with(123456789012345678, OWNER, 7, 3)
    run(go())


def test_grant_modal_rechecks_access(vp):
    async def go():
        i = I(user=OTHER); await _grant(vp, "123456789012345678", "30").on_submit(i)
        vp.db.activate_guild_premium.assert_not_awaited()
        assert "no longer authorized" in i.response.send_message.await_args.args[0]
    run(go())


def test_premium_database_error_is_reported(vp, monkeypatch):
    async def go():
        monkeypatch.setattr(vp.ac, "list_premium", AsyncMock(side_effect=RuntimeError("db")))
        v = vp.PremiumView(cog(), OWNER); await v.load()
        assert "Couldn't load" in text_of(v)
    run(go())
