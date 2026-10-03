"""Owner-panel referral giveaway: counting, winners, roles, manual prize, owner-only."""
import asyncio
import importlib
import json
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, OTHER = 111, 222
MAIN = "discord_bot.cogs._views_admin_panel"
CTRL = "discord_bot.cogs._views_admin_panel_controls"
VIEW = "discord_bot.cogs._views_admin_panel_referral"
AC = "modules.admin_controls"
RG = "modules.referral_giveaway"
MODULES = (MAIN, CTRL, VIEW, AC, RG)
ROOT = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)
GUILD, ROLE = 555000000000000001, 666000000000000001


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    cfg.PREMIUM_DAYS = 30
    cfg.PREMIUM_GRACE_DAYS = 3
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(VIEW)
    mod.main = main
    mod.rg = importlib.import_module(RG)
    mod.audit = MagicMock()
    mod.ac = importlib.import_module(AC)
    yield mod
    for name in MODULES:
        sys.modules.pop(name, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = None
    i.data = {"values": values or []}
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    return i


def cog():
    return MagicMock()


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def press(view, label, **kw):
    i = I(**kw)
    run(buttons(view)[label].callback(i))
    return i


# ── fake database ────────────────────────────────────────────────────────

class _Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False


class FakeConn:
    def __init__(self):
        self.giveaways = {}
        self.redemptions = []      # (referred, referrer, at)
        self.fail = False
        self._next = 1

    def _check(self):
        if self.fail:
            raise RuntimeError("db down")

    def transaction(self):
        return _Tx()

    async def fetchval(self, sql, *a):
        self._check()
        assert sql.startswith("INSERT INTO referral_giveaways")
        gid = self._next
        self._next += 1
        title, prize, kind, guild, role, wc, starts, ends, by, desc = a
        self.giveaways[gid] = dict(id=gid, title=title, prize=prize, prize_kind=kind, guild_id=guild, role_id=role,
                                   winner_count=wc, min_referrals=1, starts_at=starts, ends_at=ends,
                                   status="active", winners_json=None, created_by=by, ended_at=None,
                                   description=desc, channel_id=None, message_id=None)
        return gid

    async def fetch(self, sql, *a):
        self._check()
        if "FROM referral_giveaways" in sql:
            rows = sorted(self.giveaways.values(), key=lambda g: (g["status"] != "active", -g["id"]))
            return [dict(r) for r in rows[: a[0]]]
        if "FROM ad_referral_redemptions" in sql:
            start, end, minimum, limit = a
            counts = {}
            for referred, referrer, at in self.redemptions:
                if start <= at <= end:
                    n, last = counts.get(referrer, (0, at))
                    counts[referrer] = (n + 1, max(last, at))
            rows = [dict(referrer_id=r, n=n, last_at=last) for r, (n, last) in counts.items() if n >= minimum]
            rows.sort(key=lambda r: (-r["n"], r["last_at"], r["referrer_id"]))
            return rows[:limit]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        self._check()
        if sql.startswith("SELECT * FROM referral_giveaways"):
            g = self.giveaways.get(a[0])
            return dict(g) if g else None
        if sql.startswith("UPDATE referral_giveaways SET status = 'ended'"):
            g = self.giveaways.get(a[0])
            if g and g["status"] == "active":
                g.update(status="ended", winners_json=a[1], ended_at=NOW)
                return dict(g)
            return None
        if sql.startswith("SELECT winners_json"):
            g = self.giveaways.get(a[0])
            return dict(winners_json=g["winners_json"]) if g and g["status"] == "ended" else None
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        self._check()
        assert sql.startswith("UPDATE referral_giveaways SET winners_json")
        g = self.giveaways.get(a[0])
        if g and (g["status"] == "ended" or "status = 'ended'" not in sql):
            g["winners_json"] = a[1]
        return "UPDATE 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(s): return conn
            async def __aexit__(s, *e): return False
        return Ctx()


@pytest.fixture()
def db(vp, monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(vp.rg, "_pool", AsyncMock(return_value=FakePool(conn)))
    vp.ac._helpers.update(ts=0.0, ok=False, map={})
    return conn


def redeem(db, referrer, n, start=None):
    base = start or NOW
    for k in range(n):
        db.redemptions.append((10_000 * referrer + len(db.redemptions), referrer, base + timedelta(minutes=len(db.redemptions))))


def new(vp, db, winners=1, guild=None, role=None):
    gid = run(vp.rg.create_giveaway("Launch", "VIP", 7, winners, OWNER, guild, role, now=NOW - timedelta(hours=1)))
    return gid


# ── parsing ──────────────────────────────────────────────────────────────

def test_parse_role_spec(vp):
    p = vp.rg.parse_role_spec
    assert p("") == (True, None, None) and p("   ") == (True, None, None)
    assert p(f"{GUILD} {ROLE}") == (True, GUILD, ROLE)
    assert p(f"{GUILD}, {ROLE}") == (True, GUILD, ROLE)
    assert p(f"{GUILD} <@&{ROLE}>") == (True, GUILD, ROLE)
    assert p("123") == (False, None, None) and p("vip role") == (False, None, None)
    assert p(f"{GUILD} {ROLE} {ROLE}") == (False, None, None)


# ── counting + winners ───────────────────────────────────────────────────

def test_only_referrals_inside_the_window_count(vp, db):
    gid = new(vp, db)
    redeem(db, 1, 2, start=NOW - timedelta(days=30))          # before the giveaway: not counted
    redeem(db, 2, 1, start=NOW)                               # inside
    g = run(vp.rg.list_giveaways())[0]
    assert [(r["user_id"], r["count"]) for r in run(vp.rg.standings(g))] == [(2, 1)]
    assert gid == g["id"]


def test_top_referrers_win_and_ties_go_to_who_got_there_first(vp, db):
    new(vp, db, winners=2)
    redeem(db, 7, 1)          # 7 reaches 2 referrals before 8 does
    redeem(db, 8, 1)
    redeem(db, 7, 1)
    redeem(db, 8, 1)
    redeem(db, 9, 1)
    done = run(vp.rg.end_giveaway(1))
    assert [w["user_id"] for w in done["winners"]] == [7, 8]
    assert [w["count"] for w in done["winners"]] == [2, 2]


def test_owners_never_win_their_own_giveaway(vp, db):
    new(vp, db, winners=1)
    redeem(db, OWNER, 5)
    redeem(db, 42, 1)
    assert [w["user_id"] for w in run(vp.rg.end_giveaway(1, {OWNER}))["winners"]] == [42]


def test_ending_twice_changes_nothing(vp, db):
    new(vp, db)
    redeem(db, 5, 1)
    assert run(vp.rg.end_giveaway(1)) is not None
    first = db.giveaways[1]["winners_json"]
    redeem(db, 6, 3)
    assert run(vp.rg.end_giveaway(1)) is None
    assert db.giveaways[1]["winners_json"] == first


def test_no_referrals_means_no_winners(vp, db):
    new(vp, db)
    assert run(vp.rg.end_giveaway(1))["winners"] == []


# ── prizes ───────────────────────────────────────────────────────────────

def test_role_giveaway_grants_role_automatically(vp, db, monkeypatch):
    new(vp, db, winners=2, guild=GUILD, role=ROLE)
    redeem(db, 5, 2)
    redeem(db, 6, 1)
    grant = AsyncMock(side_effect=[True, False])
    monkeypatch.setitem(sys.modules, "discord_bot.role_grant", types.SimpleNamespace(grant_role=grant))
    done = run(vp.rg.end_giveaway(1))
    assert run(vp.rg.grant_winner_roles(done)) == 1
    saved = json.loads(db.giveaways[1]["winners_json"])
    assert [w["role_ok"] for w in saved] == [True, False] and saved[0]["awarded"] is True
    assert grant.call_args_list[0].args[:3] == (GUILD, 5, ROLE)
    # retry only touches the failed one
    grant2 = AsyncMock(return_value=True)
    monkeypatch.setitem(sys.modules, "discord_bot.role_grant", types.SimpleNamespace(grant_role=grant2))
    g = run(vp.rg.list_giveaways())[0]
    assert run(vp.rg.grant_winner_roles(g, only_failed=True)) == 2
    assert grant2.call_count == 1 and grant2.call_args.args[1] == 6


def test_manual_prize_can_be_marked_given_once(vp, db):
    new(vp, db)
    redeem(db, 5, 1)
    run(vp.rg.end_giveaway(1))
    assert run(vp.rg.mark_awarded(1, 5)) is True
    assert run(vp.rg.mark_awarded(1, 5)) is False
    assert run(vp.rg.mark_awarded(1, 99)) is False


# ── panel screen ─────────────────────────────────────────────────────────

def test_screen_flow_end_is_two_step_and_shows_winners(vp, db):
    new(vp, db)
    redeem(db, 5, 3)
    v = vp.ReferralGiveawayView(cog(), OWNER)
    run(v.load())
    assert "<@5>" in text_of(v) and "3 referral" in text_of(v)
    press(v, "End & pick winners")                      # arms only
    assert db.giveaways[1]["status"] == "active" and "Confirm" in text_of(v)
    press(v, "Confirm end")
    assert db.giveaways[1]["status"] == "ended"
    assert "waiting for you to give the prize" in text_of(v)
    vp.audit.assert_any_call(vp.audit.call_args.args[0], "referral.giveaway.end", id=1, winners=[5], roles_given=0)


def test_screen_mark_prize_given(vp, db):
    new(vp, db)
    redeem(db, 5, 1)
    v = vp.ReferralGiveawayView(cog(), OWNER)
    run(v.load())
    press(v, "End & pick winners")
    press(v, "Confirm end")
    assert buttons(v)["Mark prize given"].disabled is True
    i = I(values=["5"])
    run(selects(v)[1].callback(i))
    press(v, "Mark prize given")
    assert "prize given" in text_of(v) and vp.audit.call_args.args[1] == "referral.giveaway.prize_given"


def test_screen_db_errors_are_shown_not_raised(vp, db):
    db.fail = True
    v = vp.ReferralGiveawayView(cog(), OWNER)
    run(v.load())
    assert "Couldn't load" in text_of(v)


def test_new_giveaway_modal_validates_and_creates(vp, db):
    m = vp.NewGiveawayModal(cog())
    m.name.default, m.prize.default = "Launch", "VIP role"
    for field, value in ((m.name, "Launch"), (m.prize, "VIP"), (m.description, "Invite friends!"), (m.days, "7"),
                         (m.winners, "2")):
        field._value = value
    i = I()
    run(m.on_submit(i))
    assert db.giveaways[1]["winner_count"] == 2 and db.giveaways[1]["prize_kind"] == "manual"
    assert db.giveaways[1]["description"] == "Invite friends!"
    assert vp.audit.call_args.args[1] == "referral.giveaway.create"
    m.days._value = "0"
    i2 = I()
    run(m.on_submit(i2))
    assert len(db.giveaways) == 1 and "Days" in i2.response.send_message.call_args.args[0]


# ── owner-only ───────────────────────────────────────────────────────────

def test_screen_is_owner_only_and_not_grantable(vp, db):
    assert "referral" in vp.main.allowed_sections(OWNER)
    assert "referral" not in vp.main.allowed_sections(OTHER)
    assert "referral" not in vp.ac.GRANTABLE
    vp.ac._helpers.update(ts=1e18, ok=True, map={333: set(vp.ac.GRANTABLE)})
    assert "referral" not in vp.main.allowed_sections(333)
    v = vp.ReferralGiveawayView(cog(), OWNER)
    assert run(v.interaction_check(I(user=OTHER))) is False
    assert run(v.interaction_check(I(user=OWNER))) is True


def test_home_button_is_disabled_for_non_owners(vp, db):
    assert buttons(vp.main.HomeView(cog(), OWNER))["Referral giveaway"].disabled is False
    assert buttons(vp.main.HomeView(cog(), OTHER))["Referral giveaway"].disabled is True


def test_modal_refuses_non_owners(vp, db):
    m = vp.NewGiveawayModal(cog())
    i = I(user=OTHER)
    run(m.on_submit(i))
    assert db.giveaways == {}


# ── schema wiring ────────────────────────────────────────────────────────

def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/027_referral_giveaway.sql").read_text()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).upper()
    assert "DROP " not in code and "DELETE " not in code and "TRUNCATE" not in code
    assert code.count("CREATE TABLE") == code.count("CREATE TABLE IF NOT EXISTS") == 2
    src = (ROOT / "database.py").read_text()
    assert "027_referral_giveaway.sql" in src
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)) >= 49


# ── public post ──────────────────────────────────────────────────────────

def test_public_embed_explains_how_to_enter_and_shows_description(vp):
    import datetime as dt
    from discord_bot.cogs import referral_giveaway_post as post
    g = dict(id=3, title="Big one", prize="10 Nitro", prize_kind="manual", description="Invite your friends!",
             winner_count=2, status="active", starts_at=NOW, ends_at=NOW + dt.timedelta(days=7), ended_at=None, winners=[])
    e = post.build_embed(g, [{"user_id": 42, "count": 5}], 9)
    assert "Invite your friends!" in e.description
    assert "Get my code" in e.description and "Enter a code" in e.description
    assert "<@42>" in e.description and "10 Nitro" in e.description and "9" in e.description
    g.update(status="ended", ended_at=NOW, winners=[{"user_id": 42, "count": 5}])
    done = post.build_embed(g, [], 9)
    assert "Winners" in done.description and "How to enter" not in done.description


def test_entry_stats_gives_count_and_rank(vp, db):
    rg = vp.rg
    now = NOW
    gid = run(rg.create_giveaway("T", "P", 7, 1, 1, now=now))
    for n, (who, by) in enumerate([(10, 1000), (11, 1000), (12, 2000)]):
        db.redemptions.append((who, by, now + timedelta(minutes=n + 1)))
    g = run(rg.list_giveaways())[0]
    assert run(rg.entry_stats(g, 1000)) == {"count": 2, "rank": 1, "total_referrers": 2}
    assert run(rg.entry_stats(g, 2000))["rank"] == 2
    assert run(rg.entry_stats(g, 9999))["count"] == 0


def test_enter_a_code_modal_redeems_and_refuses_on_ended_giveaway(vp, db, monkeypatch):
    import datetime as dt
    from unittest.mock import AsyncMock, MagicMock
    from discord_bot.cogs import referral_giveaway_post as post
    g = dict(id=1, status="active", message_id=77)
    monkeypatch.setattr(post.rg, "get_by_message", AsyncMock(return_value=g))
    refreshed = AsyncMock()
    monkeypatch.setattr(post, "refresh_post", refreshed)
    refs = types.ModuleType("modules.referrals")
    refs.use_referral_code = AsyncMock(return_value={"ok": True, "reason": "applied"})
    monkeypatch.setitem(sys.modules, "modules.referrals", refs)
    m = post.UseCodeModal(77)
    m.code._value = " ab12cd34 "
    i = I()
    i.client = MagicMock()
    i.followup.send = AsyncMock()
    run(m.on_submit(i))
    refs.use_referral_code.assert_awaited_once_with(i.user.id, "ab12cd34")
    assert "Applied" in i.followup.send.call_args.args[0] and refreshed.await_count == 1
    g["status"] = "ended"
    i2 = I()
    i2.followup.send = AsyncMock()
    run(m.on_submit(i2))
    assert "ended" in i2.followup.send.call_args.args[0] and refs.use_referral_code.await_count == 1


def test_channel_select_gets_its_own_row_and_payload_is_valid(vp, db):
    """Regression: ChannelSelect was packed into a row with buttons, so Discord rejected the
    edit after 'New giveaway' and the modal just kept loading."""
    gid = run(vp.rg.create_giveaway("T", "P", 7, 1, OWNER))
    v = vp.ReferralGiveawayView(cog(), OWNER)
    v.selected = gid
    run(v.load())
    rows = [c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)]
    for r in rows:
        kinds = [type(c) for c in r.children]
        if any(issubclass(k, (discord.ui.ChannelSelect, discord.ui.Select)) for k in kinds):
            assert len(kinds) == 1
        assert len(kinds) <= 5
    assert any(isinstance(c, discord.ui.ChannelSelect) for c in v.walk_children())
    v.to_components()


def test_join_dm_has_referral_code_button_that_redeems(monkeypatch):
    from unittest.mock import AsyncMock
    from discord_bot.cogs import _views_join_dm as jd
    assert jd._ReferralCodeButton in jd.DYNAMIC_ITEMS
    b = jd._ReferralCodeButton(123, None)
    assert b.item.custom_id == "join_dm_refcode:123:-"
    refs = types.ModuleType("modules.referrals")
    refs.use_referral_code = AsyncMock(return_value={"ok": False, "reason": "not_found"})
    monkeypatch.setitem(sys.modules, "modules.referrals", refs)
    m = jd._ReferralCodeModal()
    m.code._value = " zz99 "
    i = I()
    i.followup.send = AsyncMock()
    run(m.on_submit(i))
    refs.use_referral_code.assert_awaited_once_with(i.user.id, "zz99")
    assert "doesn't match" in i.followup.send.call_args.args[0]


def test_delete_giveaway_is_two_step_and_removes_it(vp, db, monkeypatch):
    from unittest.mock import AsyncMock
    gid = run(vp.rg.create_giveaway("T", "P", 7, 1, OWNER))
    orig = db.fetchrow

    async def fetchrow(sql, *a):
        if sql.startswith("DELETE FROM referral_giveaways"):
            return db.giveaways.pop(a[0], None)
        return await orig(sql, *a)
    db.fetchrow = fetchrow
    gone_post = AsyncMock(return_value=True)
    monkeypatch.setattr(vp.rgp, "delete_post", gone_post)
    v = vp.ReferralGiveawayView(cog(), OWNER)
    v.selected = gid
    run(v.load())
    press(v, "Delete giveaway")
    assert gid in db.giveaways and "Confirm delete" in buttons(v)
    press(v, "Confirm delete")
    assert gid not in db.giveaways and vp.audit.call_args.args[1] == "referral.giveaway.delete"
    assert "deleted" in text_of(v)
