"""Anti-raid Pro (premium): profile filter, raid report, quarantine & review, premium gating."""
import asyncio
import importlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

AR = "discord_bot.cogs.antiraid"
PRO = "modules.antiraid_pro"
VIEWS = "discord_bot.cogs._views_antiraid_pro"
GUILD, LOG, ROLE, OWNER = 7000, 7100, 7200, 1
ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class FakeDB:
    def __init__(self, premium=True, **cfg):
        self.premium = premium
        self.cfg = {
            "guild_id": GUILD, "clone_id": None, "enabled": True, "sensitivity": "strict",
            "response": "alert", "joiner_action": "none", "lockdown_minutes": 15,
            "log_channel_id": LOG, "alert_role_id": None, "active_until": None,
            "prev_verification": None, "triggered_count": 0, "last_triggered_at": None,
            "created_by": None, "filter_age_days": 0, "filter_default_avatar": False,
            "filter_suspicious_name": False, "filter_action": "flag",
        }
        self.cfg.update(cfg)
        self.quarantined = []
        self.removed = []

    async def is_guild_premium_active(self, g, c):
        return self.premium

    async def get_antiraid_config(self, g, clone_id=None):
        return dict(self.cfg)

    async def set_antiraid_config(self, g, clone_id=None, **f):
        self.cfg.update(f)
        return dict(self.cfg)

    async def bump_antiraid_triggers(self, g, clone_id=None):
        pass

    async def get_automod_config(self, g, clone_id=None):
        return {"log_channel_id": None}

    async def list_quarantined(self, g, clone_id, limit=25):
        return list(self.quarantined)

    async def get_quarantined(self, g, clone_id, uid):
        return next((r for r in self.quarantined if r["user_id"] == uid), None)

    async def remove_quarantined(self, g, clone_id, uid):
        self.removed.append(uid)
        self.quarantined = [r for r in self.quarantined if r["user_id"] != uid]


def member(uid, age_days=400, avatar=True, name="alice", staff=False, bot=False):
    perms = SimpleNamespace(administrator=False, manage_guild=staff, manage_messages=False,
                            ban_members=False, kick_members=False, moderate_members=False)
    m = SimpleNamespace(
        id=uid, name=name, global_name=None, bot=bot, mention=f"<@{uid}>", top_role=1,
        guild_permissions=perms, timeout=AsyncMock(),
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        avatar=("hash" if avatar else None),
        display_avatar=SimpleNamespace(url="https://x/a.png"), joined_at=datetime.now(timezone.utc))
    return m


def make_guild(members=()):
    g = MagicMock()
    g.id = GUILD
    g.owner_id = OWNER
    g.verification_level = discord.VerificationLevel.medium
    g.edit = AsyncMock()
    g.kick = AsyncMock()
    g.ban = AsyncMock()
    roster = {m.id: m for m in members}
    for m in members:
        m.guild = g
    g.get_member = MagicMock(side_effect=lambda uid: roster.get(uid))
    g.me = SimpleNamespace(guild_permissions=SimpleNamespace(
        manage_guild=True, moderate_members=True, kick_members=True, ban_members=True,
        manage_roles=True, mention_everyone=False), top_role=10)
    g.get_role = MagicMock(return_value=None)
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.mention = "<#7100>"
    g.log = channel
    g.get_channel = MagicMock(side_effect=lambda cid: channel if cid == LOG else None)
    return g


@pytest.fixture()
def ar(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database")
    dbm.db = fake
    dbm.get_pool = AsyncMock(side_effect=RuntimeError("no database in unit tests"))
    monkeypatch.setitem(sys.modules, "database", dbm)
    names = (AR, PRO, VIEWS, "modules.server_panel", "modules.server_panel_quarantine")
    for n in names:
        sys.modules.pop(n, None)
    mod = importlib.import_module(AR)
    pro = mod.pro          # the cog's own reference (the `modules` package attribute can outlive sys.modules pops)
    mod._cache.clear(); mod._joins.clear(); mod._starting.clear()
    mod._last_persist.clear(); mod._last_test.clear()
    pro._premium_cache.clear(); pro._logs.clear(); pro._flag_posts.clear()
    pro.quarantine_suspect = AsyncMock(return_value=(True, "ok"))     # real one is tested separately
    bot = SimpleNamespace(clone_id=None)
    yield SimpleNamespace(mod=mod, pro=pro, db=fake, bot=bot, views=mod.pro_ui)
    for n in names:
        sys.modules.pop(n, None)


# ── pure: filter ──────────────────────────────────────────────────────────

def test_name_flags(ar):
    f = ar.pro.name_flags
    assert f("alice") == []
    assert "invite/link in name" in f("join discord.gg/abc")
    assert "mass-ping bait in name" in f("@everyone")
    assert "scam wording in name" in f("FREE NITRO here")
    assert "long number at the end" in f("user12345678")
    assert f("user123") == []                         # a few digits are normal
    assert "repeated characters" in f("aaaaaaaa")


def test_evaluate_profile_age_avatar_name(ar):
    cfg = {"filter_age_days": 7, "filter_default_avatar": True, "filter_suspicious_name": True}
    assert ar.pro.evaluate_profile(member(1), cfg) == []
    r = ar.pro.evaluate_profile(member(2, age_days=2, avatar=False, name="bot12345678"), cfg)
    assert len(r) == 3
    only_age = {"filter_age_days": 7}
    assert ar.pro.evaluate_profile(member(3, age_days=2, avatar=False), only_age)[0].startswith("account is")
    assert ar.pro.evaluate_profile(member(4, age_days=2), {}) == []        # nothing enabled -> nothing flagged


def test_filter_enabled_and_summary(ar):
    assert not ar.pro.filter_enabled({})
    assert ar.pro.filter_summary({}) == "Off"
    s = ar.pro.filter_summary({"filter_age_days": 3, "filter_default_avatar": True, "filter_action": "kick"})
    assert "under 3 d" in s and "default avatar" in s and "Kick" in s


def test_effective_cfg_pauses_quarantine_for_free(ar):
    cfg = {"joiner_action": "quarantine"}
    assert ar.pro.effective_cfg(cfg, True)["joiner_action"] == "quarantine"
    assert ar.pro.effective_cfg(cfg, False)["joiner_action"] == "none"
    assert cfg["joiner_action"] == "quarantine"                             # saved choice is kept
    assert ar.pro.effective_cfg({"joiner_action": "kick"}, False)["joiner_action"] == "kick"


def test_premium_lookup_fails_safe_and_caches(ar):
    assert run(ar.pro.is_premium(GUILD, None)) is True
    ar.db.premium = False
    assert run(ar.pro.is_premium(GUILD, None)) is True                      # cached 30s
    ar.pro._premium_cache.clear()
    assert run(ar.pro.is_premium(GUILD, None)) is False
    ar.db.is_guild_premium_active = AsyncMock(side_effect=RuntimeError("db down"))
    ar.pro._premium_cache.clear()
    assert run(ar.pro.is_premium(GUILD, None)) is False                     # never raises


def test_flag_post_rate_limit(ar):
    k = (1, None)
    assert all(ar.pro.flag_post_allowed(k, now=100.0 + i) for i in range(5))
    assert not ar.pro.flag_post_allowed(k, now=106.0)
    assert ar.pro.flag_post_allowed(k, now=170.0)


# ── pure: report ──────────────────────────────────────────────────────────

def _log(pro, n=6, age=2):
    t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
    log = pro.start_log((1, None), t0)
    for i in range(n):
        pro.record_join((1, None), member(100 + i, age_days=age, avatar=False, name=f"raider{i}"),
                        "quarantined", ["x"] if i < 2 else None, now=t0 + timedelta(seconds=i))
    return log


def test_report_analysis(ar):
    log = _log(ar.pro)
    assert dict(ar.pro.age_buckets(log.entries)) == {"1–7 days": 6}
    assert ar.pro.name_stems(log.entries) == [("raider", 6)]
    assert ar.pro.peak_window(log.entries)[0] == 6
    assert ar.pro.median_age(log.entries) == pytest.approx(2.0, abs=0.01)
    assert log.filter_hits == 2 and log.outcomes["quarantined"] == 6


def test_report_embed_contents(ar):
    log = _log(ar.pro)
    e = ar.pro.build_report_embed(log, datetime.now(timezone.utc), "Verification put back to **Medium**.", waiting=4)
    text = " ".join([e.description or ""] + [f.name + " " + f.value for f in e.fields])
    assert "Raid report" in e.title
    assert "**6** joined" in text and "Quarantined" in text
    assert "raider" in text and "Default avatar" in text and "Peak" in text
    assert "**4**" in text and "Waiting for your decision" in text


def test_report_without_log_still_posts(ar):
    e = ar.pro.build_report_embed(None, datetime.now(timezone.utc), "x")
    assert "No joiner details" in e.fields[0].value


def test_raid_log_is_bounded(ar):
    k = (2, None)
    for i in range(ar.pro._MAX_ENTRIES + 20):
        ar.pro.record_join(k, member(i), "left_alone")
    log = ar.pro.pop_log(k)
    assert len(log.entries) == ar.pro._MAX_ENTRIES and log.total == ar.pro._MAX_ENTRIES + 20 and log.truncated
    assert ar.pro.pop_log(k) is None


# ── join handling ─────────────────────────────────────────────────────────

def test_filter_is_premium_only(ar):
    ar.db.premium = False
    ar.db.cfg.update(filter_age_days=7, filter_action="kick")
    g = make_guild()
    m = member(10, age_days=1); m.guild = g
    run(ar.mod.handle_join(ar.bot, m))
    g.kick.assert_not_awaited(); g.log.send.assert_not_awaited()


def test_filter_flag_only_posts_and_changes_nothing(ar):
    ar.db.cfg.update(filter_age_days=7, filter_action="flag")
    g = make_guild()
    m = member(10, age_days=1); m.guild = g
    run(ar.mod.handle_join(ar.bot, m))
    g.kick.assert_not_awaited()
    ar.pro.quarantine_suspect.assert_not_awaited()
    embed = g.log.send.await_args.kwargs["embed"]
    assert "Flagged" in embed.title and "account is" in embed.description


def test_filter_kick(ar):
    ar.db.cfg.update(filter_default_avatar=True, filter_action="kick")
    g = make_guild()
    m = member(10, avatar=False); m.guild = g
    run(ar.mod.handle_join(ar.bot, m))
    g.kick.assert_awaited_once()


def test_filter_quarantine_attaches_review_buttons(ar):
    ar.db.cfg.update(filter_age_days=7, filter_action="quarantine")
    g = make_guild()
    m = member(10, age_days=1); m.guild = g
    run(ar.mod.handle_join(ar.bot, m))
    ar.pro.quarantine_suspect.assert_awaited_once()
    assert "[anti-raid]" not in ar.pro.quarantine_suspect.await_args.args[3]      # prefix is added inside
    ids = {c.custom_id for c in g.log.send.await_args.kwargs["view"].children}
    assert ids == {f"arpro:{k}:{GUILD}:-" for k in ("qban", "qrel", "qreview")}


def test_filter_never_touches_staff_or_bots(ar):
    ar.db.cfg.update(filter_age_days=7, filter_action="kick")
    g = make_guild()
    for m in (member(10, age_days=1, staff=True), member(11, age_days=1, bot=True)):
        m.guild = g
        run(ar.mod.handle_join(ar.bot, m))
    g.kick.assert_not_awaited(); g.log.send.assert_not_awaited()


def test_filter_failure_to_quarantine_falls_back_to_flag(ar):
    ar.db.cfg.update(filter_age_days=7, filter_action="quarantine")
    ar.pro.quarantine_suspect.return_value = (False, "no role")
    g = make_guild()
    m = member(10, age_days=1); m.guild = g
    run(ar.mod.handle_join(ar.bot, m))
    embed = g.log.send.await_args.kwargs["embed"]
    assert "Flagged" in embed.title and "no role" in embed.description


# ── raid flow ─────────────────────────────────────────────────────────────

def test_raid_quarantines_joiners_for_premium_and_posts_review_buttons(ar):
    ar.db.cfg["joiner_action"] = "quarantine"
    ms = [member(20 + i, age_days=1) for i in range(4)]
    g = make_guild(ms)
    ok, _ = run(ar.mod.start_raid(ar.bot, g, joined_ids=[m.id for m in ms]))
    assert ok and ar.pro.quarantine_suspect.await_count == 4
    kw = g.log.send.await_args.kwargs
    assert kw["view"] is not None and "quarantine" in kw["embed"].description.lower()
    assert ar.pro.get_log((GUILD, None)).total == 4


def test_free_server_never_quarantines_even_if_saved(ar):
    ar.db.premium = False
    ar.db.cfg["joiner_action"] = "quarantine"
    ms = [member(20 + i) for i in range(4)]
    g = make_guild(ms)
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[m.id for m in ms]))
    ar.pro.quarantine_suspect.assert_not_awaited()
    assert ar.pro.get_log((GUILD, None)) is None            # no log is kept for free servers
    assert g.log.send.await_args.kwargs.get("view") is None


def test_joiner_during_raid_is_logged_and_filter_not_doubled(ar):
    ar.db.cfg.update(joiner_action="quarantine", filter_age_days=7, filter_action="quarantine",
                     active_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    g = make_guild()
    m = member(30, age_days=1); m.guild = g
    run(ar.mod.handle_join(ar.bot, m))
    assert ar.pro.quarantine_suspect.await_count == 1        # filter handled it; raid action skipped
    assert ar.pro.get_log((GUILD, None)).outcomes["quarantined"] == 1


def test_end_raid_premium_posts_report_with_buttons_when_people_wait(ar):
    ar.db.cfg["joiner_action"] = "quarantine"
    ms = [member(40 + i, age_days=1) for i in range(3)]
    g = make_guild(ms)
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[m.id for m in ms]))
    ar.db.quarantined = [{"user_id": m.id, "reason": "[anti-raid] joined during a raid",
                          "created_at": datetime.now(timezone.utc)} for m in ms]
    g.log.send.reset_mock()
    run(ar.mod.end_raid(ar.bot, g))
    kw = g.log.send.await_args.kwargs
    assert "Raid report" in kw["embed"].title and kw["view"] is not None
    assert ar.pro.get_log((GUILD, None)) is None             # log is released


def test_end_raid_free_gets_simple_embed_with_upsell(ar):
    ar.db.premium = False
    g = make_guild([member(50)])
    run(ar.mod.start_raid(ar.bot, g, joined_ids=[50]))
    g.log.send.reset_mock()
    run(ar.mod.end_raid(ar.bot, g))
    e = g.log.send.await_args.kwargs["embed"]
    assert e.title == "✅ Raid mode ended" and "Premium" in e.footer.text


# ── quarantine & review ops ───────────────────────────────────────────────

def _rows():
    now = datetime.now(timezone.utc)
    return [
        {"user_id": 1, "reason": "[anti-raid] joined during a raid", "created_at": now - timedelta(minutes=3)},
        {"user_id": 2, "reason": "spam by hand", "created_at": now - timedelta(minutes=2)},      # staff's own
        {"user_id": 3, "reason": "[anti-raid] profile filter: x", "created_at": now - timedelta(minutes=1)},
    ]


def test_review_only_sees_raid_rows_oldest_first(ar):
    ar.db.quarantined = _rows()
    rows = run(ar.pro.raid_quarantined(make_guild(), None))
    assert [r["user_id"] for r in rows] == [1, 3]


def test_ban_all_and_release_all_skip_manual_quarantines(ar, monkeypatch):
    ar.db.quarantined = _rows()
    g = make_guild()
    monkeypatch.setitem(sys.modules, "modules.server_panel",
                        types.SimpleNamespace(record_change=AsyncMock()))
    msg = run(ar.pro.ban_all(g, None, SimpleNamespace(id=9)))
    assert "Banned 2" in msg and g.ban.await_count == 2
    assert [r["user_id"] for r in ar.db.quarantined] == [2]                 # the manual one is untouched

    ar.db.quarantined = _rows()
    fake_spq = types.SimpleNamespace(release_member=AsyncMock(return_value=(True, "ok")))
    monkeypatch.setitem(sys.modules, "modules.server_panel_quarantine", fake_spq)
    import modules as _pkg
    monkeypatch.setattr(_pkg, "server_panel_quarantine", fake_spq, raising=False)
    out = run(ar.pro.release_all(g, None, 9))
    assert "Released 2" in out
    assert {c.args[3] for c in fake_spq.release_member.await_args_list} == {1, 3}


def test_ban_needs_ban_permission_and_keeps_row_on_refusal(ar, monkeypatch):
    ar.db.quarantined = _rows()
    monkeypatch.setitem(sys.modules, "modules.server_panel",
                        types.SimpleNamespace(record_change=AsyncMock()))
    g = make_guild()
    g.me.guild_permissions.ban_members = False
    ok, msg = run(ar.pro.ban_suspect(g, None, SimpleNamespace(id=9), 1))
    assert not ok and "Ban Members" in msg and g.ban.await_count == 0
    g.me.guild_permissions.ban_members = True
    g.ban.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    ok, msg = run(ar.pro.ban_suspect(g, None, SimpleNamespace(id=9), 1))
    assert not ok and 1 not in ar.db.removed


def test_next_pending_order(ar):
    rows = [{"user_id": i} for i in (5, 6, 7)]
    assert ar.pro.next_pending(rows)["user_id"] == 5
    assert ar.pro.next_pending(rows, 5)["user_id"] == 6
    assert ar.pro.next_pending(rows, 7) is None
    assert ar.pro.next_pending([]) is None


def test_quarantine_suspect_is_idempotent(ar):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_pro_real", ROOT / "modules/antiraid_pro.py")
    real = importlib.util.module_from_spec(spec)             # a clean copy without the AsyncMock stub
    spec.loader.exec_module(real)
    ar.db.quarantined = [{"user_id": 8, "reason": "x"}]
    ok, msg = run(real.quarantine_suspect(make_guild(), None, member(8), "r"))
    assert ok and msg == "already quarantined"


def test_permission_helpers(ar):
    p = ar.pro
    mod = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, ban_members=False, manage_guild=True))
    banner = SimpleNamespace(guild_permissions=SimpleNamespace(administrator=False, ban_members=True, manage_guild=False))
    assert p.can_review(mod) and not p.can_ban(mod)          # Manage Server can release/review, not ban
    assert p.can_ban(banner) and p.can_review(banner)


# ── panel + premium gate ──────────────────────────────────────────────────

def _ids(view):
    return {getattr(c, "custom_id", None) for c in view.walk_children()}


def test_panel_shows_locked_buttons_for_free_and_review_for_premium(ar):
    g = make_guild()
    g.get_role = MagicMock(return_value=None)
    cfg = dict(ar.db.cfg)
    free = _ids(ar.mod.build_panel(g, None, cfg, g.log, premium=False))
    prem = _ids(ar.mod.build_panel(g, None, cfg, g.log, premium=True))
    assert f"arpro:filter:{GUILD}:-" in free and f"arpro:upgrade:{GUILD}:-" in free
    assert f"arpro:qreview:{GUILD}:-" not in free
    assert f"arpro:filter:{GUILD}:-" in prem and f"arpro:qreview:{GUILD}:-" in prem
    assert f"arpro:upgrade:{GUILD}:-" not in prem


def test_all_pro_custom_ids_match_a_registered_template(ar):
    views = ar.views
    g = make_guild()
    cfg = dict(ar.db.cfg)
    ids = set()
    ids |= _ids(views.build_filter_panel(g, None, cfg))
    ids |= _ids(views.action_view(GUILD, None))
    ids |= _ids(views.review_view(GUILD, None, 5))
    ids = {i for i in ids if i}
    assert ids
    for cid in ids:
        assert any(re.match(item.__discord_ui_compiled_template__.pattern, cid) for item in views.DYNAMIC_ITEMS), cid
    assert set(views.DYNAMIC_ITEMS) <= set(ar.mod.DYNAMIC_ITEMS)             # registered via antiraid


def test_free_server_picking_quarantine_gets_pitch_and_nothing_saved(ar, monkeypatch):
    ar.db.premium = False
    pitch = AsyncMock()
    monkeypatch.setitem(sys.modules, "discord_bot.cogs._views_premium",
                        types.SimpleNamespace(send_premium_pitch=pitch))
    g = make_guild()
    g.get_member = MagicMock(return_value=SimpleNamespace(
        id=5, guild_permissions=SimpleNamespace(manage_guild=True), ))
    g.owner = None
    client = SimpleNamespace(get_guild=lambda gid: g, clone_id=None)
    inter = MagicMock()
    inter.client = client
    inter.user = SimpleNamespace(id=5)
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=True)
    inter.followup.send = AsyncMock()
    inter.edit_original_response = AsyncMock()
    item = ar.mod.AntiRaidSelect("joiner", GUILD, None, "none")
    monkeypatch.setattr(type(item.item), "values", property(lambda self: ["quarantine"]))
    run(item.callback(inter))
    pitch.assert_awaited_once()
    assert ar.db.cfg["joiner_action"] == "none"


def test_quarantine_option_is_listed_and_flagged_premium(ar):
    assert "quarantine" in ar.mod.JOINER_ACTIONS and "💎" in ar.mod.JOINER_ACTIONS["quarantine"][0]
    assert all(len(v[2]) <= 100 for v in ar.mod.JOINER_ACTIONS.values())    # Discord option description limit


def test_missing_manage_roles_reported_for_quarantine(ar):
    g = make_guild()
    g.me.guild_permissions.manage_roles = False
    assert "Manage Roles" in ar.mod.missing_permissions(g, {"joiner_action": "quarantine"})


# ── perks + schema wiring ─────────────────────────────────────────────────

def test_premium_perks_list_mentions_anti_raid_pro():
    text = (ROOT / "discord_bot/cogs/_views_premium.py").read_text(encoding="utf-8")
    assert "Anti-raid Pro" in text.split("_PERKS = (")[1].split(")\n")[0]


def test_database_schema_covers_filter_columns():
    src = (ROOT / "database.py").read_text(encoding="utf-8")
    import re as _re
    # The anti-raid columns landed at schema 57; later bumps (other features) must not break this.
    assert int(_re.search(r'^SCHEMA_VERSION = "(\d+)"', src, _re.M).group(1)) >= 57
    for col in ("filter_age_days", "filter_default_avatar", "filter_suspicious_name", "filter_action"):
        assert src.count(col) >= 6, col       # ALTER, fields tuple, default dict, INSERT, UPDATE, args
