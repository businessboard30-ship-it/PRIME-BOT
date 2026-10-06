"""Dex extras: per-rarity completion and the species info page (pure logic + views)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import discord_bot.cogs._views_catch_collection as views
from modules import catch_dex as dx
from modules import catch_emoji
from modules.catch_collection import DexEntry
from modules.catch_species import all_species
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction

LOCALE = json.loads((Path(__file__).resolve().parent.parent.parent / "locales" / "en.json").read_text(encoding="utf-8"))


def entry(sid, *, seen=False, caught=0, shiny=0):
    sp = all_species()[sid]
    return DexEntry(sid, sp["name"], sp["rarity"], sp["element"], seen, caught, shiny)


def full_dex(state=None):
    """Every species as an entry; ``state`` maps species id -> (seen, caught, shiny)."""
    state = state or {}
    return [entry(sid, **dict(zip(("seen", "caught", "shiny"), state.get(sid, (False, 0, 0))))) for sid in sorted(all_species())]


# ---- completion -----------------------------------------------------------

def test_rarity_completion_counts_caught_per_rarity_in_rarity_order():
    entries = full_dex({1: (True, 2, 0), 23: (True, 1, 0), 3: (True, 0, 0)})  # 1,23 common caught; 3 rare only seen
    prog = {p.rarity: p for p in dx.rarity_completion(entries)}
    assert prog["common"].caught == 2 and prog["rare"].caught == 0
    assert sum(p.total for p in prog.values()) == len(entries)
    assert [p.rarity for p in dx.rarity_completion(entries)] == [r for r in ("common", "uncommon", "rare", "epic", "mythic") if r in prog]
    assert dx.rarity_completion([]) == []


# ---- reveal rules ---------------------------------------------------------

def test_unknown_or_unlisted_species_have_no_info():
    entries = full_dex({1: (True, 1, 0)})
    assert dx.species_info(entries, 2) is None  # listed but never seen
    assert dx.species_info(entries, 9999) is None  # not in the roster
    assert dx.species_info([], 1) is None


def test_seen_only_shows_name_rarity_and_type_but_no_stats_habitat_or_evolution():
    info = dx.species_info(full_dex({1: (True, 0, 0)}), 1)
    assert info.name == "Cindrop" and info.rarity == "common" and info.element == "ember"
    assert info.caught is False and info.stats is None and info.habitat is None
    assert info.evolves_to is None and info.evolve_level is None and info.evolves_from is None


def test_caught_shows_stats_habitat_record_and_hides_an_undiscovered_evolution_target():
    info = dx.species_info(full_dex({1: (True, 3, 1)}), 1)
    assert info.caught and info.caught_count == 3 and info.shiny_caught == 1
    assert info.habitat and set(info.stats) == {"vigor", "power", "guard", "speed", "spirit"}
    assert info.evolves_to == dx.UNKNOWN_NAME and info.evolve_level == 16  # Pyrrock not seen yet
    assert "Pyrrock" not in repr(info)


def test_evolution_target_and_source_names_appear_once_discovered():
    dex = full_dex({1: (True, 1, 0), 2: (True, 1, 0), 3: (True, 0, 0)})
    one, two, three = (dx.species_info(dex, i) for i in (1, 2, 3))
    assert one.evolves_to == "Pyrrock" and one.evolves_from is None
    assert two.evolves_from == "Cindrop" and two.evolves_to == "Blazemane" and two.evolve_level == 36
    assert three.evolves_from is None  # seen only: no evolution line at all
    only_two = dx.species_info(full_dex({2: (True, 1, 0)}), 2)
    assert only_two.evolves_from is None  # Cindrop undiscovered: not named


def test_final_form_has_no_evolution_target():
    final = next(i for i, s in all_species().items() if s.get("evolves_to") is None)
    assert dx.species_info(full_dex({final: (True, 1, 0)}), final).evolves_to is None


# ---- views ----------------------------------------------------------------

def test_browse_view_keeps_the_old_buttons_and_offers_only_discovered_species():
    dex = full_dex({1: (True, 1, 0), 3: (True, 0, 0)})
    base, view = views.DexView(7, dex), views.DexBrowseView(7, dex)
    assert [(type(c), c.label) for c in view.children[:2]] == [(type(c), c.label) for c in base.children]
    pick = view.children[2]
    assert [o.value for o in pick.options] == ["1", "3"] and pick.row == 1
    assert len(views.DexBrowseView(7, full_dex()).children) == 2  # nothing discovered: no select
    assert len(view.to_components()) <= 5 and len(pick.options) <= 25


def test_dex_embed_gets_completion_field_and_keeps_the_footer():
    embed = views.DexBrowseView(7, full_dex({1: (True, 1, 0)})).embed()
    field = next(f for f in embed.fields if f.name == LOCALE["catch.dex.completion"])
    assert "Common `1/" in field.value and catch_emoji.mark("rarity", "common") in field.value
    assert embed.footer.text.startswith("Caught 1")


def test_species_pick_edits_in_place_with_no_db_and_back_returns_to_the_same_page():
    rec = Recorder()
    entries = [entry(i, seen=True, caught=1) for i in range(1, 30) if i in all_species()]
    view = views.DexBrowseView(7, entries, page=1)
    edits = []

    async def edit_message(**kw):
        rec.add("response")
        edits.append(kw)

    inter = make_interaction(rec)
    inter.response.edit_message = edit_message
    inter.data = {"values": [str(view.children[2].options[0].value)]}
    asyncio.run(view._open_species(inter))
    assert rec.calls == ["response"] and isinstance(edits[0]["view"], views.DexInfoView)
    assert edits[0]["embed"].title.startswith(f"{int(inter.data['values'][0]):03d} ")
    back_inter = make_interaction(rec)
    back_inter.response.edit_message = edit_message
    asyncio.run(edits[0]["view"]._back(back_inter))
    assert edits[1]["view"] is view and view.page == 1 and edits[1]["embed"].footer.text.endswith("Page 2/3")


def test_species_pick_refuses_undiscovered_ids_and_junk():
    rec = Recorder()
    view = views.DexBrowseView(7, full_dex({1: (True, 1, 0)}))
    sent = []

    async def send_message(*a, **kw):
        sent.append((a, kw))

    for value in ("2", "9999", "x"):
        inter = make_interaction(rec)
        inter.response.send_message = send_message
        inter.data = {"values": [value]}
        asyncio.run(view._open_species(inter))
    assert [a[0] for a, _ in sent] == [LOCALE["catch.dex.info.not_found"]] * 3


def test_info_view_is_owner_only():
    rec = Recorder()
    screen = views.DexInfoView(views.DexBrowseView(7, full_dex()))
    other = make_interaction(rec)
    other.user = SimpleNamespace(id=8)
    assert asyncio.run(screen.interaction_check(other)) is False
    assert asyncio.run(screen.interaction_check(make_interaction(rec))) is True


def test_species_embed_fits_discord_for_every_shipped_species_caught_and_seen():
    for state in ((True, 5, 2), (True, 0, 0)):
        dex = full_dex({i: state for i in all_species()})
        for sid in all_species():
            info = dx.species_info(dex, sid)
            embed = views.species_embed(info)
            assert len(embed.title) <= 256 and len(embed) <= 6000
            assert all(len(f.value) <= 1024 and f.value for f in embed.fields)
            assert (len(embed.fields) > 3) == bool(state[1])


def test_species_embed_shows_stats_only_for_caught():
    seen = views.species_embed(dx.species_info(full_dex({1: (True, 0, 0)}), 1))
    assert LOCALE["catch.dex.info.locked"] in seen.description
    assert not any(f.name == LOCALE["catch.dex.info.stats"] for f in seen.fields)
    caught = views.species_embed(dx.species_info(full_dex({1: (True, 2, 1)}), 1))
    assert any(f.name == LOCALE["catch.dex.info.stats"] for f in caught.fields)
    assert "Shiny ×1" in next(f for f in caught.fields if f.name == LOCALE["catch.dex.info.record"]).value


def test_open_dex_builds_the_browse_view(monkeypatch):
    rec = Recorder()
    from tests.unit.test_catch_interaction_timing import slow
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "load_dex", slow(rec, "db", full_dex({1: (True, 1, 0)})))
    inter = make_interaction(rec)
    sent = []

    async def followup_send(*a, **kw):
        sent.append(kw)

    inter.followup = SimpleNamespace(send=followup_send)
    asyncio.run(views.open_dex(inter))
    assert isinstance(sent[0]["view"], views.DexBrowseView) and rec.calls[0] == "response"
