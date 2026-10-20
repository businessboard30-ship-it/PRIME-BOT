"""Anime feed D3 (Search + Categories): query building, caching, limiter priority, persistent browser buttons, adult filter. No network."""
import asyncio
import re
import time
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_anime_browse as vb
from discord_bot.cogs import _views_anime_feed as vf
from modules import anime_browse as ab
from modules import anime_feed as af


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def clean():
    ab._cache.clear()
    af.provider_reset()
    af._cooldowns.clear()
    yield
    ab._cache.clear()
    af.provider_reset()


def media(i, adult=False, banner="https://b/x.jpg", title="Show"):
    return {"id": i, "siteUrl": f"https://anilist.co/anime/{i}", "isAdult": adult, "format": "TV", "status": "RELEASING", "episodes": 12,
            "genres": ["Action", "Fantasy", "A", "B", "C"], "countryOfOrigin": "CN", "bannerImage": banner,
            "title": {"romaji": title, "english": None, "native": "名"}, "coverImage": {"large": "https://c/l.jpg", "extraLarge": "https://c/xl.jpg", "color": "#112233"},
            "description": "<b>Hi</b> there", "nextAiringEpisode": {"episode": 3, "airingAt": 2_000_000_000}}


# ── pure ────────────────────────────────────────────────────────────────

def test_curated_categories_cover_the_agreed_list_with_unique_short_keys():
    labels = [c.label for c in ab.CATEGORIES]
    for want in ("Love", "Sci-Fi", "Donghua", "Action", "Fantasy / Isekai", "Comedy", "Slice of Life", "Horror", "Sports", "Mecha", "Mystery",
                 "Cultivation / Xianxia", "New this season", "Movies"):
        assert want in labels
    keys = [c.key for c in ab.CATEGORIES]
    assert len(set(keys)) == len(keys) and all(re.fullmatch(r"[a-z0-9_]{1,20}", k) for k in keys) and len(keys) <= 25


def test_query_normalising_and_roundtrip_fit_a_custom_id_even_for_cjk():
    assert ab.normalize_query("  Sousou   NO Frieren ") == "sousou no frieren"
    long = "鬼" * 60
    q = ab.normalize_query(long)
    assert len(q.encode()) <= ab.QUERY_MAX_BYTES and q == "鬼" * (ab.QUERY_MAX_BYTES // 3)
    cid = f"anime_nav:s:{ab.encode_query(long)}:0:9"
    assert len(cid) <= 100 and re.match(vb._NavButton.__discord_ui_compiled_template__, cid)
    assert ab.decode_query(ab.encode_query("Frieren")) == "frieren" and ab.decode_query("###") == ""


def test_category_variables_donghua_season_and_sort():
    d = ab.category_variables(ab.get_category("donghua"))
    assert d["country"] == "CN" and d["sort"] == ["POPULARITY_DESC"] and d["status_in"] == ["RELEASING"]
    assert ab.category_variables(ab.get_category("love"), ab.TOP)["sort"] == ["SCORE_DESC"]
    s = ab.category_variables(ab.get_category("season"), now=time.mktime((2026, 10, 20, 0, 0, 0, 0, 0, 0)))
    assert s["season"] == "FALL" and s["year"] == 2026
    assert ab.current_season(time.mktime((2026, 1, 5, 12, 0, 0, 0, 0, 0)))[0] == "WINTER"
    assert ab.category_variables(ab.get_category("movies"))["format"] == "MOVIE"


def test_parse_drops_adult_and_broken_items_and_only_keeps_https_images():
    payload = {"data": {"Page": {"media": [media(1), media(2, adult=True), {"id": None}, {"id": 4, "title": {}}, None,
                                           media(5, banner="http://insecure/x.jpg")]}}}
    out = ab.parse_media(payload)
    assert [e.anime_id for e in out] == [1, 5]
    assert out[0].image == "https://b/x.jpg" and out[1].image == "https://c/xl.jpg"      # insecure banner ignored, big cover used
    assert out[0].synopsis == "Hi there" and len(out[0].genres) == 4
    assert ab.parse_media({}) == [] and ab.parse_media({"data": {"Page": None}}) == []


# ── limiter + cache ─────────────────────────────────────────────────────

def test_worker_has_priority_over_browsing_and_retry_after_blocks_everyone():
    for _ in range(af.BROWSE_BUDGET):
        af.provider_note()
    assert af.provider_allowed(False) is False and af.provider_allowed(True) is True      # browsing is capped, the worker is not
    af.provider_reset()
    af.provider_block(30)
    assert af.provider_allowed(False) is False and af.provider_allowed(True) is False      # AniList said wait: nobody asks


def test_search_is_cached_and_a_busy_limiter_serves_the_old_copy(monkeypatch):
    calls = []

    async def post(query, variables, priority=False):
        calls.append((variables, priority))
        return {"data": {"Page": {"media": [media(1)]}}}
    monkeypatch.setattr(af, "provider_post", post)
    first, ok = run(ab.search("  Frieren "))
    second, ok2 = run(ab.search("frieren"))
    assert ok and ok2 and len(first) == 1 and len(calls) == 1 and calls[0][1] is False          # one request serves both; browsing is non-priority
    assert calls[0][0]["search"] == "frieren" and calls[0][0]["per"] == ab.SEARCH_RESULTS
    ab._cache[("s", "frieren")] = (time.time() - ab.SEARCH_TTL - 1, first)                       # expired

    async def busy(*a, **k):
        return None
    monkeypatch.setattr(af, "provider_post", busy)
    stale, ok3 = run(ab.search("frieren"))
    assert ok3 and len(stale) == 1                                                               # an old copy beats nothing
    fresh, ok4 = run(ab.search("never searched"))
    assert fresh == [] and ok4 is False                                                          # nothing cached and busy: say so
    assert run(ab.search("a")) == ([], True) and run(ab.category("nope")) == ([], True)


def test_cache_is_bounded(monkeypatch):
    for i in range(ab.CACHE_MAX + 10):
        ab._remember(("s", str(i)), [])
    assert len(ab._cache) <= ab.CACHE_MAX


# ── buttons survive a restart ───────────────────────────────────────────

def test_every_browse_button_has_one_handler_and_rebuilds_from_its_custom_id_alone():
    entries = [ab.parse_media({"data": {"Page": {"media": [media(i) for i in (7, 8, 9)]}}})][0]
    view = vb.browse_view(entries, 1, "c", "donghua", ab.POPULAR, "x")
    ids = [k.custom_id for k in view.walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert {"anime_more:8", "anime_follow:8", "anime_alert:8", "anime_nav:c:donghua:0:0", "anime_nav:c:donghua:0:2", "anime_sort:donghua:1"} <= set(ids)
    assert all(len(i) <= 100 for i in ids)
    for cid in ids + ["anime_search", "anime_cats", "anime_catsel"]:
        classes = [c for c in vf.DYNAMIC_ITEMS if re.match(c.__discord_ui_compiled_template__, cid)]
        assert len(classes) == 1, cid
    nav = run(vb._NavButton.from_custom_id(None, None, re.match(vb._NavButton.__discord_ui_compiled_template__, "anime_nav:s:c2Vhcg==:0:4")))
    assert (nav.kind, nav.arg, nav.sort, nav.index) == ("s", "c2Vhcg==", 0, 4)
    assert len([k for k in view.walk_children()]) <= 40


def test_browse_view_edges_and_card_buttons():
    assert any("Nothing found" in (getattr(k, "content", "") or "") for k in vb.browse_view([], 0, "s", "x").walk_children())
    one = ab.parse_media({"data": {"Page": {"media": [media(1)]}}})
    kids = list(vb.browse_view(one, 99, "s", ab.encode_query("x")).walk_children())            # an out-of-range index is clamped
    navs = [k for k in kids if isinstance(k, vb._NavButton)]
    assert all(n.item.disabled for n in navs)                                                  # a single result: both Prev and Next off
    assert not any(isinstance(k, vb._SortButton) for k in kids)                                # Sort is for categories only
    card = [getattr(k, "item", k).label for k in vf.post_view(af.Preview(1, "T", 1, 2_000_000_000, "s", None, "https://anilist.co/anime/1")).walk_children()
            if isinstance(k, (discord.ui.Button, discord.ui.DynamicItem))]
    assert card[-2:] == ["Search", "Categories"]


# ── callbacks ───────────────────────────────────────────────────────────

class Resp:
    def __init__(self): self.log = []
    def is_done(self): return bool(self.log)
    async def defer(self, **kw): self.log.append(("defer", kw))
    async def send_message(self, *a, **kw): self.log.append(("send", a, kw))
    async def send_modal(self, m): self.log.append(("modal", m))


def inter(uid=1, values=None):
    sent = []

    class F:
        async def send(self, *a, **kw): sent.append((a, kw))
    return SimpleNamespace(user=SimpleNamespace(id=uid), guild=None, response=Resp(), followup=F(), data={"values": values or []},
                           edit_original_response=None, sent=sent)


def test_search_button_opens_a_modal_first_and_the_modal_respects_the_cooldown(monkeypatch):
    i = inter()
    run(vb.SearchButton().callback(i))
    assert i.response.log[0][0] == "modal"
    opened = []

    async def fake_open(interaction, kind, arg, sort):
        opened.append((kind, ab.decode_query(arg)))
    monkeypatch.setattr(vb, "_open_browser", fake_open)
    m = vb.SearchModal()
    m.query._value = "  Frieren "
    a = inter(5)
    run(m.on_submit(a))
    assert opened == [("s", "frieren")] and a.response.log[0][0] == "defer"
    b = inter(5)
    run(m.on_submit(b))                                                                        # a second search straight away
    assert opened == [("s", "frieren")] and b.response.log[0][2]["ephemeral"] is True and "limited" in b.response.log[0][1][0]


def test_category_select_defers_then_opens_and_rejects_unknown_keys(monkeypatch):
    opened = []

    async def fake_open(interaction, kind, arg, sort):
        opened.append((kind, arg, sort))
    monkeypatch.setattr(vb, "_open_browser", fake_open)
    good = inter(2, ["donghua"])
    run(vb.CategorySelect().callback(good))
    assert opened == [("c", "donghua", ab.POPULAR)] and good.response.log[0][0] == "defer"
    bad = inter(3, ["../etc"])
    run(vb.CategorySelect().callback(bad))
    assert opened == [("c", "donghua", ab.POPULAR)] and "isn't available" in bad.response.log[0][1][0]


def test_next_after_a_restart_requeries_and_edits_the_same_private_message(monkeypatch):
    shown = {}

    async def post(query, variables, priority=False):
        return {"data": {"Page": {"media": [media(i) for i in (1, 2, 3)]}}}
    monkeypatch.setattr(af, "provider_post", post)

    async def edit(view=None, **kw):
        shown["view"] = view
    i = inter(9)
    i.edit_original_response = edit
    cid = "anime_nav:c:love:0:2"
    item = run(vb._NavButton.from_custom_id(None, None, re.match(vb._NavButton.__discord_ui_compiled_template__, cid)))
    run(item.callback(i))
    assert i.response.log[0][0] == "defer"
    text = " ".join(getattr(k, "content", "") or "" for k in shown["view"].walk_children())
    assert "result 3 of 3" in text and "Love" in text


def test_top_picks_goes_through_the_plus_gate(monkeypatch):
    async def no(*a, **k): return False
    monkeypatch.setattr(af, "has_anime_plus", no)
    i = inter(4)
    run(vb._SortButton("love", ab.TOP).callback(i))
    assert "Anime+" in i.response.log[0][1][0] and i.response.log[0][2]["ephemeral"] is True
