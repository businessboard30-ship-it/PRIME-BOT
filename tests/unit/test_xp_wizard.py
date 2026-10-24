"""The leaderboard's XP & Clans wizard: Components V2 tabs that edit one private message in place,
with no timeout and nothing kept in memory (state rides in the custom_ids)."""
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


def nodes(v):
    out = []
    def go(n):
        out.append(n)
        for c in getattr(n, "children", []) or []:
            go(c)
    for n in v.children:
        go(n)
    return out


def text_of(v):
    return "\n".join(n.content for n in nodes(v) if isinstance(n, discord.ui.TextDisplay))


def rows(v):
    return [n for n in nodes(v) if isinstance(n, discord.ui.ActionRow)]


def controls(v):
    return [c for r in rows(v) for c in r.children]


def label_of(c):
    return getattr(getattr(c, "item", c), "label", None)


def cid_of(c):
    return getattr(getattr(c, "item", c), "custom_id", None)


def make_view(env, tab="boost", clan=None, page=0):
    st = wiz.XPWizardState(5, None, 7, tab, clan, page)
    st.guild = Guild()
    run(st.load())
    if tab == "clans":
        run(st._load_clan_page())
    return st.build()


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


def edited(i):
    return i.response.edit_message.await_args.kwargs["view"]


def click(item_cls, custom_id, i):
    """What discord.py does after a restart: match the custom_id, rebuild the item with no state, run it."""
    m = item_cls.__discord_ui_compiled_template__.match(custom_id)
    assert m, custom_id
    item = run(item_cls.from_custom_id(i, None, m))
    return item


def test_every_tab_is_v2_with_no_timeout(env):
    for tab, _l, _e in wiz.TABS:
        v = make_view(env, tab)
        assert isinstance(v, discord.ui.LayoutView) and v.timeout is None and v.to_components()


def test_tabs_are_on_the_first_row_and_the_active_one_is_highlighted(env):
    for tab, _l, _e in wiz.TABS:
        first = rows(make_view(env, tab))[0].children
        assert [label_of(c) for c in first] == [t[1] for t in wiz.TABS] + ["Close"]
        active = [label_of(c) for c in first if getattr(c.item, "style", None) == discord.ButtonStyle.primary]
        assert active == [{t[0]: t[1] for t in wiz.TABS}[tab]]


def test_summary_shows_boost_server_boost_and_pings_on_every_tab(env):
    env["boost"] = {"expires_at": FUTURE, "multiplier": 2}
    env["server"] = {"expires_at": FUTURE, "multiplier": 3}
    env["muted"] = {7}
    for tab, _l, _e in wiz.TABS:
        t = text_of(make_view(env, tab))
        assert "Your XP boost:** ⚡ 2x" in t and "Server boost:** ⚡ 3x" in t and "🔕 off" in t
    env["boost"] = env["server"] = None
    env["muted"] = set()
    t = text_of(make_view(env))
    assert "none right now" in t and "🔔 on" in t


def test_a_failing_lookup_never_breaks_the_wizard(env, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(wiz.db, "get_active_xp_boost", boom)
    assert "none right now" in text_of(make_view(env))


def test_boost_tab_sells_the_pass_and_the_packs_inline(env):
    v = make_view(env)
    buy = [label_of(c) for r in rows(v)[1:] for c in r.children]
    assert buy[0].startswith("Level Pass") and any("3-Pack" in b for b in buy)
    assert "Level Pass" in text_of(v)
    env["pass"], env["bundles"] = False, {}
    v = make_view(env)
    assert len(rows(v)) == 1 and "aren't on sale" in text_of(v)


def test_a_buy_button_starts_the_same_checkout_as_before(env, monkeypatch):
    started = []
    async def fake(interaction, payment_type, guild_id):
        started.append((payment_type, guild_id))
    monkeypatch.setattr(boost_mod, "_start_boost_checkout", fake)
    v = make_view(env)
    pass_btn = next(c for c in controls(v) if (label_of(c) or "").startswith("Level Pass"))
    i = inter()
    run(pass_btn.callback(i))
    i.response.defer.assert_awaited_once()
    assert started == [("level_pass", 5)]


def test_switching_tab_edits_the_same_message_and_refreshes_state(env):
    v = make_view(env)
    i = inter()
    tab = next(c for c in rows(v)[0].children if label_of(c) == "Pings")
    env["muted"] = {7}                                   # changed since the message was drawn
    run(tab.callback(i))
    out = edited(i)
    assert "**Off.**" in text_of(out) and out.timeout is None
    assert i.followup.send.await_count == 0


def test_pings_toggle_flips_the_setting_and_the_button(env):
    v = make_view(env, "pings")
    off = rows(v)[1].children[0]
    assert label_of(off) == "Turn pings OFF"
    i = inter()
    run(off.callback(i))
    assert env["toggles"] == 1 and 7 in env["muted"]
    again = rows(edited(i))[1].children[0]
    assert label_of(again) == "Turn pings ON" and "**Off.**" in text_of(edited(i))


def test_clans_tab_lists_clans_then_shows_members_with_paging(env):
    v = make_view(env, "clans")
    assert len(rows(v)) == 2 and "Pick a clan" in text_of(v)
    sel = rows(v)[1].children[0]
    fn = sel.item.options[0].value
    i = inter()
    sel.item._values = [fn]
    run(sel.callback(i))
    out = edited(i)
    t = text_of(out)
    assert "Ann" in t and "<@99>" in t and "23 members" in t
    pager = rows(out)[2].children
    assert [label_of(c) for c in pager] == ["◀", "1/3", "▶"]
    assert pager[0].item.disabled is True and pager[2].item.disabled is False
    nxt = pager[2]
    i2 = inter()
    run(nxt.callback(i2))
    assert env["clan_calls"][-1] == (fn, 10)                       # second page = offset 10
    assert "2/3" in [label_of(c) for c in rows(edited(i2))[2].children]


def test_server_boost_tab_uses_the_same_select_and_id(env):
    v = make_view(env, "server")
    sel = [c for c in controls(v) if (cid_of(c) or "").startswith("buyboost_xp:")]
    assert len(sel) == 1
    assert "2x XP for everyone" in text_of(v).replace(".0", "")


def test_close_removes_the_controls(env):
    v = make_view(env)
    close = next(c for c in rows(v)[0].children if label_of(c) == "Close")
    i = inter()
    run(close.callback(i))
    out = edited(i)
    assert not rows(out) and "Closed" in text_of(out)


def test_opening_acknowledges_then_sends_one_private_message(env):
    i = inter()
    run(wiz.open_xp_wizard(i, 5, None))
    i.response.defer.assert_awaited_once_with(ephemeral=True)
    (args, kw), = [i.followup.send.await_args]
    assert kw["ephemeral"] is True and isinstance(kw["view"], discord.ui.LayoutView)
    assert "XP & Clans" in text_of(kw["view"]) and "embed" not in kw


def test_opening_on_the_clans_tab_loads_the_clan_page(env):
    i = inter()
    run(wiz.open_xp_wizard(i, 5, None, tab="clans"))
    assert wiz.XPWizardState(5, None, 7, "nonsense").tab == "boost"


def test_every_tab_is_within_discord_limits(env):
    for tab, _l, _e in wiz.TABS:
        v = make_view(env, tab, clan=("x.png" if tab == "clans" else None))
        assert len(v.to_components()) >= 1 and len(list(v.walk_children())) <= 40
        for r in rows(v):
            assert len(r.children) <= 5
        assert len(text_of(v)) < 4000


# ── no timeout, survives a restart ────────────────────────────────────────────
def test_every_custom_id_routes_to_exactly_one_registered_item(env):
    templates = [(c, c.__discord_ui_compiled_template__) for c in
                 wiz.DYNAMIC_ITEMS + (boost_mod.BoostBuyButton,)]
    from discord_bot.cogs import _views_leveling_wallet as wallet
    templates.append((wallet.BuyBoostSelect, wallet.BuyBoostSelect.__discord_ui_compiled_template__))
    for tab, _l, _e in wiz.TABS:
        v = make_view(env, tab, clan=("clan_white_wolves.png" if tab == "clans" else None))
        for c in controls(v):
            cid = cid_of(c)
            if getattr(c.item if hasattr(c, "item") else c, "disabled", False) and cid == "xpw:pagenum":
                continue
            assert sum(1 for _c, t in templates if t.match(cid)) == 1, cid


def test_an_old_page_button_still_works_after_a_restart(env):
    """State (guild, clone, clan, page) is in the custom_id; the data is re-read, nothing is remembered."""
    fn = "clan_white_wolves.png"
    i = inter()
    item = click(wiz.XPPageButton, f"xpw:pg:5:-:{fn}:1", i)
    assert (item.guild_id, item.clone_id, item.clan, item.page) == (5, None, fn, 1)
    run(item.callback(i))
    assert env["clan_calls"][-1] == (fn, 10) and "2/3" in [label_of(c) for c in rows(edited(i))[2].children]


def test_an_old_tab_button_and_ping_toggle_still_work_after_a_restart(env):
    i = inter()
    run(click(wiz.XPTabButton, "xpw:tab:5:-:pings", i).callback(i))
    assert "Pings" in text_of(edited(i))
    i2 = inter()
    run(click(wiz.XPPingToggle, "xpw:ping:5:-", i2).callback(i2))
    assert env["toggles"] == 1 and label_of(rows(edited(i2))[1].children[0]) == "Turn pings ON"
