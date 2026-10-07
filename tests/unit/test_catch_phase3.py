"""Phase 3: emoji table, nicknames, evolution rules, creature + profile screens, wiring."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

import discord_bot.cogs._views_catch_collection as collection_views
import discord_bot.cogs._views_catch_creature as views
import discord_bot.cogs._views_catch_profile as profile_views
from modules import catch_collection as cc
from modules import catch_creature as creature
from modules import catch_emoji as emoji
from modules import catch_evolve as evo
from modules import catch_game
from modules.catch_i18n import text as catch_text
from modules.catch_profile import ProfileCreature, TrainerProfile
from modules.catch_species import all_species
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.notice_helpers import body

ROOT = Path(__file__).resolve().parent.parent.parent
LOCALE = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def detail(**over):
    base = dict(
        id=11, species_id=1, name="Cindrop", rarity="common", element="ember", element2=None, habitat="land",
        level=16, xp=120, nickname=None, shiny=False, special=False, favorite=False, locked=False,
        is_buddy=False, stats={"vigor": 50, "power": 40, "guard": 30, "speed": 20, "spirit": 10},
        iv_percent=55.5, source="wild", caught_at=NOW, caught_in_guild=1,
    )
    base.update(over)
    return creature.CreatureDetail(**base)


def ready_preview(**over):
    rule = evo.EvolutionRule(1, 2, 16, None)
    base = dict(
        owned_id=11, level=16, from_name="Cindrop", to_name="Pyrrock",
        eligibility=evo.Eligibility("ready", rule, needed_level=16),
        before={n: 10 for n in catch_game.STAT_NAMES}, after={n: 20 for n in catch_game.STAT_NAMES},
        nickname=None, item_owned=0,
    )
    base.update(over)
    return evo.EvolutionPreview(**base)


# ---------------------------------------------------------------- emoji table

def test_emoji_table_covers_every_rarity_element_stat_and_flag():
    assert set(emoji.RARITY) == set(catch_game.RARITY_BY_KEY)
    assert set(emoji.ELEMENT) == set(catch_game.ELEMENTS)
    assert set(emoji.STAT) == set(catch_game.STAT_NAMES)
    assert {"shiny", "special", "favorite", "locked", "buddy"} <= set(emoji.FLAG)
    for group in emoji.GROUPS.values():
        assert all(isinstance(v, str) and v.strip() for v in group.values())


def test_emoji_unknown_keys_never_crash_and_flags_follow_the_table():
    assert emoji.mark("rarity", "nope") == emoji.FALLBACK and emoji.mark("nogroup", "x") == emoji.FALLBACK
    assert emoji.flags() == ""
    both = emoji.flags(shiny=True, locked=True)
    assert emoji.FLAG["shiny"] in both and emoji.FLAG["locked"] in both


def test_bar_shows_at_least_one_block_and_never_overflows():
    assert emoji.bar(0) == emoji.BAR_EMPTY * emoji.BAR_LENGTH
    assert emoji.bar(1).count(emoji.BAR_FULL) == 1
    assert emoji.bar(10**6) == emoji.BAR_FULL * emoji.BAR_LENGTH
    assert all(len(emoji.bar(v)) == emoji.BAR_LENGTH for v in (0, 1, 150, 300, 999))


def test_old_rarity_tables_now_share_the_emoji_file():
    from modules import catch_sell, catch_wild
    assert cc.RARITY_MARK is emoji.RARITY and catch_sell.RARITY_MARK is emoji.RARITY and catch_wild.RARITY_MARK is emoji.RARITY


# ---------------------------------------------------------------- nicknames

@pytest.mark.parametrize("raw,ok,value", [
    (None, True, None), ("", True, None), ("   ", True, None),
    ("Sparky", True, "Sparky"), ("  Big   Sparky ", True, "Big Sparky"), ("Zoë ✨", True, "Zoë ✨"),
    ("a" * 24, True, "a" * 24),
])
def test_clean_nickname_accepts_and_normalises(raw, ok, value):
    check = creature.clean_nickname(raw)
    assert check.ok is ok and check.value == value


@pytest.mark.parametrize("raw,reason", [
    ("a" * 25, "too_long"),
    ("hi @everyone", "invalid_chars"), ("<#123>", "invalid_chars"), ("back`tick", "invalid_chars"),
    ("tab\x00bell", "invalid_chars"), ("zero\u200bwidth", "invalid_chars"),
    ("see http-x", "link"), ("x.gg/abc", "link"), ("WWW.site", "link"),
])
def test_clean_nickname_refuses(raw, reason):
    check = creature.clean_nickname(raw)
    assert not check.ok and check.reason == reason and check.value is None


def test_clean_nickname_uses_the_bundled_banned_word_list():
    words = [w.strip().lower() for w in creature.BANNED_WORDS_PATH.read_text(encoding="utf-8").splitlines()
             if w.strip().isalpha() and len(w.strip()) >= 4]
    assert words, "banned word list missing"
    word = words[0]
    assert creature.clean_nickname(word).reason == "blocked"
    assert creature.clean_nickname(f"my {word.upper()} pal").reason == "blocked"
    assert creature.clean_nickname("Classy").ok and creature.clean_nickname("Shell").ok  # whole words only


def test_every_nickname_reason_has_a_message():
    for reason in ("too_long", "invalid_chars", "link", "blocked"):
        assert f"catch.creature.nick_{reason}" in LOCALE


# ---------------------------------------------------------------- evolution rules (pure)

def test_evaluate_covers_every_branch_with_the_real_roster():
    assert evo.evaluate(1, 15).status == "level_too_low" and evo.evaluate(1, 15).needed_level == 16
    ready = evo.evaluate(1, 16)
    assert ready.status == "ready" and ready.rule.to_id == 2
    assert evo.evaluate(3, 100).status == "final_form"  # Blazemane has no next form
    assert evo.evaluate(9999, 50).status == "final_form"  # unknown species never evolves


def test_every_shipped_evolution_points_at_a_real_species():
    for sid, sp in all_species().items():
        if sp.get("evolves_to"):
            assert evo.evaluate(sid, 100).status in {"ready", "needs_item"}, sid


def test_item_triggers_need_the_item(monkeypatch):
    species = {k: dict(v) for k, v in all_species().items()}
    species[1].update(evolve_level=None, evolve_item="ember_stone")
    monkeypatch.setattr(evo, "all_species", lambda: species)
    assert evo.evaluate(1, 1, 0).status == "needs_item"
    assert evo.evaluate(1, 1, 1).status == "ready"
    species[1].update(evolve_level=10)
    assert evo.evaluate(1, 9, 5).status == "level_too_low"  # level is checked before the item


def test_species_with_no_trigger_or_missing_target_cannot_evolve(monkeypatch):
    species = {k: dict(v) for k, v in all_species().items()}
    species[1].update(evolve_level=None, evolve_item=None)
    monkeypatch.setattr(evo, "all_species", lambda: species)
    assert evo.evaluate(1, 100).status == "no_trigger"
    species[1].update(evolves_to=9999, evolve_level=5)
    assert evo.evaluate(1, 100).status == "unknown_target"


def test_every_refusal_reason_has_a_message():
    for reason in ("not_found", "final_form", "level_too_low", "needs_item", "no_item", "no_trigger",
                   "unknown_target", "changed"):
        assert f"catch.evolve.refused_{reason}" in LOCALE


# ---------------------------------------------------------------- creature embed

def test_creature_embed_fits_discord_limits_and_escapes_markdown():
    d = detail(nickname="*_Big_*", shiny=True, special=True, favorite=True, locked=True, is_buddy=True, level=100,
               stats={n: 999 for n in catch_game.STAT_NAMES}, element2="stone", rarity="mythic")
    embed = views.creature_embed(d, ready_preview())
    assert len(embed) <= 6000 and len(embed.title) <= 256
    assert "\\*\\_Big\\_\\*" in embed.title
    assert [f.name for f in embed.fields][0] == LOCALE["catch.creature.stats"]
    assert all(len(f.value) <= 1024 for f in embed.fields)
    assert emoji.FLAG["buddy"] in embed.title and emoji.FLAG["locked"] in embed.title


def test_creature_embed_shows_each_evolution_state():
    assert "final form" in views.creature_embed(detail(), evo_pv("final_form")).fields[2].value.lower()
    assert "Pyrrock" in views.creature_embed(detail(), evo_pv("ready")).fields[2].value
    assert "16" in views.creature_embed(detail(), evo_pv("level_too_low", needed_level=16)).fields[2].value
    assert views.creature_embed(detail(), None).fields[2].value == LOCALE["catch.creature.evo_none"]


def evo_pv(status, **elig):
    rule = evo.EvolutionRule(1, 2, 16, None)
    return ready_preview(eligibility=evo.Eligibility(status, rule if status != "final_form" else None, **elig))


def test_evolve_confirm_embed_shows_before_and_after_and_item_cost():
    pv = ready_preview(eligibility=evo.Eligibility("ready", evo.EvolutionRule(1, 2, None, "ember_stone"), needed_item="ember_stone"))
    embed = views.evolve_confirm_embed(pv, "Sparky")
    assert "Sparky" in embed.title and "Pyrrock" in embed.description and "Ember Stone" in embed.description
    assert len(embed.fields[0].value.splitlines()) == len(catch_game.STAT_NAMES)


# ---------------------------------------------------------------- creature view

def make_view(rec, **kw):
    back_calls = []

    async def back(interaction):
        back_calls.append(1)

    view = views.CreatureView(7, kw.pop("clone_id", None), kw.pop("detail", detail()), kw.pop("pv", ready_preview()), back=back)
    view.back_calls = back_calls
    return view


def allow(monkeypatch, rec, allowed=True):
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=allowed, reason=None if allowed else "off")))


def sent_to(inter):
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    return sent


def edits_to(inter):
    edits = []

    async def edit(*args, **kwargs):
        edits.append(kwargs)

    inter.edit_original_response = edit
    return edits


def test_creature_view_layout_fits_discord_and_evolve_only_when_ready():
    view = make_view(None)
    assert len(view.children) == 7 and {c.row for c in view.children} == {0, 1}
    assert len(view.to_components()) == 2
    evolve_btn = next(c for c in view.children if c.label == LOCALE["catch.creature.btn_evolve"])
    assert not evolve_btn.disabled
    for status in ("final_form", "level_too_low", "needs_item"):
        locked_view = make_view(None, pv=evo_pv(status))
        assert next(c for c in locked_view.children if c.label == LOCALE["catch.creature.btn_evolve"]).disabled
    assert next(c for c in make_view(None, pv=None).children if c.label == LOCALE["catch.creature.btn_evolve"]).disabled


def test_creature_view_buttons_show_current_state():
    view = make_view(None, detail=detail(favorite=True, locked=True, is_buddy=True))
    labels = {c.label for c in view.children}
    assert {LOCALE["catch.creature.btn_unfavorite"], LOCALE["catch.creature.btn_unlock"], LOCALE["catch.creature.btn_unbuddy"]} <= labels


def test_creature_view_rejects_other_users():
    sent = []

    async def send_message(*a, **k):
        sent.append(k)

    other = SimpleNamespace(user=SimpleNamespace(id=999), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(make_view(None).interaction_check(other)) is False and sent[0]["ephemeral"] is True
    mine = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(make_view(None).interaction_check(mine)) is True
    assert asyncio.run(views.EvolveConfirmView(7, None, make_view(None)).interaction_check(other)) is False


@pytest.mark.parametrize("button,writer,result", [
    ("_fav", "set_favorite", True),
    ("_lock", "toggle_lock", creature.ChangeResult(True, value=True)),
    ("_buddy", "toggle_buddy", creature.ChangeResult(True, value=True)),
])
def test_actions_respond_first_check_the_gate_then_write_and_redraw(monkeypatch, button, writer, result):
    rec = Recorder()
    allow(monkeypatch, rec)
    calls = []

    async def write(*args, **kwargs):
        rec.add("write")
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(views, writer, write)
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail()))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", ready_preview()))
    view = make_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(getattr(view, button)(inter))
    assert_response_first(rec)
    assert rec.calls.index("gate") < rec.calls.index("write") < rec.calls.index("load")
    assert calls[0][0][:3] == (11, 7, None) and edits and edits[0]["view"] is view


def test_actions_refuse_without_writing_when_the_game_is_off(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec, allowed=False)
    wrote = []

    async def write(*a, **k):
        wrote.append(1)

    for name in ("set_favorite", "toggle_lock", "toggle_buddy", "evolve", "preview"):
        monkeypatch.setattr(views, name, write)
    view = make_view(rec)
    for button in ("_fav", "_lock", "_buddy", "_evolve"):
        inter = make_interaction(rec)
        sent = sent_to(inter)
        asyncio.run(getattr(view, button)(inter))
        assert len(sent) == 1 and body(sent[0]) == catch_text("catch.unavailable", reason="off"), button
    assert not wrote


def test_missing_creature_sends_a_message_and_returns_to_the_list(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def gone(*a, **k):
        return creature.ChangeResult(False, "not_found")

    monkeypatch.setattr(views, "toggle_lock", gone)
    view = make_view(rec)
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(view._lock(inter))
    assert body(sent[0]) == LOCALE["catch.creature.not_found"] and view.back_calls == [1]


def test_no_player_buddy_message_does_not_leave_the_screen(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def no_player(*a, **k):
        return creature.ChangeResult(False, "no_player")

    monkeypatch.setattr(views, "toggle_buddy", no_player)
    view = make_view(rec)
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(view._buddy(inter))
    assert body(sent[0]) == LOCALE["catch.creature.buddy_no_player"] and view.back_calls == []


def test_a_second_tap_while_working_is_ignored(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    writes = []

    async def slow_write(*a, **k):
        writes.append(1)
        await asyncio.sleep(0.05)
        return creature.ChangeResult(True, value=True)

    monkeypatch.setattr(views, "toggle_lock", slow_write)
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail()))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", ready_preview()))
    view = make_view(rec)

    async def both():
        a, b = make_interaction(rec), make_interaction(rec)
        edits_to(a), edits_to(b)
        sent_b = sent_to(b)
        await asyncio.gather(view._lock(a), view._lock(b))
        return sent_b

    sent_b = asyncio.run(both())
    assert writes == [1] and body(sent_b[0]) == LOCALE["catch.creature.busy"]


def test_a_failing_write_shows_a_friendly_error_and_frees_the_screen(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "toggle_lock", boom)
    view = make_view(rec)
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(view._lock(inter))
    assert body(sent[0]) == LOCALE["catch.creature.error"] and view._busy is False


def test_nickname_button_opens_a_modal_without_touching_the_database(monkeypatch):
    rec = Recorder()
    opened = []

    async def send_modal(modal):
        opened.append(modal)

    view = make_view(rec, detail=detail(nickname="Sparky"))
    inter = make_interaction(rec)
    inter.response.send_modal = send_modal
    asyncio.run(view._nickname(inter))
    assert isinstance(opened[0], views.NicknameModal) and opened[0].nickname.default == "Sparky"
    assert opened[0].nickname.required is False and opened[0].nickname.max_length == creature.NICKNAME_MAX


def run_modal(monkeypatch, typed, writer=None):
    rec = Recorder()
    allow(monkeypatch, rec)
    saved = []

    async def real_set(owned_id, user_id, clone_id, raw):
        saved.append((owned_id, user_id, clone_id, raw))
        return await creature_set_nickname_stub(owned_id, user_id, clone_id, raw)

    async def creature_set_nickname_stub(owned_id, user_id, clone_id, raw):
        check = creature.clean_nickname(raw)
        return creature.ChangeResult(True, value=check.value) if check.ok else creature.ChangeResult(False, check.reason)

    monkeypatch.setattr(views, "set_nickname", writer or real_set)
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail(nickname="Sparky")))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", ready_preview()))
    view = make_view(rec)
    modal = views.NicknameModal(view)
    modal.nickname._value = typed
    inter = make_interaction(rec)
    sent, edits = sent_to(inter), edits_to(inter)
    asyncio.run(modal.on_submit(inter))
    assert_response_first(rec)
    return sent, edits, saved


def test_nickname_modal_saves_a_clean_nickname_and_redraws(monkeypatch):
    sent, edits, saved = run_modal(monkeypatch, "  Sparky  ")
    assert saved == [(11, 7, None, "  Sparky  ")] and edits and "Sparky" in edits[0]["content"] and not sent


def test_nickname_modal_blank_clears(monkeypatch):
    _, edits, _ = run_modal(monkeypatch, "")
    assert edits[0]["content"] == LOCALE["catch.creature.nick_cleared"]


def test_nickname_modal_refuses_bad_names_with_a_reason_and_no_redraw(monkeypatch):
    sent, edits, _ = run_modal(monkeypatch, "see http-x")
    assert body(sent[0]) == LOCALE["catch.creature.nick_link"] and not edits


# ---------------------------------------------------------------- evolve flow

def test_evolve_button_shows_a_confirm_screen_and_spends_nothing(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    evolved = []

    async def never(*a, **k):
        evolved.append(1)

    monkeypatch.setattr(views, "evolve", never)
    monkeypatch.setattr(views, "preview", slow(rec, "preview", ready_preview()))
    view = make_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(view._evolve(inter))
    assert_response_first(rec)
    assert isinstance(edits[0]["view"], views.EvolveConfirmView) and not evolved
    assert "Pyrrock" in edits[0]["embed"].description


def test_evolve_button_redraws_with_a_reason_when_no_longer_ready(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    monkeypatch.setattr(views, "preview", slow(rec, "preview", evo_pv("level_too_low", needed_level=16)))
    view = make_view(rec)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(view._evolve(inter))
    assert edits[0]["content"] == LOCALE["catch.evolve.refused_level_too_low"] and edits[0]["view"] is view


def confirm_flow(monkeypatch, result):
    rec = Recorder()
    allow(monkeypatch, rec)
    calls = []

    async def fake_evolve(owned_id, user_id, clone_id, *, guild_id=None):
        rec.add("evolve")
        calls.append((owned_id, user_id, clone_id, guild_id))
        return result

    monkeypatch.setattr(views, "evolve", fake_evolve)
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail(species_id=2, name="Pyrrock")))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", evo_pv("final_form")))
    creature_view = make_view(rec)
    confirm = views.EvolveConfirmView(7, None, creature_view)
    inter = make_interaction(rec)
    edits, sent = edits_to(inter), sent_to(inter)
    asyncio.run(confirm._confirm(inter))
    assert_response_first(rec)
    return rec, calls, edits, sent


def test_confirm_evolves_once_and_redraws_the_new_creature(monkeypatch):
    rec, calls, edits, _ = confirm_flow(monkeypatch, evo.EvolveResult(True, from_name="Cindrop", to_name="Pyrrock", to_species_id=2, new_dex_entry=True))
    assert calls == [(11, 7, None, 1)] and rec.calls.index("gate") < rec.calls.index("evolve")
    assert "Pyrrock" in edits[0]["content"] and "Dex" in edits[0]["content"]


@pytest.mark.parametrize("reason", ["level_too_low", "needs_item", "no_item", "changed", "final_form"])
def test_confirm_shows_the_refusal_text(monkeypatch, reason):
    _, _, edits, _ = confirm_flow(monkeypatch, evo.EvolveResult(False, reason))
    assert edits[0]["content"] == LOCALE[f"catch.evolve.refused_{reason}"]


def test_confirm_error_says_nothing_was_spent(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "evolve", boom)
    confirm = views.EvolveConfirmView(7, None, make_view(rec))
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(confirm._confirm(inter))
    assert "Nothing was spent" in body(sent[0]) and confirm._busy is False


def test_double_confirm_evolves_only_once(monkeypatch):
    rec = Recorder()
    allow(monkeypatch, rec)
    calls = []

    async def slow_evolve(*a, **k):
        calls.append(1)
        await asyncio.sleep(0.05)
        return evo.EvolveResult(True, from_name="Cindrop", to_name="Pyrrock", to_species_id=2)

    monkeypatch.setattr(views, "evolve", slow_evolve)
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", detail()))
    monkeypatch.setattr(views, "preview", slow(rec, "preview", evo_pv("final_form")))
    confirm = views.EvolveConfirmView(7, None, make_view(rec))

    async def both():
        a, b = make_interaction(rec), make_interaction(rec)
        edits_to(a), edits_to(b)
        sent_to(a), sent_to(b)
        await asyncio.gather(confirm._confirm(a), confirm._confirm(b))

    asyncio.run(both())
    assert calls == [1]


# ---------------------------------------------------------------- open from the collection

def row(i=1, **over):
    base = dict(id=i, species_id=1, name="Cindrop", rarity="common", level=5, nickname=None, shiny=False,
                special=False, favorite=False, locked=False)
    base.update(over)
    return cc.OwnedRow(*[base[k] for k in ("id", "species_id", "name", "rarity", "level", "nickname", "shiny", "special", "favorite", "locked")])


def test_collection_browse_view_keeps_the_old_controls_and_adds_the_new_ones():
    rows = [row(i) for i in range(1, 11)]
    base = collection_views.CollectionView(7, None, rows=rows, total=25)
    view = collection_views.CollectionBrowseView(7, None, rows=rows, total=25)
    assert [type(c) for c in view.children[:4]] == [type(c) for c in base.children]
    assert [(c.row, getattr(c, "label", None)) for c in view.children[:4]] == [(c.row, getattr(c, "label", None)) for c in base.children]
    extra = view.children[4:]
    assert len(extra) == 2 and len(extra[1].options) == 10
    assert {c.row for c in view.children} == {0, 1, 2, 3} and len(view.to_components()) == 4
    assert collection_views.CollectionBrowseView(7, None, rows=[], total=0).children[-1].label == LOCALE["catch.profile.button"]
    assert len(collection_views.CollectionBrowseView(7, None, rows=[], total=0).children) == 4  # prev, next, sort, profile


def test_open_collection_now_builds_the_browse_view(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(collection_views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(collection_views, "list_owned", slow(rec, "db", ([row()], 1, 0)))
    inter = make_interaction(rec)
    sent = sent_to(inter)
    asyncio.run(collection_views.open_collection(inter))
    assert isinstance(sent[0][1]["view"], collection_views.CollectionBrowseView)


def test_opening_a_creature_from_the_list_passes_the_selected_id_and_a_back_path(monkeypatch):
    rec = Recorder()
    seen = []

    async def fake_open(interaction, user_id, clone_id, owned_id, *, back):
        seen.append((user_id, clone_id, owned_id, back))

    monkeypatch.setattr(collection_views, "open_creature_detail", fake_open)
    view = collection_views.CollectionBrowseView(7, 3, rows=[row(5)], total=1)
    inter = make_interaction(rec)
    inter.data = {"values": ["5"]}
    asyncio.run(view._open_creature(inter))
    assert_response_first(rec)
    assert seen == [(7, 3, 5, view._back_to_list)]
    bad = make_interaction(rec)
    bad.data = {"values": ["x"]}
    sent = sent_to(bad)
    asyncio.run(view._open_creature(bad))
    assert body(sent[0]) == LOCALE["catch.collection.not_found"] and len(seen) == 1


def test_back_to_list_redraws_the_list_and_clears_notices(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(collection_views, "list_owned", slow(rec, "db", ([row(5)], 1, 0)))
    view = collection_views.CollectionBrowseView(7, None, rows=[row(5)], total=1)
    inter = make_interaction(rec)
    edits = edits_to(inter)
    asyncio.run(view._back_to_list(inter))
    assert edits[0]["content"] is None and edits[0]["view"] is view and isinstance(edits[0]["embed"], discord.Embed)


def test_open_creature_detail_checks_the_gate_first_and_handles_missing(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="off")))
    monkeypatch.setattr(views, "load_creature", slow(rec, "load", None))
    inter = make_interaction(rec)
    sent = sent_to(inter)
    back = []

    async def go_back(i):
        back.append(1)

    asyncio.run(views.open_creature_detail(inter, 7, None, 5, back=go_back))
    assert "load" not in rec.calls and sent
    rec2 = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(rec2, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "load_creature", slow(rec2, "load", None))
    inter2 = make_interaction(rec2)
    sent2 = sent_to(inter2)
    asyncio.run(views.open_creature_detail(inter2, 7, None, 5, back=go_back))
    assert body(sent2[0]) == LOCALE["catch.creature.not_found"] and back == [1]


# ---------------------------------------------------------------- profile

def profile(**over):
    base = dict(
        total_catches=1234, catch_streak=3, best_streak=9, daily_streak=4, owned=40, shinies=2, specials=1,
        dex_caught=20, dex_seen=30, dex_total=48,
        buddy=ProfileCreature(5, "Cindrop", "common", 12, "*Sparky*", True, False),
        rarest=ProfileCreature(9, "Blazemane", "rare", 30, None, False, False),
    )
    base.update(over)
    return TrainerProfile(**base)


def test_profile_embed_shows_everything_and_fits_limits():
    embed = profile_views.profile_embed(profile(), "Ada *Lovelace*")
    text_all = " ".join(f.value for f in embed.fields)
    assert "1,234" in text_all and "41%" in text_all and "20/48" in text_all and "\\*Sparky\\*" in text_all
    assert "Blazemane" in text_all and "Ada \\*Lovelace\\*" in embed.title
    assert len(embed) <= 6000 and not embed.title.startswith("catch.")
    assert all(not f.value.startswith("catch.") for f in embed.fields)


def test_profile_embed_for_a_brand_new_player_has_friendly_empty_states():
    embed = profile_views.profile_embed(TrainerProfile(), "Ada")
    values = [f.value for f in embed.fields]
    assert LOCALE["catch.profile.no_buddy"] in values and LOCALE["catch.profile.no_rarest"] in values
    assert TrainerProfile().dex_percent == 0


def test_open_profile_gates_then_loads_then_draws(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(profile_views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(profile_views, "load_profile", slow(rec, "load", profile()))
    inter = make_interaction(rec)
    inter.user = SimpleNamespace(id=7, display_name="Ada")
    edits = edits_to(inter)

    async def back(i):
        pass

    asyncio.run(profile_views.open_profile(inter, 7, None, back=back))
    assert rec.calls == ["gate", "load"] and isinstance(edits[0]["view"], profile_views.ProfileView)
    assert "Ada" in edits[0]["embed"].title


def test_open_profile_refuses_when_off_and_survives_a_failing_load(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(profile_views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=False, reason="off")))
    monkeypatch.setattr(profile_views, "load_profile", slow(rec, "load", profile()))
    inter = make_interaction(rec)
    sent = sent_to(inter)

    async def back(i):
        pass

    asyncio.run(profile_views.open_profile(inter, 7, None, back=back))
    assert "load" not in rec.calls and sent

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(profile_views, "check_player_allowed", slow(rec, "gate2", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(profile_views, "load_profile", boom)
    inter2 = make_interaction(rec)
    sent2 = sent_to(inter2)
    asyncio.run(profile_views.open_profile(inter2, 7, None, back=back))
    assert body(sent2[0]) == LOCALE["catch.profile.error"]


def test_profile_view_is_owner_only_and_back_defers_first():
    rec = Recorder()
    went = []

    async def back(i):
        went.append(1)

    view = profile_views.ProfileView(7, back=back)
    asyncio.run(view._back(make_interaction(rec)))
    assert_response_first(rec)
    assert went == [1]
    sent = []

    async def send_message(*a, **k):
        sent.append(k)

    other = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=send_message))
    assert asyncio.run(view.interaction_check(other)) is False and sent[0]["ephemeral"] is True


# ---------------------------------------------------------------- guide follows the emoji table

def test_guide_uses_the_shared_emoji_and_describes_the_new_screens():
    import discord_bot.cogs._views_catch_guide as guide

    fields = {f.name: f.value for f in guide.guide_embed().fields}
    rarity = fields[LOCALE["catch.guide.rarity.name"]]
    for tier in catch_game.RARITY_BY_KEY:
        assert emoji.RARITY[tier] in rarity
    assert emoji.FLAG["shiny"] in rarity and "{" not in rarity
    collection = fields[LOCALE["catch.guide.collection.name"]]
    for word in ("nickname", "buddy", "evolve", "Trainer card"):
        assert word in collection
