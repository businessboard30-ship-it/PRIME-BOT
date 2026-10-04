"""Server Owners Panel Phase 9: join gate and per-server Scam Shield (logic, listener, panel screens)."""
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

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
SP, SPP = "modules.server_panel", "modules.server_panel_protection"
JG, SS = "modules.join_gate", "modules.scam_shield"
V1, V2, V4, V5 = (f"discord_bot.cogs._views_server_panel{x}" for x in ("", "_p2", "_p4", "_p5"))
COG_JG, COG_SS = "discord_bot.cogs.join_gate", "discord_bot.cogs.scam_shield"
NAMES = (SP, SPP, JG, SS, V1, V2, V4, V5, COG_JG, COG_SS)


@pytest.fixture()
def env(monkeypatch):
    dbm = types.ModuleType("database")
    db = make_db()
    db.get_join_gate_config = AsyncMock(return_value={
        "enabled": True, "min_age_days": 7, "block_default_avatar": False, "action": "alert",
        "blocked_count": 4})
    db.set_join_gate_config = AsyncMock()
    db.bump_join_gate_blocked = AsyncMock()
    db.get_scam_shield_guild = AsyncMock(return_value={"enabled": True, "allowed_domains": ["good.com"]})
    db.set_scam_shield_guild = AsyncMock()
    db.get_automod_config = AsyncMock(return_value={
        "action": "delete", "timeout_minutes": 10, "log_channel_id": 77, "banned_words": [],
        "word_filter_enabled": False, "anti_invite_enabled": False,
        "anti_mention_enabled": False, "spam_enabled": False})
    db.get_antiraid_config = AsyncMock(return_value={})
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
    mods = {k: importlib.import_module(n) for k, n in
            dict(sp=SP, spp=SPP, jg=JG, ss=SS, v1=V1, v2=V2, v4=V4, v5=V5, cj=COG_JG, cs=COG_SS).items()}
    audit = AsyncMock()
    monkeypatch.setattr(mods["sp"], "record_change", audit)
    monkeypatch.setattr(mods["spp"], "record_change", audit)
    mods["cj"]._cache.clear()
    mods["ss"]._guild_cache.clear()
    yield SimpleNamespace(db=db, audit=audit, **mods)
    for n in NAMES:
        sys.modules.pop(n, None)


# ── join gate logic ──────────────────────────────────────────────────────

CFG = {"enabled": True, "min_age_days": 7, "block_default_avatar": False}


def test_young_account_is_caught_old_one_is_not(env):
    assert env.jg.reasons_for(NOW - timedelta(days=2), True, CFG, NOW)
    assert env.jg.reasons_for(NOW - timedelta(days=8), True, CFG, NOW) == []
    assert env.jg.reasons_for(NOW - timedelta(days=7), True, CFG, NOW) == []  # exactly at the limit is fine


def test_default_avatar_only_when_switched_on(env):
    old = NOW - timedelta(days=100)
    assert env.jg.reasons_for(old, False, CFG, NOW) == []
    assert env.jg.reasons_for(old, False, dict(CFG, block_default_avatar=True), NOW)


def test_disabled_gate_and_zero_age_catch_nothing(env):
    assert env.jg.reasons_for(NOW, False, dict(CFG, enabled=False, block_default_avatar=True), NOW) == []
    assert env.jg.reasons_for(NOW, True, dict(CFG, min_age_days=0), NOW) == []


def test_both_reasons_reported(env):
    r = env.jg.reasons_for(NOW - timedelta(hours=3), False, dict(CFG, block_default_avatar=True), NOW)
    assert len(r) == 2 and "3 hours" in r[0]


@pytest.mark.parametrize("v,ok", [("0", 0), ("30", 30), ("365", 365), ("366", None), ("-1", None), ("x", None), ("", None)])
def test_validate_min_age(env, v, ok):
    assert env.jg.validate_min_age(v) == ok


# ── join gate listener ───────────────────────────────────────────────────

def joiner(age_days=1, bot=False, avatar=True, top=1):
    m = MagicMock()
    m.id, m.bot = 900, bot
    m.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    m.avatar = object() if avatar else None
    m.kick = AsyncMock()
    m.top_role = top
    m.guild.id = GUILD_ID
    m.guild.me.guild_permissions.kick_members = True
    m.guild.me.top_role = 10
    ch = MagicMock()
    ch.send = AsyncMock()
    m.guild.get_channel = MagicMock(return_value=ch)
    return m, ch


def test_listener_alerts_without_kicking(env):
    cog = env.cj.JoinGateCog(MagicMock(clone_id=7))
    m, ch = joiner(age_days=1)
    run(cog.on_member_join(m))
    m.kick.assert_not_awaited()
    ch.send.assert_awaited_once()
    assert env.db.bump_join_gate_blocked.await_args.kwargs["clone_id"] == 7


def test_listener_kicks_when_configured(env):
    env.db.get_join_gate_config = AsyncMock(return_value=dict(CFG, action="kick"))
    cog = env.cj.JoinGateCog(MagicMock(clone_id=None))
    m, ch = joiner(age_days=1)
    run(cog.on_member_join(m))
    m.kick.assert_awaited_once()
    assert "kicked" in ch.send.await_args.kwargs["embed"].fields[2].value


def test_listener_does_not_kick_above_my_role(env):
    env.db.get_join_gate_config = AsyncMock(return_value=dict(CFG, action="kick"))
    cog = env.cj.JoinGateCog(MagicMock(clone_id=None))
    m, ch = joiner(age_days=1, top=99)
    run(cog.on_member_join(m))
    m.kick.assert_not_awaited()
    ch.send.assert_awaited_once()


def test_listener_ignores_bots_old_accounts_and_disabled(env):
    cog = env.cj.JoinGateCog(MagicMock(clone_id=None))
    for m, _ in (joiner(age_days=1, bot=True), joiner(age_days=30)):
        run(cog.on_member_join(m))
    env.cj._cache.clear()
    env.db.get_join_gate_config = AsyncMock(return_value=dict(CFG, enabled=False))
    m, ch = joiner(age_days=1)
    run(cog.on_member_join(m))
    ch.send.assert_not_awaited()
    env.db.bump_join_gate_blocked.assert_not_awaited()


def test_listener_survives_database_failure(env):
    env.db.get_join_gate_config = AsyncMock(side_effect=RuntimeError("down"))
    cog = env.cj.JoinGateCog(MagicMock(clone_id=None))
    m, _ = joiner(age_days=1)
    run(cog.on_member_join(m))   # must not raise
    m.kick.assert_not_awaited()


def test_listener_config_is_cached_until_panel_invalidates(env):
    cog = env.cj.JoinGateCog(MagicMock(clone_id=None))
    for _ in range(3):
        run(cog.on_member_join(joiner(age_days=30)[0]))
    assert env.db.get_join_gate_config.await_count == 1
    env.cj.invalidate(GUILD_ID, None)
    run(cog.on_member_join(joiner(age_days=30)[0]))
    assert env.db.get_join_gate_config.await_count == 2


# ── scam shield per-server ───────────────────────────────────────────────

def load_rules(ss):
    ss._c.domains = [(1, "bad.com")]
    ss._c.words = []
    ss._c.images = []


def test_allowed_domain_is_skipped_but_others_still_match(env):
    load_rules(env.ss)
    text = "see https://bad.com/x and https://sub.bad.com/y"
    assert env.ss.match_text(text)[:2] == ("domain", "bad.com")
    assert env.ss.match_text(text, ("bad.com",)) is None            # covers subdomains too
    env.ss._c.domains = [(1, "bad.com"), (2, "evil.net")]
    assert env.ss.match_text("bad.com evil.net", ("bad.com",))[1] == "evil.net"


def test_allowed_domains_never_unblock_words_or_heuristics(env):
    env.ss._c.domains, env.ss._c.images = [], []
    env.ss._c.words = [(5, "free nitro")]
    assert env.ss.match_text("FREE NITRO here", ("bad.com",))[0] == "word"
    assert env.ss.match_text("MrBeast casino promo code", ("bad.com",))[0] == "heuristic"


def test_guild_settings_default_on_when_database_fails_and_cached(env):
    assert run(env.ss.guild_settings(GUILD_ID, None)) == {"enabled": True, "allowed_domains": ()}
    assert (GUILD_ID, None) in env.ss._guild_cache


def test_invalidate_guild_drops_cache(env):
    env.ss._guild_cache[(GUILD_ID, 3)] = (1e18, {"enabled": False, "allowed_domains": ()})
    env.ss.invalidate_guild(GUILD_ID, 3)
    assert (GUILD_ID, 3) not in env.ss._guild_cache


def msg(content="https://bad.com/x"):
    m = MagicMock()
    m.guild.id = GUILD_ID
    m.guild.owner_id = 1
    m.type = discord.MessageType.default
    m.author = MagicMock(spec=discord.Member)
    m.author.id = 900
    m.author.guild_permissions = discord.Permissions.none()
    m.content, m.embeds, m.attachments = content, [], []
    m.delete = AsyncMock()
    return m


def shield_cog(env, settings):
    load_rules(env.ss)
    env.ss._c.enabled = True
    env.ss._c.loaded_at = 1e18
    env.ss.guild_settings = AsyncMock(return_value=settings)
    cog = env.cs.ScamShieldCog.__new__(env.cs.ScamShieldCog)
    cog.bot = MagicMock(user=MagicMock(id=5), clone_id=None)
    cog._act = AsyncMock()
    return cog


def test_cog_skips_when_server_switched_it_off(env):
    cog = shield_cog(env, {"enabled": False, "allowed_domains": ()})
    run(cog._inspect(msg()))
    cog._act.assert_not_awaited()


def test_cog_skips_allowed_domain_and_acts_otherwise(env):
    cog = shield_cog(env, {"enabled": True, "allowed_domains": ("bad.com",)})
    run(cog._inspect(msg()))
    cog._act.assert_not_awaited()
    cog = shield_cog(env, {"enabled": True, "allowed_domains": ()})
    run(cog._inspect(msg()))
    cog._act.assert_awaited_once()


def test_cog_does_not_read_server_settings_for_clean_messages(env):
    cog = shield_cog(env, {"enabled": True, "allowed_domains": ()})
    run(cog._inspect(msg("hello everyone")))
    env.ss.guild_settings.assert_not_awaited()


# ── panel data helpers ───────────────────────────────────────────────────

def test_clean_domains_parses_dedupes_and_rejects(env):
    added, rejected, final = env.spp.clean_domains("https://www.Good.com/x\nnew.org, junk\nnew.org", ["good.com"])
    assert added == ["new.org"] and rejected == ["junk"] and final == ["good.com", "new.org"]


def test_clean_domains_respects_cap(env):
    existing = [f"d{n}.com" for n in range(env.ss.MAX_ALLOWED_DOMAINS)]
    added, rejected, final = env.spp.clean_domains("extra.com", existing)
    assert added == [] and rejected == ["extra.com"] and len(final) == env.ss.MAX_ALLOWED_DOMAINS


def test_set_join_gate_validates_and_never_writes_bad_values(env):
    assert run(env.spp.set_join_gate(GUILD_ID, None, OWNER, min_age_days=9999))
    assert run(env.spp.set_join_gate(GUILD_ID, None, OWNER, action="ban"))
    env.db.set_join_gate_config.assert_not_awaited()


def test_set_join_gate_writes_with_clone_audit_and_drops_cache(env):
    env.cj._cache[(GUILD_ID, 7)] = (1e18, {})
    assert run(env.spp.set_join_gate(GUILD_ID, 7, OWNER, action="kick")) is None
    assert env.db.set_join_gate_config.await_args.kwargs == {"clone_id": 7, "action": "kick"}
    assert env.audit.await_args.args[3] == "join_gate.action"
    assert (GUILD_ID, 7) not in env.cj._cache


# ── screens ──────────────────────────────────────────────────────────────

def test_moderation_has_protection_button_and_stays_in_limits(env):
    v = run(env.v1.ModerationView.create(interaction()))
    assert "Join gate & Scam Shield" in buttons(v)
    assert len(list(v.walk_children())) < 40


@pytest.mark.parametrize("cls", ["ProtectionView", "JoinGateView", "ScamShieldView"])
def test_screens_in_limits_with_hint(env, cls):
    v = run(getattr(env.v5, cls).create(interaction()))
    assert len(list(v.walk_children())) < 40
    assert "/serversetup" in text_of(v)


def test_protection_hub_summarises(env):
    t = text_of(run(env.v5.ProtectionView.create(interaction())))
    assert "Join gate" in t and "**4**" in t and "Allowed domains: **1**" in t


def test_join_gate_toggle_needs_log_channel(env):
    env.db.get_join_gate_config = AsyncMock(return_value=dict(CFG, enabled=False, action="alert"))
    env.db.get_automod_config = AsyncMock(return_value={"log_channel_id": None, "banned_words": []})
    i = interaction()
    v = run(env.v5.JoinGateView.create(i))
    run(buttons(v)["Turn on"].callback(i))
    i.response.send_message.assert_awaited()
    env.db.set_join_gate_config.assert_not_awaited()


def test_join_gate_toggle_off_writes_with_clone(env):
    i = interaction(clone_id=7)
    v = run(env.v5.JoinGateView.create(i))
    run(buttons(v)["Turn off"].callback(i))
    assert env.db.set_join_gate_config.await_args.kwargs == {"clone_id": 7, "enabled": False}


def test_kick_action_needs_kick_permission(env):
    i = interaction()
    i.guild.me.guild_permissions.kick_members = False
    v = run(env.v5.JoinGateView.create(i))
    sel = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select) and "caught member" in (c.placeholder or ""))
    sel._values = ["kick"]
    run(sel.callback(i))
    env.db.set_join_gate_config.assert_not_awaited()


def test_min_age_modal_rejects_bad_and_accepts_good(env):
    i = interaction(clone_id=7)
    modal = env.v5.MinAgeModal(7, OWNER)
    modal.days._value = "abc"
    run(modal.on_submit(i))
    env.db.set_join_gate_config.assert_not_awaited()
    modal.days._value = "21"
    run(modal.on_submit(i))
    assert env.db.set_join_gate_config.await_args.kwargs["min_age_days"] == 21


def test_modals_deny_non_manager_and_other_user(env):
    for i, opener in ((interaction(user=MEMBER, manage=False), MEMBER), (interaction(user=OWNER), MEMBER)):
        a = env.v5.MinAgeModal(7, opener); a.days._value = "5"
        b = env.v5.AllowDomainsModal(opener); b.domains._value = "x.com"
        run(a.on_submit(i)); run(b.on_submit(i))
    env.db.set_join_gate_config.assert_not_awaited()
    env.db.set_scam_shield_guild.assert_not_awaited()


def test_scam_toggle_writes_and_audits(env):
    i = interaction(clone_id=7)
    v = run(env.v5.ScamShieldView.create(i))
    run(buttons(v)["Turn off here"].callback(i))
    assert env.db.set_scam_shield_guild.await_args.kwargs == {"clone_id": 7, "enabled": False}
    assert env.audit.await_args.args[3] == "scam_shield.enabled"


def test_allow_domains_modal_adds_and_reports(env):
    i = interaction(clone_id=7)
    modal = env.v5.AllowDomainsModal(OWNER)
    modal.domains._value = "new.org\nnot a domain"
    run(modal.on_submit(i))
    kw = env.db.set_scam_shield_guild.await_args.kwargs
    assert kw["allowed_domains"] == ["good.com", "new.org"] and kw["clone_id"] == 7
    assert "Skipped" in i.followup.send.await_args.args[0]


def test_remove_allowed_domain_is_two_step(env):
    i = interaction(clone_id=7)
    v = run(env.v5.ScamShieldView.create(i))
    sel = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select))
    sel._values = ["good.com"]
    run(sel.callback(i))
    env.db.set_scam_shield_guild.assert_not_awaited()
    confirm = i.edit_original_response.await_args.kwargs["view"]
    run(buttons(confirm)["Yes, do it"].callback(i))
    assert env.db.set_scam_shield_guild.await_args.kwargs["allowed_domains"] == []


def test_screens_survive_database_failure(env):
    env.db.get_join_gate_config = AsyncMock(side_effect=RuntimeError("down"))
    env.db.get_scam_shield_guild = AsyncMock(side_effect=RuntimeError("down"))
    for cls in ("ProtectionView", "JoinGateView", "ScamShieldView"):
        assert run(getattr(env.v5, cls).create(interaction()))


def test_only_opener_can_click(env):
    v = run(env.v5.JoinGateView.create(interaction()))
    assert run(v.interaction_check(interaction(user=MEMBER, manage=True))) is False
