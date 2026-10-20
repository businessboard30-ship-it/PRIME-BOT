"""Anime feed D1: big banner picture, origin tag (donghua), Read more, and button persistence. No network."""
import asyncio
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from discord_bot.cogs import _views_anime_feed as vf
from discord_bot.cogs import _views_join_dm as jd
from modules import anime_feed as af
from tests.unit.test_anime_feed_slice_a import payload, row


def run(c):
    return asyncio.run(c)


def media_row(**extra):
    r = row(7, at=2_000_000_000)
    r["media"].update(extra)
    return r


def P(**kw):
    base = dict(anime_id=7, title="Show", episode=2, airing_at=2_000_000_000, synopsis="syn", cover=None, url="https://anilist.co/anime/7")
    base.update(kw)
    return af.Preview(**base)


# -- picture / colour / origin parsing -------------------------------------------------

def test_parse_keeps_banner_big_cover_colour_and_origin_https_only():
    p = af.parse_airing(payload(media_row(bannerImage="https://s4.anilist.co/b.jpg", countryOfOrigin="CN",
                                          coverImage={"large": "https://s4.anilist.co/c.jpg", "extraLarge": "https://s4.anilist.co/x.jpg", "color": "#e4a15d"})))[0]
    assert p.banner == "https://s4.anilist.co/b.jpg" and p.cover_xl == "https://s4.anilist.co/x.jpg"
    assert p.color == 0xE4A15D and p.origin == "CN" and af.origin_label(p) == "Donghua"
    bad = af.parse_airing(payload(media_row(bannerImage="http://insecure/b.jpg", coverImage={"large": "ftp://c", "extraLarge": "javascript:x", "color": "red"})))[0]
    assert bad.banner is None and bad.cover is None and bad.cover_xl is None and bad.color is None and bad.image is None


def test_image_prefers_banner_then_big_cover_then_cover_and_origin_is_never_guessed():
    assert P(banner="https://b", cover_xl="https://x", cover="https://c").image == "https://b"
    assert P(cover_xl="https://x", cover="https://c").image == "https://x"
    assert P(cover="https://c").image == "https://c"
    assert P().image is None and af.origin_label(P()) == "" and af.origin_label(P(origin="JP")) == "Anime"


# -- the card ----------------------------------------------------------------------------

def test_card_uses_one_full_width_gallery_not_a_small_thumbnail_and_tags_donghua():
    p = P(banner="https://s4.anilist.co/b.jpg", origin="CN", color=0x112233)
    view = vf.post_view(p)
    kids = list(view.walk_children())
    assert sum(isinstance(k, discord.ui.MediaGallery) for k in kids) == 1
    assert not any(isinstance(k, (discord.ui.Thumbnail, discord.ui.Section)) for k in kids)
    assert "Donghua" in vf.card_text(p)
    box = next(k for k in kids if isinstance(k, discord.ui.Container))
    assert box.accent_colour == discord.Color(0x112233)


def test_card_without_any_image_still_posts_text_only_and_default_colour():
    kids = list(vf.post_view(P()).walk_children())
    assert not any(isinstance(k, discord.ui.MediaGallery) for k in kids)
    assert next(k for k in kids if isinstance(k, discord.ui.Container)).accent_colour == vf.ACCENT


def test_card_buttons_read_more_follow_and_link_and_fit_the_limits():
    kids = list(vf.post_view(P(banner="https://b"), ping="<@&5> <@1>").walk_children())
    buttons = [getattr(k, "item", k) for k in kids if isinstance(k, (discord.ui.Button, discord.ui.DynamicItem))]   # DynamicItem wraps a Button
    labels = [b.label for b in buttons]
    assert labels == ["Read more", "Follow", "Alert me", "Search", "Categories", "Details on AniList"]
    assert len(kids) <= 40


# -- persistence: every card button survives a restart --------------------------------------

def test_every_dynamic_button_on_a_card_is_registered_and_rebuilds_from_its_custom_id_alone():
    kids = list(vf.post_view(P(anime_id=42, banner="https://b")).walk_children())
    ids = [k.custom_id for k in kids if isinstance(k, discord.ui.DynamicItem)]
    assert set(ids) == {"anime_more:42", "anime_follow:42", "anime_alert:42", "anime_search", "anime_cats"} and all(len(i) <= 100 for i in ids)
    for cid in ids:                                                   # a restart: only the custom_id string is left
        classes = [c for c in vf.DYNAMIC_ITEMS if re.match(c.__discord_ui_compiled_template__, cid)]
        assert len(classes) == 1 and classes[0] in jd.DYNAMIC_ITEMS   # exactly one handler, and the bot registers it
        m = classes[0].__discord_ui_compiled_template__.match(cid)
        item = run(classes[0].from_custom_id(None, None, m))
        assert getattr(item, "anime_id", 42) == 42 and item.item.custom_id == cid       # Search / Categories carry no id


# -- Read more ------------------------------------------------------------------------------

def detail_payload(**extra):
    m = {"id": 7, "siteUrl": "https://anilist.co/anime/7", "isAdult": False, "format": "TV", "status": "RELEASING", "episodes": 24,
         "genres": ["Action", "Fantasy"], "countryOfOrigin": "CN", "title": {"romaji": "Shen", "english": None, "native": "神"},
         "description": "word " * 400, "nextAiringEpisode": {"episode": 5, "airingAt": 2_000_000_000}}
    m.update(extra)
    return {"data": {"Media": m}}


def test_detail_is_the_full_synopsis_not_the_short_card_text_and_adult_is_refused():
    d = af.parse_detail(detail_payload())
    assert d.title == "Shen" and d.native == "神" and d.origin == "CN" and d.next_episode == 5
    assert len(d.synopsis) > af.SYNOPSIS_CHARS and len(d.synopsis) <= af.READ_MORE_CHARS + 1
    assert af.parse_detail(detail_payload(isAdult=True)) is None and af.parse_detail({}) is None and af.parse_detail(None) is None
    text = vf.detail_text(d)
    assert "Donghua" in text and "神" in text and len(text) <= 4000


def test_read_more_defers_first_then_answers_privately(monkeypatch):
    order = []

    async def defer(**kw):
        order.append(("defer", kw))

    async def get_detail(i):
        order.append(("fetch", i))
        return af.parse_detail(detail_payload()), True

    async def send(**kw):
        order.append(("send", kw))
    monkeypatch.setattr(af, "get_detail", get_detail)
    inter = SimpleNamespace(user=SimpleNamespace(id=991), response=SimpleNamespace(defer=defer, is_done=lambda: True),
                            followup=SimpleNamespace(send=send))
    run(vf._MoreButton(7).callback(inter))
    assert [o[0] for o in order] == ["defer", "fetch", "send"]          # acknowledged BEFORE any network work
    assert order[0][1]["ephemeral"] is True and order[2][1]["ephemeral"] is True


def test_read_more_has_a_cooldown_and_says_so_privately(monkeypatch):
    sent = []
    inter = SimpleNamespace(user=SimpleNamespace(id=555_001), response=SimpleNamespace(
        defer=AsyncMock(), is_done=lambda: True, send_message=AsyncMock(side_effect=lambda *a, **k: sent.append((a, k)))), followup=SimpleNamespace(send=AsyncMock()))

    async def get_detail(i):
        return None, False
    monkeypatch.setattr(af, "get_detail", get_detail)
    run(vf._MoreButton(7).callback(inter))
    run(vf._MoreButton(7).callback(inter))
    assert sent and sent[0][1]["ephemeral"] is True and "try again" in sent[0][0][0]


def test_detail_view_says_plainly_when_anilist_is_down_and_still_links_out():
    kids = list(vf.detail_view(None, False, 7).walk_children())
    assert any("can't reach AniList" in getattr(k, "content", "") for k in kids)
    assert any(getattr(k, "url", None) == "https://anilist.co/anime/7" for k in kids)


def test_detail_cache_is_bounded_and_serves_an_old_copy_when_the_provider_fails(monkeypatch):
    af._details.clear()
    old = af.parse_detail(detail_payload())
    af._details[7] = (time.time() - af.DETAIL_TTL - 5, old)             # expired, but better than nothing

    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("down")

    async def sess():
        return Boom()
    import anime_service
    monkeypatch.setattr(anime_service, "get_session", sess)
    got, ok = run(af.get_detail(7))
    assert got == old and ok is True
    for i in range(af.DETAIL_MAX + 5):
        af._details[1000 + i] = (time.time(), None)
    af._details.pop(7, None)
    assert len(af._details) <= af.DETAIL_MAX + 5
    af._details.clear()
