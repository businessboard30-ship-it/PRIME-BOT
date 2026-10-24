"""Level-up ping panel, Boost XP picker and /levelrole list: Components V2, no timeout, restart-safe."""
import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import discord

import config
from discord_bot.cogs import _views_leveling_boost as boost
from discord_bot.cogs import _views_leveling_leaderboard as lb
from discord_bot.cogs import leveling

run = asyncio.run


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


def inter(uid=7):
    i = MagicMock()
    i.user = types.SimpleNamespace(id=uid)
    i.client = types.SimpleNamespace(clone_id=None)
    for name in ("edit_message", "send_message", "defer"):
        setattr(i.response, name, AsyncMock())
    i.followup.send = AsyncMock()
    return i


# ---- ping status panel ----
def test_ping_panel_is_v2_never_expires_and_toggle_rereads_state(monkeypatch):
    for muted in (True, False):
        v = lb.build_ping_status_view(5, muted)
        assert isinstance(v, discord.ui.LayoutView) and v.timeout is None and v.to_components()
        assert ("OFF" in text_of(v)) == muted
    calls = []
    async def toggle(g, u):
        calls.append((g, u)); return True
    monkeypatch.setattr(lb.db, "toggle_level_ping_optout", toggle)
    item = lb.LevelPingToggleButton.__discord_ui_compiled_template__.match("lvl_pingtoggle:5")
    item = run(lb.LevelPingToggleButton.from_custom_id(None, None, item))     # rebuilt after a "restart"
    i = inter()
    run(item.callback(i))
    assert calls == [(5, 7)]
    out = i.response.edit_message.await_args.kwargs["view"]
    assert "OFF" in text_of(out) and out.timeout is None


# ---- boost picker ----
def test_boost_picker_ids_route_and_rows_hold_at_most_five(monkeypatch):
    bundles = dict(config.XP_BOOST_BUNDLES)
    v = boost.build_boost_picker_view(5, bundles, True, "pick one")
    assert isinstance(v, discord.ui.LayoutView) and v.timeout is None and v.to_components()
    btns = [n for n in nodes(v) if isinstance(n, boost.BoostBuyButton)]
    assert btns[0].item.label.startswith("Level Pass")
    t = boost.BoostBuyButton.__discord_ui_compiled_template__
    for b in btns:
        assert t.match(b.item.custom_id)
    for r in (n for n in nodes(v) if isinstance(n, discord.ui.ActionRow)):
        assert len(r.children) <= 5
    many = {f"xp_boost_{k}": {"label": f"P{k}", "fee_usd": 1, "boosts": 1} for k in range(8)}
    v = boost.build_boost_picker_view(5, many, True, "x")
    assert all(len(r.children) <= 5 for r in nodes(v) if isinstance(r, discord.ui.ActionRow))


def test_buy_button_only_charges_for_what_is_on_sale_now(monkeypatch):
    started = []
    async def fake(interaction, payment_type, guild_id):
        started.append((payment_type, guild_id))
    monkeypatch.setattr(boost, "_start_boost_checkout", fake)
    async def offered(clone):
        return True
    monkeypatch.setattr(boost, "pass_offered", offered)
    monkeypatch.setattr(boost, "_available_bundles", lambda: {"xp_boost_3": config.XP_BOOST_BUNDLES["xp_boost_3"]})
    m = boost.BoostBuyButton.__discord_ui_compiled_template__.match("lvlboostbuy:5:xp_boost_3")
    ok = run(boost.BoostBuyButton.from_custom_id(None, None, m))
    i = inter(); run(ok.callback(i))
    assert started == [("xp_boost_3", 5)] and i.response.defer.await_count == 1
    gone = run(boost.BoostBuyButton.from_custom_id(
        None, None, boost.BoostBuyButton.__discord_ui_compiled_template__.match("lvlboostbuy:5:xp_boost_5")))
    j = inter(); run(gone.callback(j))                        # withdrawn since the message was posted
    assert started == [("xp_boost_3", 5)]
    assert "isn't on sale" in j.response.send_message.await_args.args[0]


# ---- /levelrole list ----
def test_levelrole_list_is_v2_and_pings_nobody(monkeypatch):
    async def roles(guild_id, clone_id=None):
        return [{"level": 5, "role_id": 111}, {"level": 10, "role_id": 222}]
    monkeypatch.setattr(leveling.db, "get_level_roles", roles)
    i = inter()
    i.guild_id = 5
    i.followup.send = AsyncMock()
    cog = leveling.LevelingCog.__new__(leveling.LevelingCog)
    run(leveling.LevelingCog.list_roles.callback(cog, i))
    kw = i.followup.send.await_args.kwargs
    assert isinstance(kw["view"], discord.ui.LayoutView) and kw["view"].to_components()
    assert "<@&111>" in text_of(kw["view"]) and "Level 10" in text_of(kw["view"])
    assert kw["allowed_mentions"].roles is False and "embed" not in kw
