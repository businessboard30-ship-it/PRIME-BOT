import re
"""Leaderboard: XP controls grouped under one 'XP options' button + Dashboard link."""
import asyncio
import types

import discord
import pytest

import config
from discord_bot.cogs import _views_leveling_leaderboard as lb
from discord_bot.cogs import _views_leveling_boost as boost
from discord_bot.cogs import _views_leveling_wallet as wallet

CEILING = 40


def run(c):
    return asyncio.run(c)


class Guild:
    id = 99
    name = "G"
    icon = types.SimpleNamespace(url="https://cdn.discordapp.com/x.png")


def _count(view):
    n = 0
    for c in view.walk_children():
        n += 1
    return n


@pytest.fixture()
def build(monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "https://dash.example")
    monkeypatch.setattr(config, "TOPGG_VOTE_URL", "https://top.gg/vote")
    rows = [{"user_id": 1000 + i, "total_xp": 5000 - i, "level": 5} for i in range(10)]

    async def page(*a, **k):
        return rows, 50

    async def boosts(*a, **k):
        return {1000: 2.0, 1001: 2.0, 1002: 2.0, 1003: 2.0}

    async def seats(*a, **k):
        return [{"user_id": 1000, "clan_slug": "wolves"}]

    async def links(*a, **k):
        return {1000 + i: {"status": "approved", "invite_url": f"https://discord.gg/{i}"} for i in range(10)}

    async def voters(ids):
        return set()

    async def prem(*a, **k):
        return False

    async def noop(*a, **k):
        return None

    async def disp(bot, guild, mode, uid):
        return (f"user{uid}", None, None, None)
    d = lb.db
    for name, fn in [("get_xp_leaderboard_page", page), ("get_global_xp_leaderboard_page", page),
                     ("get_active_xp_boosts_for_users", boosts), ("get_active_global_boosts_for_users", boosts),
                     ("get_clan_seats", seats), ("get_leader_links_for_users", links),
                     ("is_guild_premium_active", prem)]:
        monkeypatch.setattr(d, name, fn, raising=False)
    monkeypatch.setattr(lb, "_safe_voters", voters)
    monkeypatch.setattr(lb, "_bulk_cache_members", noop)
    monkeypatch.setattr(lb, "_resolve_display", disp)

    def _build(mode="local", clone_id=None):
        return run(lb.build_leaderboard_view(object(), Guild(), clone_id, mode=mode, page=0))
    return _build


@pytest.mark.parametrize("mode", ["local", "global"])
def test_worst_case_page_never_exceeds_ceiling(build, mode):
    # 10 entries, boosted badges, leader links, guild icon, vote line, premium footer
    v = build(mode)
    assert _count(v) <= CEILING


def test_over_ceiling_page_falls_back_to_compact_with_headroom(build):
    v = build("local")
    assert _count(v) <= CEILING - 2
    assert [c for c in v.walk_children() if isinstance(c, discord.ui.Button) and c.label == "Dashboard"]


def test_typical_page_fits_with_headroom(build, monkeypatch):
    async def none(*a, **k):
        return {}
    monkeypatch.setattr(lb.db, "get_leader_links_for_users", none, raising=False)
    v = build("local")
    assert _count(v) <= CEILING - 1


def test_top_level_has_xp_wizard_my_card_dashboard_and_no_wallet_select(build):
    v = build("local")
    ids = [getattr(c, "custom_id", None) or getattr(c, "url", None) for c in v.walk_children()]
    ids = [i for i in ids if i]
    assert any(str(i).startswith("lvllb_xpopts:99:-") for i in ids)
    assert any(str(i).startswith("lvllb_card:99:-") for i in ids)            # My card is on the page now
    for old in ("lvllb_clans:", "levelboost_xp:", "lvllb_ping:", "buyboost_xp:"):             # Clans moved into the wizard
        assert not any(str(i).startswith(old) for i in ids)


@pytest.mark.parametrize("mode,frag", [("local", "/#/me/rank"), ("global", "/#/me/leaderboard")])
def test_dashboard_link_button_url_per_mode(build, mode, frag):
    v = build(mode)
    links = [c for c in v.walk_children()
             if isinstance(c, discord.ui.Button) and c.label == "Dashboard"]
    assert len(links) == 1
    assert links[0].style == discord.ButtonStyle.link
    assert links[0].url == "https://dash.example" + frag


@pytest.mark.parametrize("mode", ["local", "global"])
def test_clone_gets_dashboard_button_pointing_at_its_server_dashboard(build, mode):
    v = build(mode, clone_id=3)
    links = [c for c in v.walk_children() if isinstance(c, discord.ui.Button) and c.label == "Dashboard"]
    assert len(links) == 1 and links[0].style == discord.ButtonStyle.link
    assert re.fullmatch(r"https://dash\.example/#/c/3/g/\d+", links[0].url)
    assert _count(v) <= CEILING


def test_clone_without_guild_id_gets_no_button():
    assert lb._dashboard_link_button(3, "local", None) is None
    assert lb._dashboard_link_button(3, "local") is None


def test_no_dashboard_url_hides_button(build, monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "")
    v = build("local")
    assert not [c for c in v.walk_children() if isinstance(c, discord.ui.Button) and c.label == "Dashboard"]


def test_old_custom_ids_still_registered():
    templates = {i.__discord_ui_compiled_template__.pattern for i in lb.DYNAMIC_ITEMS}
    assert any("lvllb_ping" in t for t in templates)
    assert any("lvllb_clans" in t for t in templates)
    assert any("lvllb_xpopts" in t for t in templates)
    assert boost.BoostXPButton in boost.DYNAMIC_ITEMS
    assert wallet.BuyBoostSelect in wallet.DYNAMIC_ITEMS


def test_xp_button_is_renamed_but_keeps_its_custom_id():
    btn = lb.LeaderboardXPOptionsButton(99, 5)
    assert btn.item.label == "XP & Clans" and btn.item.custom_id == "lvllb_xpopts:99:5"


def test_xp_button_opens_the_wizard(monkeypatch):
    from discord_bot.cogs import _views_xp_wizard as wiz
    opened = []

    async def fake_open(interaction, guild_id, clone_id, tab="boost"):
        opened.append((guild_id, clone_id, tab))
    monkeypatch.setattr(wiz, "open_xp_wizard", fake_open)
    run(lb.LeaderboardXPOptionsButton(99, 5).callback(object()))
    run(lb.LeaderboardClansButton(99, 5).callback(object()))                    # an old message's Clans button
    assert opened == [(99, 5, "boost"), (99, 5, "clans")]


def test_xp_options_button_from_custom_id_roundtrip():
    import re
    m = re.match(lb.LeaderboardXPOptionsButton.__discord_ui_compiled_template__, "lvllb_xpopts:12:-")
    b = run(lb.LeaderboardXPOptionsButton.from_custom_id(None, None, m))
    assert (b.guild_id, b.clone_id) == (12, None)
