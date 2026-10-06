"""Trainer card and wild/caught cards: rendering and how each screen uses them."""

import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_profile as profile_views
import discord_bot.cogs.catch as catch
from modules import catch_card, catch_profile
from modules.catch_profile import ProfileCreature, TrainerProfile
from modules.catch_spawn import SpawnRoll
from modules.catch_species import all_species
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow
from tests.unit.test_catch_phase3 import edits_to, profile

PNG = b"\x89PNG\r\n\x1a\n"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- trainer card

def full_profile(**over):
    base = dict(buddy=ProfileCreature(5, "Tidecaller", "rare", 34, "x", True, False, "tide", "lumen"))
    base.update(over)
    return profile(**base)


def test_trainer_card_is_a_png_of_the_card_size_for_every_kind_of_player():
    for p in (full_profile(), profile(), TrainerProfile(), full_profile(buddy=None),
              full_profile(total_catches=10**9, dex_total=0), full_profile(dex_caught=48, dex_seen=48, dex_total=48)):
        data = run(catch_card.trainer_card_png(p))
        assert data.startswith(PNG) and Image.open(io.BytesIO(data)).size == (catch_card.W, catch_card.H)


def test_trainer_card_changes_with_the_numbers_and_the_hero_creature():
    base = run(catch_card.trainer_card_png(full_profile()))
    assert run(catch_card.trainer_card_png(full_profile(total_catches=1))) != base
    assert run(catch_card.trainer_card_png(full_profile(dex_caught=1))) != base
    assert run(catch_card.trainer_card_png(full_profile(buddy=None))) != base


def test_trainer_card_uses_the_buddy_else_the_rarest_creature():
    with_buddy = catch_card._trainer_key(full_profile())
    assert with_buddy[0][1] == "Tidecaller" and with_buddy[1] == "BUDDY"
    rarest = catch_card._trainer_key(full_profile(buddy=None, rarest=ProfileCreature(9, "Cragtitan", "epic", 50, None, False, False)))
    assert rarest[0][1] == "Cragtitan" and rarest[1] == "RAREST" and rarest[0][3] == "stone"  # no element known: neutral
    assert catch_card._trainer_key(TrainerProfile())[0] is None


def test_profile_creature_now_carries_its_species_elements():
    rec = {"id": 3, "species_id": 6, "level": 9, "nickname": None, "shiny": False, "special": False}
    c = catch_profile._creature(rec)
    assert (c.name, c.element, c.element2) == ("Tidecaller", "tide", "lumen")
    old = ProfileCreature(1, "Cindrop", "common", 5, None, False, False)  # 7-argument form still valid
    assert old.element == "" and old.element2 is None


def test_profile_message_attaches_the_card_and_keeps_every_embed_field():
    msg = run(profile_views.profile_message(full_profile(), "Ada"))
    plain = profile_views.profile_embed(full_profile(), "Ada")
    assert len(msg["attachments"]) == 1 and msg["attachments"][0].filename == profile_views.CARD_FILE
    assert msg["embed"].image.url == f"attachment://{profile_views.CARD_FILE}"
    assert [f.name for f in msg["embed"].fields] == [f.name for f in plain.fields]


def test_profile_message_falls_back_to_the_text_embed(monkeypatch):
    async def boom(_p):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(profile_views, "trainer_card_png", boom)
    msg = run(profile_views.profile_message(full_profile(), "Ada"))
    assert msg["attachments"] == [] and msg["embed"].image.url is None


def test_open_profile_shows_the_card(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(profile_views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(profile_views, "load_profile", slow(rec, "load", full_profile()))
    inter = make_interaction(rec)
    inter.user = SimpleNamespace(id=7, display_name="Ada")
    edits = edits_to(inter)
    run(profile_views.open_profile(inter, 7, None, back=lambda i: None))
    assert len(edits) == 1 and len(edits[0]["attachments"]) == 1 and rec.calls.index("gate") < rec.calls.index("load")


# ---------------------------------------------------------------- wild / caught card

def species(i):
    return all_species()[i]


@pytest.mark.parametrize("kind", ["wild", "caught"])
def test_encounter_card_renders_for_every_species_and_flag_combination(kind):
    every_fourth = list(all_species().values())[::4]  # covers every element; keeps the test quick
    assert len({sp["element"] for sp in every_fourth}) >= 5
    for sp in every_fourth:
        assert run(catch_card.encounter_card_png(kind, sp, level=10)).startswith(PNG)
    for shiny in (False, True):
        for new in (False, True):
            data = run(catch_card.encounter_card_png(kind, species(2), level=10, shiny=shiny, new_species=new))
            assert Image.open(io.BytesIO(data)).size == (catch_card.W, catch_card.H)


def test_encounter_card_changes_with_kind_flags_and_level():
    base = run(catch_card.encounter_card_png("wild", species(2), level=10))
    assert run(catch_card.encounter_card_png("caught", species(2), level=10)) != base
    assert run(catch_card.encounter_card_png("wild", species(2), level=11)) != base
    assert run(catch_card.encounter_card_png("wild", species(2), level=10, shiny=True)) != base
    assert run(catch_card.encounter_card_png("caught", species(2), level=10, new_species=True)) != \
        run(catch_card.encounter_card_png("caught", species(2), level=10))


def test_card_parts_returns_a_file_and_points_the_embed_at_it():
    embed = discord.Embed(title="t")
    parts = run(catch.card_parts(embed, "wild", 2, level=12))
    assert set(parts) == {"file"} and parts["file"].filename == catch.SPAWN_CARD_FILE
    assert embed.image.url == f"attachment://{catch.SPAWN_CARD_FILE}"


def test_card_parts_leaves_the_embed_alone_when_it_cannot_draw(monkeypatch):
    embed = discord.Embed(title="t")
    assert run(catch.card_parts(embed, "wild", 999999, level=1)) == {} and embed.image.url is None
    assert run(catch.card_parts(embed, "wild", "nope", level=1)) == {} and embed.image.url is None

    async def boom(*_a, **_k):
        raise RuntimeError("no pillow")

    monkeypatch.setattr(catch, "encounter_card_png", boom)
    assert run(catch.card_parts(embed, "wild", 2, level=1)) == {} and embed.image.url is None


def test_published_spawn_carries_the_card(monkeypatch):
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(id=77)

    async def noop(*_a, **_k):
        return 5

    roll = SpawnRoll(2, "Pyrrock", "pyrrock", "uncommon", 12, False, (1, 2, 3, 4, 5), "pyrrock")
    monkeypatch.setattr(catch, "roll_spawn", lambda *a, **k: roll)
    monkeypatch.setattr(catch, "create_spawn", noop)
    monkeypatch.setattr(catch, "attach_spawn_message", noop)
    setup = SimpleNamespace(despawn_seconds=300, rare_ping_role_id=None)
    run(catch.publish_spawn(SimpleNamespace(id=9, send=send), guild_id=1, clone_id=None, setup=setup))
    assert len(sent[0]["file"].filename) and sent[0]["embed"].image.url


def test_published_spawn_without_a_card_is_the_old_plain_message(monkeypatch):
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(id=77)

    async def noop(*_a, **_k):
        return 5

    async def nothing(*_a, **_k):
        return {}

    roll = SpawnRoll(2, "Pyrrock", "pyrrock", "uncommon", 12, False, (1, 2, 3, 4, 5), "pyrrock")
    monkeypatch.setattr(catch, "roll_spawn", lambda *a, **k: roll)
    monkeypatch.setattr(catch, "create_spawn", noop)
    monkeypatch.setattr(catch, "attach_spawn_message", noop)
    monkeypatch.setattr(catch, "card_parts", nothing)
    setup = SimpleNamespace(despawn_seconds=300, rare_ping_role_id=None)
    run(catch.publish_spawn(SimpleNamespace(id=9, send=send), guild_id=1, clone_id=None, setup=setup))
    assert "file" not in sent[0] and sent[0]["embed"].image.url is None


def claim_inter(rec, sent):
    inter = make_interaction(rec)
    inter.message = SimpleNamespace(edit=slow(rec, "edit"))
    inter.guild_id = 1

    async def followup_send(*args, **kwargs):
        sent.append(kwargs)

    inter.followup = SimpleNamespace(send=followup_send)
    return inter


def run_claim(monkeypatch, result):
    rec, sent = Recorder(), []
    monkeypatch.setattr(catch, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(catch, "ensure_starter_kit", slow(rec, "starter", True))
    monkeypatch.setattr(catch, "record_catch", slow(rec, "record", result))
    run(catch.SpawnClaimView(1)._claim(claim_inter(rec, sent)))
    return sent


def test_successful_claim_shows_the_caught_card(monkeypatch):
    sent = run_claim(monkeypatch, SimpleNamespace(
        claimed=True, species_id=2, level=12, shiny=True, new_species=True, replay=True, owned_id=None))
    assert len(sent) == 1 and sent[0]["file"].filename == catch.SPAWN_CARD_FILE and sent[0]["embed"].image.url
    assert "Pyrrock" in sent[0]["embed"].description  # the text part is unchanged


def test_failed_claim_has_no_card(monkeypatch):
    sent = run_claim(monkeypatch, SimpleNamespace(claimed=False))
    assert len(sent) == 1 and "file" not in sent[0]


def test_expired_spawn_clears_its_card():
    edits = []

    async def edit(**kwargs):
        edits.append(kwargs)

    channel = SimpleNamespace(fetch_message=lambda _id: _msg(edit))

    async def _msg(edit_fn):
        return SimpleNamespace(edit=edit_fn)

    fake_self = SimpleNamespace(bot=SimpleNamespace(get_channel=lambda _id: channel))
    row = {"channel_id": 5, "message_id": 6, "id": 7, "created_at": datetime.now(timezone.utc)}
    run(catch.CatchCog._mark_spawn_expired(fake_self, row))
    assert len(edits) == 1 and edits[0]["attachments"] == []
