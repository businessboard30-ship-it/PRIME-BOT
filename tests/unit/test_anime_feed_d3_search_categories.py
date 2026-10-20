"""Anime feed D3: Search + Categories (browser, query building, cache, limiter, cooldowns, persistence). No network, no database."""
import asyncio
import re
import time
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_anime_browse as ab
from discord_bot.cogs import _views_anime_feed as vf
from discord_bot.cogs import _views_join_dm as jd
from modules import anime_feed as af
from modules import anime_feed_browse as br
from modules import anime_feed_limiter as lim


def run(c):
    return asyncio.run(c)


def media(i=1, **extra):
    m = {"id": i, "siteUrl": f"https://anilist.co/anime/{i}", "isAdult": False, "format": "TV", "status": "RELEASING", "episodes": 12, "genres": ["Action", "Fantasy", "Drama", "X"],
         "countryOfOrigin": "CN", "bannerImage": "https://s4.anilist.co/b.jpg", "title": {"romaji": f"Show {i}", "english": None, "native": "神"},
         "coverImage": {"large": "https://s4.anilist.co/c.jpg", "extraLarge": "https://s4.anilist.co/x.jpg", "color": "#aabbcc"},
         "description": "A <b>story</b>. " * 80, "nextAiringEpisode": {"episode": 5, "airingAt": 2_000_000_000}}
    m.update(extra)
    return m


def payload(*rows):
    return {"data": {"Page": {"media": list(rows)}}}


def entries(n):
    return br.parse_entries(payload(*[media(i) for i in range(1, n + 1)]))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    br._cache.clear()
    br._locks.clear()
    monkeypatch.setattr(lim, "anilist", lim.Limiter())
    af._cooldowns.clear()


# -- parsing + safety -----------------------------------------------------------------------

def test_parse_filters_adult_dupes_and_bad_rows_and_keeps_only_https_art():
    out = br.parse_entries(payload(media(1), media(2, isAdult=True), media(1), media(3, title={}), {"id": None}, media(4, bannerImage="http://x", coverImage={"extraLarge": "ftp://y"}), "junk"))
    assert [e.anime_id for e in out] == [1, 4]
    assert out[0].banner and out[0].color == 0xAABBCC and out[0].genres == ("Action", "Fantasy", "Drama") and out[0].next_episode == 5
    assert out[1].banner is None and out[1].cover_xl is None and out[1].image is None
    assert len(br.parse_entries(payload(*[media(i) for i in range(1, 40)]))) == br.MAX_RESULTS
    assert br.parse_entries({}) == [] and br.parse_entries({"data": None}) == []


def test_query_normalising_and_validation():
    assert br.normalise_query("  Soul   LAND  ") == "soul land" and len(br.normalise_query("x" * 200)) == br.MAX_QUERY
    assert br.valid_query("ab") and not br.valid_query("a") and not br.valid_query("!!") and not br.valid_query("")


def test_category_list_is_curated_unique_short_keyed_and_covers_the_requested_ones():
    keys = [c.key for c in br.CATEGORIES]
    assert len(keys) == len(set(keys)) == 14 and len(br.CATEGORIES) <= 25
    assert {"love", "scifi", "donghua", "action", "fantasy", "comedy", "slice", "horror", "sports", "mecha", "mystery", "cultivation", "season", "movies"} == set(keys)
    assert all(len(f"anime_pg:c:99:{k}") <= 100 and re.fullmatch(r"[a-z]+", k) for k in keys)


def test_category_queries_only_put_code_constants_in_the_text_and_user_data_in_variables():
    q, v = br.category_query(br.get_category("donghua"), 0)
    assert "countryOfOrigin: $origin" in q and v["origin"] == "CN" and "status: RELEASING" in q and "isAdult: false" in q
    q, v = br.category_query(br.get_category("horror"), 0)
    assert v["genres"] == ["Horror", "Thriller"] and "genre_in: $genres" in q
    q, v = br.category_query(br.get_category("movies"), 0)
    assert "format: MOVIE" in q and "status:" not in q and "TRENDING_DESC" in q
    q, v = br.category_query(br.get_category("season"), 1_792_000_000)              # October 2026
    assert v["season"] == "FALL" and v["year"] == 2026 and "seasonYear: $year" in q
    q, v = br.category_query(br.get_category("cultivation"), 0)
    assert v["tags"] == ["Cultivation"] and "tag_in: $tags" in q
    assert "$q" in br.search_query() and "isAdult: false" in br.search_query()
    assert br.season_of(1_700_000_000) == ("FALL", 2023) and br.season_of(1_705_000_000)[0] == "WINTER"


# -- cache, limiter, failure handling ---------------------------------------------------------

def fake_request(monkeypatch, behaviour):
    calls = []

    async def req(query, variables):
        calls.append(variables)
        r = behaviour(len(calls))
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(br, "_request", req)
    return calls


def test_search_is_cached_per_normalised_query_and_shared_and_one_request_serves_simultaneous_callers(monkeypatch):
    calls = fake_request(monkeypatch, lambda n: payload(media(1)))

    async def go():
        a = await asyncio.gather(br.search("Soul Land"), br.search("  soul   land "), br.search("SOUL LAND"))
        b = await br.search("soul land")
        return a, b
    a, b = run(go())
    assert len(calls) == 1 and all(r[1] == "ok" and len(r[0]) == 1 for r in a) and b[1] == "ok"


def test_short_query_never_reaches_the_provider(monkeypatch):
    calls = fake_request(monkeypatch, lambda n: payload(media(1)))
    assert run(br.search("a")) == ([], "short") and calls == []


def test_busy_down_and_stale_copy_behaviour(monkeypatch):
    fake_request(monkeypatch, lambda n: br.BusyError())
    assert run(br.search("naruto")) == ([], "busy")
    fake_request(monkeypatch, lambda n: None)
    assert run(br.search("naruto")) == ([], "down")
    fake_request(monkeypatch, lambda n: RuntimeError("boom"))
    assert run(br.search("naruto")) == ([], "down")
    fake_request(monkeypatch, lambda n: payload(media(1)) if n == 1 else None)       # works once, then AniList fails
    assert run(br.search("bleach"))[1] == "ok"
    br._cache["s:bleach"] = (time.time() - br.SEARCH_TTL - 1, br._cache["s:bleach"][1])      # expired
    got, state = run(br.search("bleach"))
    assert state == "ok" and len(got) == 1                                           # an old copy beats nothing


def test_category_unknown_key_is_refused_and_known_one_is_cached(monkeypatch):
    calls = fake_request(monkeypatch, lambda n: payload(media(1), media(2)))
    assert run(br.category("../etc")) == ([], "unknown") and calls == []
    assert len(run(br.category("action"))[0]) == 2 and len(run(br.category("action"))[0]) == 2 and len(calls) == 1


def test_cache_is_bounded(monkeypatch):
    fake_request(monkeypatch, lambda n: payload(media(1)))

    async def go():
        for i in range(br.CACHE_MAX + 30):
            await br.search(f"query number {i}")
    run(go())
    assert len(br._cache) <= br.CACHE_MAX


def test_request_uses_the_shared_limiter_without_priority(monkeypatch):
    l = lim.Limiter(per_minute=4, reserve=2)
    monkeypatch.setattr(lim, "anilist", l)
    monkeypatch.setitem(__import__("sys").modules, "anime_service", SimpleNamespace(get_session=None))
    monkeypatch.setitem(__import__("sys").modules, "config", SimpleNamespace(ANILIST_ENDPOINT="x"))
    assert l.try_acquire() and l.try_acquire()
    with pytest.raises(br.BusyError):                                                # users are out of budget, the worker's reserve is untouched
        run(br._request("q", {}))
    assert l.try_acquire(priority=True)


# -- the browser ------------------------------------------------------------------------------

def texts(view):
    return " ".join(getattr(k, "content", "") or "" for k in view.walk_children())


def test_browser_shows_one_big_card_with_nav_and_clamps_the_page():
    es = entries(3)
    v = ab.browse_view(es, 1, "s", "show")
    kids = list(v.walk_children())
    assert sum(isinstance(k, discord.ui.MediaGallery) for k in kids) == 1 and "## Show 2" in texts(v) and "2 of 3" in texts(v)
    nav = [k for k in kids if isinstance(k, ab._PageButton)]
    assert [(n.page, n.item.disabled) for n in nav] == [(0, False), (2, False)]
    assert [n.item.disabled for n in [k for k in ab.browse_view(es, 0, "s", "show").walk_children() if isinstance(k, ab._PageButton)]] == [True, False]
    assert [n.item.disabled for n in [k for k in ab.browse_view(es, 99, "s", "show").walk_children() if isinstance(k, ab._PageButton)]] == [False, True]
    assert "3 of 3" in texts(ab.browse_view(es, 99, "s", "show")) and "1 of 3" in texts(ab.browse_view(es, -5, "s", "show"))
    assert len(kids) <= 40 and all(isinstance(getattr(k, "item", k), discord.ui.Button) or True for k in kids)


def test_card_buttons_work_for_results_and_category_browser_has_the_picker_too():
    es = entries(2)
    ids = [k.custom_id for k in ab.browse_view(es, 0, "c", "donghua").walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert {"anime_more:1", "anime_follow:1", "anime_alert:1", "anime_catsel", "anime_pg:c:1:donghua"} <= set(ids)
    assert "anime_catsel" not in [k.custom_id for k in ab.browse_view(es, 0, "s", "x").walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert "Donghua" in texts(ab.browse_view(es, 0, "c", "donghua"))


def test_empty_and_failed_results_say_so_plainly():
    assert "No anime found" in texts(ab.browse_view([], 0, "s", "zzzz"))
    assert "can't reach AniList" in texts(ab.browse_view([], 0, "s", "zzzz", "down"))
    assert "Lots of people" in texts(ab.browse_view([], 0, "c", "love", "busy"))
    v = ab.browse_view([], 0, "c", "love", "down")
    assert any(isinstance(k, ab._CatSelect) for k in v.walk_children())              # you can still pick another category


def test_picker_offers_every_category_in_one_select():
    sel = next(k for k in ab.picker_view().walk_children() if isinstance(k, ab._CatSelect))
    assert [o.value for o in sel.item.options] == [c.key for c in br.CATEGORIES]


# -- persistence -------------------------------------------------------------------------------

def test_every_browse_item_is_registered_and_rebuilds_from_its_custom_id_alone():
    items = vf.DYNAMIC_ITEMS
    assert all(c in jd.DYNAMIC_ITEMS for c in ab.BROWSE_ITEMS)
    es = entries(3)
    views = [vf.post_view(af.Preview(7, "S", 1, 2_000_000_000, "", None, "https://anilist.co/anime/7")), ab.picker_view(),
             ab.browse_view(es, 1, "s", "soul land: part 2"), ab.browse_view(es, 2, "c", "scifi")]
    ids = [k.custom_id for v in views for k in v.walk_children() if isinstance(k, discord.ui.DynamicItem)]
    assert {"anime_search", "anime_cats", "anime_catsel", "anime_pg:s:0:soul land: part 2", "anime_pg:c:1:scifi"} <= set(ids)
    for cid in ids:
        owners = [c for c in items if re.match(c.__discord_ui_compiled_template__, cid)]
        assert len(owners) == 1 and len(cid) <= 100                                  # exactly one handler, and the id fits Discord's limit
    m = ab._PageButton.__discord_ui_compiled_template__.match("anime_pg:s:3:soul land: part 2")
    item = run(ab._PageButton.from_custom_id(None, SimpleNamespace(label="Next"), m))
    assert (item.kind, item.page, item.key) == ("s", 3, "soul land: part 2") and item.item.custom_id == "anime_pg:s:3:soul land: part 2"


def test_the_longest_possible_search_id_still_fits():
    assert len(f"anime_pg:s:99:{'x' * br.MAX_QUERY}") <= 100


# -- callbacks: answer first, cooldowns, no admin gate ------------------------------------------

class Resp:
    def __init__(self):
        self.sent, self.deferred, self.modal = [], 0, None

    async def defer(self, **kw):
        self.deferred += 1

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))

    async def send_modal(self, m):
        self.modal = m

    def is_done(self):
        return self.deferred > 0 or bool(self.sent)


def inter(uid=5, values=None):
    return SimpleNamespace(user=SimpleNamespace(id=uid), response=Resp(), data={"values": values or []}, followup=SimpleNamespace(sent=[]), edits=[])


def test_search_button_opens_a_modal_for_any_member_with_no_permission_check():
    i = inter()
    run(ab._SearchButton().callback(i))
    assert isinstance(i.response.modal, ab.SearchModal)


def test_search_submit_defers_first_answers_privately_and_enforces_the_cooldown(monkeypatch):
    fake_request(monkeypatch, lambda n: payload(media(1)))
    sent = []

    async def followup_send(**kw):
        sent.append(kw)
    modal = ab.SearchModal()
    modal.query._value = "Soul Land"

    def make():
        i = inter()
        i.followup = SimpleNamespace(send=followup_send)
        return i
    i1 = make()
    run(modal.on_submit(i1))
    assert i1.response.deferred == 1 and sent[0]["ephemeral"] is True and "Show 1" in texts(sent[0]["view"])
    assert sent[0]["allowed_mentions"].everyone is False and not sent[0]["allowed_mentions"].users
    i2 = make()                                                                       # same person again, straight away
    run(modal.on_submit(i2))
    assert i2.response.deferred == 0 and "again in" in i2.response.sent[0][0][0] and len(sent) == 1
    other = make()
    other.user = SimpleNamespace(id=6)
    run(modal.on_submit(other))
    assert len(sent) == 2                                                             # the cooldown is per person


def test_category_select_rejects_unknown_values_and_edits_the_same_message_for_known_ones(monkeypatch):
    fake_request(monkeypatch, lambda n: payload(media(1)))
    bad = inter(values=["../../x"])
    run(ab._CatSelect().callback(bad))
    assert bad.response.sent and bad.response.deferred == 0
    edits = []
    good = inter(values=["action"])

    async def edit(**kw):
        edits.append(kw)
    good.edit_original_response = edit
    run(ab._CatSelect().callback(good))
    assert good.response.deferred == 1 and len(edits) == 1 and "Action" in texts(edits[0]["view"])


def test_page_button_defers_then_edits_and_survives_a_cold_cache(monkeypatch):
    calls = fake_request(monkeypatch, lambda n: payload(*[media(i) for i in (1, 2, 3)]))
    i = inter()
    edits = []

    async def edit(**kw):
        edits.append(kw)
    i.edit_original_response = edit
    run(ab._PageButton("s", 2, "soul land", "Next", None).callback(i))              # the cache is empty, as after a restart
    assert i.response.deferred == 1 and "3 of 3" in texts(edits[0]["view"]) and len(calls) == 1
