"""Anime feed slice A: provider parsing, cache sharing, V2 card/wizard budgets, join DM wiring. No network."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from discord_bot.cogs import _views_anime_feed as vf
from discord_bot.cogs import _views_join_dm as jd
from modules import anime_feed as af

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)

@pytest.fixture(autouse=True)
def _no_donghua_network(monkeypatch):
    """The donghua schedule is a second provider call; these tests stay offline."""
    async def none():
        return [], True
    monkeypatch.setattr(af, "donghua_soon", none)



def row(mid=1, ep=3, adult=False, title="Frieren", at=2_000_000_000, desc="<b>Hi</b><br>there (Source: X)", cover="https://s4.anilist.co/c.jpg"):
    return {"airingAt": at, "episode": ep, "media": {"id": mid, "siteUrl": f"https://anilist.co/anime/{mid}", "isAdult": adult, "format": "TV_SHORT",
            "episodes": 12, "genres": ["Action", "Drama", "Fantasy", "Extra"], "title": {"romaji": title, "english": None},
            "coverImage": {"large": cover}, "description": desc}}


def payload(*rows):
    return {"data": {"Page": {"airingSchedules": list(rows)}}}


def test_parse_skips_adult_missing_and_duplicate_rows_and_never_raises():
    got = af.parse_airing(payload(row(1), row(2, adult=True), row(3, title=""), row(1), {"airingAt": None, "media": {}}, "junk", row(4, ep=0)))
    assert [p.anime_id for p in got] == [1]
    assert got[0].synopsis == "Hi there" and got[0].genres == ("Action", "Drama", "Fantasy") and got[0].fmt == "Tv Short"
    assert af.parse_airing({}) == [] and af.parse_airing(None) == [] and af.parse_airing({"data": {"Page": None}}) == []


def test_unsafe_urls_are_replaced_and_synopsis_is_trimmed():
    bad = row(5, cover="http://evil/x.png")
    bad["media"]["siteUrl"] = "https://evil.example/phish"
    p = af.parse_airing(payload(bad))[0]
    assert p.url == "https://anilist.co/anime/5" and p.cover is None
    long = af.clean_synopsis("word " * 200)
    assert len(long) <= af.SYNOPSIS_CHARS + 1 and long.endswith("…") and af.clean_synopsis(None) == ""


def test_one_provider_request_serves_every_server_and_failures_are_reported(monkeypatch):
    calls = []

    async def fake(now):
        calls.append(now)
        await asyncio.sleep(0)
        return payload(row(1))
    monkeypatch.setattr(af, "_fetch", fake)
    monkeypatch.setattr(af, "_cache", (0.0, []))
    monkeypatch.setattr(af, "_lock", None)

    async def many():
        return await asyncio.gather(*[af.airing_soon() for _ in range(10)])
    res = run(many())
    assert len(calls) == 1 and all(ok and len(items) == 1 for items, ok in res)
    assert run(af.airing_soon())[1] and len(calls) == 1                         # fresh cache, no new request

    async def boom(now):
        raise RuntimeError("down")
    monkeypatch.setattr(af, "_fetch", boom)
    monkeypatch.setattr(af, "_cache", (0.0, []))
    monkeypatch.setattr(af, "_lock", None)
    assert run(af.airing_soon()) == ([], False)
    monkeypatch.setattr(af, "_cache", (time.time() - af.CACHE_TTL - 5, af.parse_airing(payload(row(9)))))
    items, ok = run(af.airing_soon())
    assert ok and items[0].anime_id == 9                                        # an old list beats nothing when the provider fails


def test_card_is_v2_with_source_time_and_a_link_button_and_fits():
    p = af.parse_airing(payload(row(1)))[0]
    text = vf.card_text(p, tag="Preview")
    assert "<t:2000000000:F>" in text and "<t:2000000000:R>" in text and "Source: AniList" in text and "Episode 3 of 12" in text
    view = vf.post_view(p)
    kids = list(view.walk_children())
    assert sum(1 for _ in kids) <= 40
    assert any(getattr(c, "url", None) == "https://anilist.co/anime/1" for c in kids)
    nocover = af.parse_airing(payload(row(2, cover="")))[0]
    assert sum(1 for _ in vf.post_view(nocover).walk_children()) <= 40


class _Chan:
    id = 55
    mention = "<#55>"


def _guild():
    me = SimpleNamespace()
    ch = SimpleNamespace(id=55, mention="<#55>", permissions_for=lambda m: SimpleNamespace(send_messages=True))
    return SimpleNamespace(id=1, me=me, get_channel=lambda i: ch)


def test_wizard_screens_fit_and_only_the_opener_can_use_them():
    v = vf.AnimeSetupView(_guild(), None, 7, af.FeedConfig(1, None, True, 55))
    assert sum(1 for _ in v.walk_children()) <= 40
    v.previews, v.index, v.screen = af.parse_airing(payload(*[row(i) for i in range(1, 6)])), 2, "preview"
    v._build()
    assert sum(1 for _ in v.walk_children()) <= 40
    v.previews, v.screen = [], "preview"
    v._build()
    assert sum(1 for _ in v.walk_children()) <= 40
    other = SimpleNamespace(user=SimpleNamespace(id=8), response=SimpleNamespace(send_message=AsyncMock()))
    assert run(v.interaction_check(other)) is False
    assert run(v.interaction_check(SimpleNamespace(user=SimpleNamespace(id=7)))) is True


def test_join_dm_more_screen_has_the_anime_button_and_still_fits():
    view = jd.build_join_dm_more_view(123, None)
    kids = list(view.walk_children())
    assert sum(1 for _ in kids) <= 40
    ids = [getattr(c, "custom_id", "") for c in kids]
    assert any(i.startswith("join_dm_anime:") for i in ids) and all(len(i) <= 100 for i in ids if i)
    assert jd._AnimeButton in jd.DYNAMIC_ITEMS


def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/070_anime_feed.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS anime_feed_config" in sql and "DROP" not in sql.upper()
    assert "070_anime_feed.sql" in (ROOT / "database.py").read_text()


def test_provider_list_is_not_cut_to_the_preview_size_but_previews_are():
    items = af.parse_airing(payload(*[row(i, at=1_000 + i) for i in range(1, 41)]))
    assert len(items) == 40                                  # the worker must see every episode, not just the first 12
    shown = af.for_preview(items, now=1_000)
    assert len(shown) == af.MAX_ITEMS and [p.anime_id for p in shown] == list(range(1, af.MAX_ITEMS + 1))
