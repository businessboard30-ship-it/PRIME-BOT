"""The leaderboard's XP & Clans wizard: tabs that edit one private message in place."""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from discord_bot.cogs import _views_leveling_boost as boost_mod
from discord_bot.cogs import _views_xp_wizard as wiz
from modules import clan_cards

run = asyncio.run
FUTURE = datetime.now(timezone.utc) + timedelta(days=3)


class Member:
    def __init__(self, uid, name):
        self.id, self.display_name = uid, name


class Guild:
    def __init__(self):
        self.members = {1: Member(1, "Ann"), 2: Member(2, "Bo")}

    def get_member(self, uid):
        return self.members.get(uid)


@pytest.fixture
def env(monkeypatch):
    st = {"muted": set(), "toggles": 0, "boost": None, "server": None, "clan_calls": []}

    async def get_boost(g, u, clone_id=None):
        return st["boost"]

    async def get_server(g, clone_id=None):
        return st["server"]

    async def muted(g, ids):
        return set(st["muted"])

    async def toggle(g, u):
        st["toggles"] += 1
        st["muted"] ^= {u}
        return u in st["muted"]

    async def members(g, clan, clone_id=None, limit=10, offset=0):
        st["clan_calls"].append((clan, offset))
        return [{"user_id": 1, "level": 5, "total_xp": 900}, {"user_id": 99, "level": 2, "total_xp": 50}][: limit]

    async def count(g, clan, clone_id=None):
        return 23
    for n, f in (("get_active_xp_boost", get_boost), ("get_active_guild_xp_boost", get_server), ("get_level_ping_muted", muted),
                 ("toggle_level_ping_optout", toggle), ("get_clan_members", members), ("get_clan_member_count", count)):
        monkeypatch.setattr(wiz.db, n, f, raising=False)

    async def offered(clone):
        return st.get("pass", True)
    monkeypatch.setattr(boost_mod, "pass_offered", offered)
    monkeypatch.setattr(boost_mod, "_available_bundles", lambda: st.get("bundles", dict(config.XP_BOOST_BUNDLES)))
    return st


def make_view(env, tab="boost"):
    v = wiz.XPWizardView(5, None, 7, tab)
    v.guild = Guild()
    run(v.load())
    return v.build()


def inter(uid=7):
    i = MagicMock()
    i.user = types.SimpleNamespace(id=uid)
    i.guild = Guild()
    i.response.edit_message = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock(return_value=MagicMock())
    i.data = {}
    return i


def labels(v):
    return [getattr(c, "label", None) for c in v.children]


def test_tabs_are_on_the_first_row_and_the_active_one_is_highlighted(env):
    for tab in ("boost", "pings", "clans", "server"):
        v = make_view(env, tab)
        row0 = [c for c in v.children if c.row == 0]
        assert [c.label for c in row0] == ["Boost XP", "Pings", "Clans", "Server boost", "Close"]
        active = [c.label for c in row0 if c.style == discord.ButtonStyle.primary]
        assert active == [dict((t[0], t[1]) for t in wiz.TABS)[tab]]


def test_summary_shows_boost_server_boost_and_pings_on_every_tab(env):
    env["boost"] = {"multiplier": 2.0, "expires_at": FUTURE}
    env["muted"] = {7}
    for tab in ("boost", "pings", "clans", "server"):
        d = make_view(env, tab).embed().description
        assert "Your XP boost:** ⚡ 2x XP until" in d and "Server boost:** none" in d and "Level-up pings:** 🔕 off" in d
    env["boost"], env["muted"] = None, set()
    assert "none right now" in make_view(env).embed().description and "🔔 on" in make_view(env).embed().description


def test_a_failing_lookup_never_breaks_the_wizard(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(wiz.db, "get_active_xp_boost", boom)
    v = make_view(env)
    assert "none right now" in v.embed().description


def test_boost_tab_sells_the_pass_and_the_packs_inline(env):
    v = make_view(env, "boost")
    buy = [c.label for c in v.children if c.row != 0]
    assert buy[0].startswith("Level Pass") and any("3-Pack" in b for b in buy) and any("5-Pack" in b for b in buy)
    assert "Level Pass" in v.embed().fields[0].value
    env["pass"], env["bundles"] = False, {}
    v = make_view(env, "boost")
    assert not [c for c in v.children if c.row != 0] and "aren't on sale" in v.embed().fields[0].value


def test_a_buy_button_starts_the_same_checkout_as_before(env, monkeypatch):
    started = []

    async def fake_start(interaction, payment_type, guild_id):
        started.append((payment_type, guild_id))
    monkeypatch.setattr(boost_mod, "_start_boost_checkout", fake_start)
    v = make_view(env, "boost")
    pass_btn = next(c for c in v.children if (c.label or "").startswith("Level Pass"))
    i = inter()
    run(pass_btn.callback(i))
    i.response.defer.assert_called_once_with(ephemeral=True, thinking=True)
    assert started == [("level_pass", 5)]


def test_switching_tab_edits_the_same_message_and_refreshes_state(env):
    v = make_view(env, "boost")
    env["boost"] = {"multiplier": 2.0, "expires_at": FUTURE}                      # bought while the panel was open
    i = inter()
    run(next(c for c in v.children if c.label == "Pings").callback(i))
    kw = i.response.edit_message.call_args.kwargs
    assert v.tab == "pings" and kw["view"] is v and "until" in kw["embed"].description


def test_only_the_person_who_opened_it_can_use_it(env):
    v = make_view(env)
    other = inter(uid=8)
    assert run(v.interaction_check(other)) is False
    assert "belongs to someone else" in other.response.send_message.call_args.args[0]
    assert run(v.interaction_check(inter(uid=7))) is True


def test_pings_toggle_flips_the_setting_and_the_button(env):
    v = make_view(env, "pings")
    off = next(c for c in v.children if c.row == 1)
    assert off.label == "Turn pings OFF"
    i = inter()
    run(off.callback(i))
    assert env["toggles"] == 1 and env["muted"] == {7}
    again = next(c for c in v.children if c.row == 1)
    assert again.label == "Turn pings ON" and "off" in i.response.edit_message.call_args.kwargs["embed"].description


def test_clans_tab_lists_clans_then_shows_members_with_paging(env):
    v = make_view(env, "clans")
    sel = next(c for c in v.children if isinstance(c, discord.ui.Select))
    assert {o.value for o in sel.options} == {fn for fn, *_ in clan_cards.CLAN_CARDS}
    assert not [c for c in v.children if c.row == 2] and "Pick a clan" in v.embed().fields[0].value
    i = inter()
    i.data = {"values": ["clan_deviors.png"]}
    run(v._pick_clan(i))
    f = i.response.edit_message.call_args.kwargs["embed"].fields[0]
    assert "DEVIORS" in f.name and "23 members" in f.name and "Ann" in f.value and "<@99>" in f.value
    row2 = [c for c in v.children if c.row == 2]
    assert [c.label for c in row2] == ["◀", "▶", "1/3"] and row2[0].disabled and not row2[1].disabled
    run(row2[1].callback(inter()))
    assert env["clan_calls"][-1] == ("clan_deviors.png", 10)
    assert [c for c in v.children if c.row == 2][0].disabled is False


def test_server_boost_tab_uses_the_same_select_and_id(env):
    v = make_view(env, "server")
    sel = [c for c in v.children if getattr(getattr(c, "item", None), "custom_id", "").startswith("buyboost_xp:")]
    assert len(sel) == 1 and sel[0].item.custom_id == "buyboost_xp:5:-"
    assert "2x XP for everyone" in v.embed().fields[0].value.replace(".0", "")


def test_close_removes_the_controls(env):
    v = make_view(env)
    i = inter()
    run(next(c for c in v.children if c.label == "Close").callback(i))
    assert i.response.edit_message.call_args.kwargs["view"] is None


def test_opening_acknowledges_then_sends_one_private_message(env):
    i = inter()
    run(wiz.open_xp_wizard(i, 5, None))
    i.response.defer.assert_called_once_with(ephemeral=True)
    kw = i.followup.send.call_args.kwargs
    assert kw["ephemeral"] is True and isinstance(kw["view"], wiz.XPWizardView) and kw["embed"].title == "⚡ XP & Clans"


def test_opening_on_the_clans_tab_loads_the_clan_page(env):
    i = inter()
    run(wiz.open_xp_wizard(i, 5, None, tab="clans"))
    assert i.followup.send.call_args.kwargs["view"].tab == "clans"
    assert wiz.XPWizardView(5, None, 7, "nonsense").tab == "boost"


def test_every_tab_is_a_valid_discord_view_within_the_limits(env):
    for tab in ("boost", "pings", "clans", "server"):
        v = make_view(env, tab)
        assert len(v.children) <= 25
        for row in range(5):
            assert len([c for c in v.children if c.row == row]) <= 5
        assert len(v.embed()) < 6000
