"""Anime feed D4 (Donghua): kinds filter, merged schedule, two-step donghua fetch + caches, worker wiring, wizard setting, migration. No network."""
import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_anime_feed as vf
from modules import anime_alerts as aa
from modules import anime_feed as af
from modules import anime_feed_worker as aw

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def P(i, ep=1, at=0, origin="JP"):
    return af.Preview(i, f"Show {i}", ep, at, "syn", None, f"https://anilist.co/anime/{i}", origin=origin)


@pytest.fixture(autouse=True)
def clean():
    af.provider_reset()
    af._cn_ids, af._cn_cache = (0.0, None), (0.0, None)
    af._cache = (0.0, [])
    yield
    af.provider_reset()
    af._cn_ids, af._cn_cache = (0.0, None), (0.0, None)
    af._cache = (0.0, [])


# ── pure ────────────────────────────────────────────────────────────────

def test_kinds_filter_and_default_is_both():
    assert af.FeedConfig(1).content_kinds == "both"
    items = [P(1, origin="JP"), P(2, origin="CN"), P(3, origin=""), P(4, origin="KR")]
    ids = lambda k: [p.anime_id for p in af.for_kinds(items, k)]
    assert ids("both") == [1, 2, 3, 4] and ids("anime") == [1, 3, 4] and ids("donghua") == [2]
    assert af.normalize_kinds("junk") == "both" and af.normalize_kinds(None) == "both"
    assert af.wants("anime", "CN") is False and af.wants("donghua", "cn") is True


def test_merge_dedupes_by_title_and_episode_and_sorts_by_time():
    out = af.merge([P(1, at=300), P(2, at=100)], [P(2, at=100), P(3, at=200), P(1, ep=2, at=400)])
    assert [(p.anime_id, p.episode) for p in out] == [(2, 1), (3, 1), (1, 1), (1, 2)]


def test_migration_defaults_existing_servers_to_both_and_is_idempotent():
    sql = (ROOT / "database" / "migrations" / "075_anime_content_kinds.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS content_kinds" in sql and "DEFAULT 'both'" in sql and "NOT NULL" in sql
    assert "075_anime_content_kinds.sql" in (ROOT / "database.py").read_text()


# ── the two-step donghua fetch ──────────────────────────────────────────

def schedule(i, at, origin="CN"):
    return {"airingAt": at, "episode": 2, "media": {"id": i, "siteUrl": f"https://anilist.co/anime/{i}", "isAdult": False, "format": "TV", "episodes": 24,
                                                  "genres": ["Action"], "countryOfOrigin": origin, "bannerImage": None,
                                                  "title": {"romaji": f"Dong {i}", "english": None, "native": "x"},
                                                  "coverImage": {"large": "https://c/l.jpg", "extraLarge": None, "color": None}, "description": "d"}}


def fake_provider(monkeypatch, ids=(11, 12), at=None, fail_ids=False, fail_schedule=False):
    calls = []
    at = at or int(time.time()) + 600

    async def post(query, variables, priority=False):
        calls.append((("ids" if "countryOfOrigin: CN" in query else "sched"), variables, priority))
        if "countryOfOrigin: CN" in query:
            return None if fail_ids else {"data": {"Page": {"media": [{"id": i} for i in ids]}}}
        return None if fail_schedule else {"data": {"Page": {"airingSchedules": [schedule(i, at) for i in ids]}}}
    monkeypatch.setattr(af, "provider_post", post)
    return calls


def test_donghua_fetch_asks_for_the_chinese_ids_then_only_their_schedule_and_caches(monkeypatch):
    calls = fake_provider(monkeypatch)
    items, ok = run(af.donghua_soon())
    assert ok and [p.anime_id for p in items] == [11, 12] and all(p.origin == "CN" for p in items)
    assert [c[0] for c in calls] == ["ids", "sched"] and calls[1][1]["ids"] == [11, 12] and all(c[2] for c in calls)   # worker priority
    run(af.donghua_soon())
    assert len(calls) == 2                                                                                           # cached: no new requests


def test_donghua_ids_are_cached_longer_than_the_schedule(monkeypatch):
    calls = fake_provider(monkeypatch)
    run(af.donghua_soon())
    af._cn_cache = (time.time() - af.CACHE_TTL - 1, af._cn_cache[1])                                                   # schedule expired, ids still fresh
    run(af.donghua_soon())
    assert [c[0] for c in calls] == ["ids", "sched", "sched"]


def test_donghua_failures_serve_an_old_copy_or_say_not_ok(monkeypatch):
    fake_provider(monkeypatch, fail_ids=True)
    assert run(af.donghua_soon()) == ([], False)
    fake_provider(monkeypatch, ids=())
    assert run(af.donghua_soon()) == ([], True)                                                                       # AniList knows no airing donghua: ok, empty
    af._cn_ids, af._cn_cache = (0.0, None), (0.0, None)
    fake_provider(monkeypatch)
    first, _ = run(af.donghua_soon())
    af._cn_cache = (time.time() - af.CACHE_TTL - 1, first)
    fake_provider(monkeypatch, fail_schedule=True)
    stale, ok = run(af.donghua_soon())
    assert ok and len(stale) == 2


def test_feed_items_never_lets_a_missing_donghua_list_block_anime(monkeypatch):
    async def anime():
        return [P(1, at=50)], True

    async def cn_down():
        return [], False

    async def cn():
        return [P(2, origin="CN", at=40)], True
    monkeypatch.setattr(af, "airing_soon", anime)
    monkeypatch.setattr(af, "donghua_soon", cn_down)
    assert run(af.feed_items(True)) == ([P(1, at=50)], True)
    monkeypatch.setattr(af, "donghua_soon", cn)
    assert [p.anime_id for p in run(af.feed_items(True))[0]] == [2, 1]
    assert [p.anime_id for p in run(af.feed_items(False))[0]] == [1]                                                  # nobody wants donghua: no second call

    async def down():
        return [], False
    monkeypatch.setattr(af, "airing_soon", down)
    assert run(af.feed_items(True)) == ([], False)


# ── worker wiring ───────────────────────────────────────────────────────

def test_worker_posts_each_server_only_what_its_setting_allows_and_skips_the_donghua_call_when_unwanted(monkeypatch):
    now = int(time.time())
    asked, posted = [], []
    items = [P(1, at=now - 60, origin="JP"), P(2, at=now - 60, origin="CN")]

    async def feed(include):
        asked.append(include)
        return items, True

    async def no_dm():
        return False

    async def enabled(clone_id=None):
        return guilds
    monkeypatch.setattr(af, "feed_items", feed)
    monkeypatch.setattr(aa, "any_dm_alerts", no_dm)
    monkeypatch.setattr(af, "enabled_guilds", enabled)
    cfgs = lambda kinds: [af.FeedConfig(1, None, True, 100, datetime.fromtimestamp(now - 3600, timezone.utc), content_kinds=kinds)]
    w = aw.Worker(SimpleNamespace(get_guild=lambda g: object()), None, post_gap=0, dm_gap=0)

    async def post(cfg, p):
        posted.append((cfg.content_kinds, p.anime_id))
        return True
    monkeypatch.setattr(w, "_post", post)
    guilds = cfgs("anime")
    run(w.cycle())
    assert asked == [False] and posted == [("anime", 1)]
    guilds, posted[:], asked[:] = cfgs("donghua"), [], []
    run(w.cycle())
    assert asked == [True] and posted == [("donghua", 2)]
    guilds, posted[:], asked[:] = cfgs("both"), [], []
    run(w.cycle())
    assert asked == [True] and sorted(posted) == [("both", 1), ("both", 2)]


# ── wizard setting ──────────────────────────────────────────────────────

def test_wizard_has_a_posts_select_defaulting_to_both_and_it_saves(monkeypatch):
    cfg = af.FeedConfig(5, None, False, 100)
    view = vf.AnimeSetupView(SimpleNamespace(id=5), None, 9, cfg)
    sel = [c for c in view.walk_children() if isinstance(c, discord.ui.Select) and c.placeholder == "What to post"][0]
    assert {o.value: o.default for o in sel.options} == {"anime": False, "donghua": False, "both": True}
    assert "Anime and Donghua" in vf.setup_text(cfg)
    saved = []

    async def save(c, by=None):
        saved.append(c.content_kinds)
    monkeypatch.setattr(af, "save_config", save)

    class R:
        async def edit_message(self, view=None): pass
        def is_done(self): return False
    inter = SimpleNamespace(user=SimpleNamespace(id=9), data={"values": ["anime"]}, response=R())
    run(view._on_kinds(inter))
    assert saved == ["anime"] and cfg.content_kinds == "anime" and "Anime only" in vf.setup_text(cfg)
    inter.data = {"values": ["hax"]}
    run(view._on_kinds(inter))
    assert cfg.content_kinds == "both"                                                                                # junk falls back to the safe default
