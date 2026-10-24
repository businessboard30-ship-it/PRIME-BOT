"""Anime spawns: what spawns, never a repeat, the card carries every button as Components V2, and the setup lets admins pick the interval."""
import asyncio
import random
from types import SimpleNamespace

import discord

from discord_bot.cogs import _views_anime_feed as vf
from modules import anime_feed as af
from modules import anime_feed_browse as ab
from modules import anime_feed_spawn as sp


def E(i, origin="JP"):
    return ab.Entry(i, f"Show {i}", f"https://anilist.co/anime/{i}", "syn", "Tv", "Releasing", 12, ("Action",), origin)


def test_kinds_pick_the_right_categories():
    assert set(sp.category_keys("donghua")) == {"donghua", "cultivation"}
    anime = sp.category_keys("anime")
    assert "donghua" not in anime and "cultivation" not in anime and "action" in anime
    assert set(sp.category_keys("both")) == {c.key for c in ab.CATEGORIES}


def test_a_spawn_never_repeats_a_recent_title_and_respects_the_kinds():
    entries = [E(1), E(2), E(3, "CN")]
    assert sp.choose(entries, "both", [1, 2, 3]) is None
    assert sp.choose(entries, "both", [1, 2]).anime_id == 3
    assert sp.choose(entries, "donghua", [], random.Random(0)).anime_id == 3
    assert sp.choose(entries, "anime", [1], random.Random(0)).anime_id == 2


def test_spawn_card_is_components_v2_with_every_button_and_no_airing_line():
    p = sp.to_preview(E(5))
    assert p.airing_at == 0
    text = vf.card_text(p)
    assert "An anime appeared" in text and "airs" not in text and "aired" not in text
    view = vf.post_view(p)
    assert isinstance(view, discord.ui.LayoutView)
    ids = {getattr(c, "custom_id", None) for c in view.walk_children()}
    assert {"anime_more:5", "anime_follow:5", "anime_alert:5", "anime_search", "anime_cats"} <= ids
    assert any(getattr(c, "url", None) == p.url for c in view.walk_children())


def test_setup_has_an_interval_choice_and_saves_it(monkeypatch):
    saved = []

    async def save(cfg, by=None):
        saved.append(cfg.spawn_interval_minutes)
    monkeypatch.setattr(af, "save_config", save)

    async def go():
        cfg = af.FeedConfig(1, None, False, 100)
        view = vf.AnimeSetupView(SimpleNamespace(), None, 9, cfg)
        selects = [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]
        pace = next(s for s in selects if "spawn" in (s.placeholder or "").lower())
        assert [int(o.value) for o in pace.options] == list(af.SPAWN_INTERVALS)

        class I:
            data = {"values": ["30"]}
            user = SimpleNamespace(id=9)
            response = SimpleNamespace(is_done=lambda: True)

            async def edit_original_response(self, **kw):
                pass
        await view._on_interval(I())
        assert saved == [30] and "every 30 minutes" in vf.setup_text(cfg)
    asyncio.run(go())


def test_setup_text_says_it_sleeps_until_set_up():
    off = vf.setup_text(af.FeedConfig(1, None, False, None))
    assert "I do nothing in your server" in off and "OFF (asleep)" in off
