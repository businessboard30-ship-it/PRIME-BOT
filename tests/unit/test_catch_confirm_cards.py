"""Evolve and release confirm cards: rendering, fallback, and how the creature screen uses them."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import discord_bot.cogs._views_catch_creature as views
from modules import catch_confirm_card as cc
from modules import catch_species
from modules.catch_card import H, W
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow
from tests.unit.test_catch_phase3 import detail, edits_to, make_view, ready_preview

PNG = b"\x89PNG\r\n\x1a\n"
STATS_A = {"vigor": 50, "power": 50, "guard": 50, "speed": 50, "spirit": 50}
STATS_B = {"vigor": 79, "power": 92, "guard": 73, "speed": 40, "spirit": 46}


def sp(i):
    return catch_species.get(i)


def evo(**over):
    kw = dict(level=16, shiny=False, special=False, before=STATS_A, after=STATS_B)
    kw.update(over)
    return asyncio.run(cc.evolve_card_png(sp(1), sp(2), **kw))


def rel(species=None, **over):
    kw = dict(level=22, shiny=False, special=False, buddy=False)
    kw.update(over)
    return asyncio.run(cc.release_card_png(species or sp(2), **kw))


def allow(monkeypatch, rec):
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))


def test_cards_are_real_pngs_of_the_documented_size():
    for data in (evo(), rel()):
        assert data.startswith(PNG) and Image.open(io.BytesIO(data)).size == (W, H)


def test_evolve_card_changes_with_what_it_shows():
    base = evo()
    assert evo(level=17) != base
    assert evo(shiny=True) != base
    assert evo(after=dict(STATS_B, vigor=5)) != base
    assert evo(before=dict(STATS_A, power=1)) != base
    assert asyncio.run(cc.evolve_card_png(sp(1), sp(3), level=16, before=STATS_A, after=STATS_B)) != base


def test_release_card_changes_with_what_it_shows():
    base = rel()
    assert rel(level=23) != base
    assert rel(buddy=True) != base
    assert rel(shiny=True) != base
    assert rel(sp(6)) != base


def test_edge_values_do_not_crash():
    assert evo(before={}, after={}).startswith(PNG)
    assert evo(before={k: 9999 for k in STATS_A}, after={k: 0 for k in STATS_A}).startswith(PNG)
    assert rel(level=1).startswith(PNG) and rel(level=9999, shiny=True, buddy=True).startswith(PNG)
    long = dict(sp(2), name="A" * 80)
    assert rel(long).startswith(PNG)
    assert asyncio.run(cc.evolve_card_png(long, dict(sp(3), name="B" * 80), level=5)).startswith(PNG)


def test_bad_input_raises_so_callers_fall_back():
    with pytest.raises(Exception):
        asyncio.run(cc.release_card_png(None, level=1))
    with pytest.raises(Exception):
        asyncio.run(cc.evolve_card_png(sp(1), None, level=1))


def test_renders_are_cached_and_cache_clears():
    assert cc._cached_release.cache_info().currsize >= 0
    rel()
    hits = cc._cached_release.cache_info().hits
    rel()
    assert cc._cached_release.cache_info().hits == hits + 1
    cc.clear_confirm_cache()
    assert cc._cached_release.cache_info().currsize == 0 and cc._cached_evolve.cache_info().currsize == 0


def test_confirm_file_is_fresh_each_time():
    a, b = cc.confirm_file(b"x", "evolve.png"), cc.confirm_file(b"x", "evolve.png")
    assert a is not b and a.filename == "evolve.png"


# ---------------------------------------------------------------- messages

def test_evolve_message_sets_image_and_drops_only_the_stats_field():
    d, pv = detail(), ready_preview()
    plain = views.evolve_confirm_embed(pv, d.nickname)
    msg = asyncio.run(views.evolve_confirm_message(pv, d))
    assert msg["embed"].image.url == "attachment://evolve.png"
    assert [f.filename for f in msg["attachments"]] == ["evolve.png"]
    assert len(plain.fields) == 1 and msg["embed"].fields == []
    assert msg["embed"].description == plain.description and msg["embed"].title == plain.title


def test_evolve_message_falls_back_to_the_full_text_embed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no pillow today")

    monkeypatch.setattr(views, "evolve_card_png", boom)
    d, pv = detail(), ready_preview()
    msg = asyncio.run(views.evolve_confirm_message(pv, d))
    assert msg["attachments"] == [] and msg["embed"].image.url is None
    assert msg["embed"].to_dict() == views.evolve_confirm_embed(pv, d.nickname).to_dict()


def test_evolve_message_with_unknown_species_is_plain():
    msg = asyncio.run(views.evolve_confirm_message(ready_preview(), detail(species_id=999999)))
    assert msg["attachments"] == [] and msg["embed"].image.url is None and len(msg["embed"].fields) == 1


def test_evolve_message_with_no_target_is_plain():
    final = max(catch_species.all_species().values(), key=lambda s: s["id"] if not s.get("evolves_to") else -1)
    msg = asyncio.run(views.evolve_confirm_message(ready_preview(), detail(species_id=final["id"])))
    assert msg["attachments"] == [] and msg["embed"].image.url is None


def test_release_message_keeps_all_text_and_adds_the_image():
    d = detail(is_buddy=True)
    plain = views.release_confirm_embed(d)
    msg = asyncio.run(views.release_confirm_message(d))
    assert msg["embed"].image.url == "attachment://release.png"
    assert [f.filename for f in msg["attachments"]] == ["release.png"]
    assert msg["embed"].description == plain.description and "buddy" in msg["embed"].description.lower()


def test_release_message_falls_back_to_plain(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no pillow today")

    monkeypatch.setattr(views, "release_card_png", boom)
    d = detail()
    msg = asyncio.run(views.release_confirm_message(d))
    assert msg["attachments"] == [] and msg["embed"].to_dict() == views.release_confirm_embed(d).to_dict()


def test_release_message_with_unknown_species_is_plain():
    msg = asyncio.run(views.release_confirm_message(detail(species_id=999999)))
    assert msg["attachments"] == [] and msg["embed"].image.url is None


def test_nicknames_never_reach_the_renderers(monkeypatch):
    seen = []
    real_e, real_r = cc.evolve_card_png, cc.release_card_png

    async def spy_e(*a, **k):
        seen.append((a, k))
        return await real_e(*a, **k)

    async def spy_r(*a, **k):
        seen.append((a, k))
        return await real_r(*a, **k)

    monkeypatch.setattr(views, "evolve_card_png", spy_e)
    monkeypatch.setattr(views, "release_card_png", spy_r)
    d = detail(nickname="Zoë ✨ Sparky")
    asyncio.run(views.evolve_confirm_message(ready_preview(nickname=d.nickname), d))
    asyncio.run(views.release_confirm_message(d))
    assert len(seen) == 2 and "Sparky" not in repr(seen)


# ---------------------------------------------------------------- screens

def test_buttons_show_their_own_card_and_respond_first(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    monkeypatch.setattr(views, "preview", slow(rec, "preview", ready_preview()))
    for call, name in (("_release", "release.png"), ("_evolve", "evolve.png")):
        view = make_view(rec)
        inter = make_interaction(rec)
        edits = edits_to(inter)
        asyncio.run(getattr(view, call)(inter))
        assert [f.filename for f in edits[0]["attachments"]] == [name]
        assert edits[0]["embed"].image.url == f"attachment://{name}"


def test_cancelling_a_confirm_screen_returns_to_the_creature_card_not_the_confirm_card(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def fake_load(*a, **k):
        return detail()

    async def fake_prev(*a, **k):
        return ready_preview()

    monkeypatch.setattr(views, "load_creature", fake_load)
    monkeypatch.setattr(views, "preview", fake_prev)
    for confirm_cls in (views.EvolveConfirmView, views.ReleaseConfirmView):
        cv = make_view(rec)
        confirm = confirm_cls(7, None, cv)
        inter = make_interaction(rec)
        edits = edits_to(inter)
        asyncio.run(confirm._cancel(inter))
        names = [f.filename for e in edits for f in e.get("attachments", [])]
        assert names == ["creature.png"]


def test_views_hand_the_renderers_the_real_values(monkeypatch):
    got = {}

    async def spy_e(species, target, **k):
        got["evolve"] = (species["id"], target["id"], k)
        return b"\x89PNG"

    async def spy_r(species, **k):
        got["release"] = (species["id"], k)
        return b"\x89PNG"

    monkeypatch.setattr(views, "evolve_card_png", spy_e)
    monkeypatch.setattr(views, "release_card_png", spy_r)
    d = detail(shiny=True, special=True, is_buddy=True, level=23)
    pv = ready_preview(level=23, before=STATS_A, after=STATS_B)
    asyncio.run(views.evolve_confirm_message(pv, d))
    asyncio.run(views.release_confirm_message(d))
    assert got["evolve"] == (1, 2, dict(level=23, shiny=True, special=True, before=STATS_A, after=STATS_B))
    assert got["release"] == (1, dict(level=23, shiny=True, special=True, buddy=True))
