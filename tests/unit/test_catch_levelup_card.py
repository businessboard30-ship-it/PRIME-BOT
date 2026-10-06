"""Level-up card: renderer and the second message sent after a buddy level-up."""
import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from discord_bot.cogs import catch as cog
from modules import catch_levelup_card as lc
from modules import catch_profile
from modules.catch_profile import ProfileCreature
from modules.catch_species import all_species
from modules.catch_xp import XpResult


def _species(sid):
    return all_species()[sid]


def _png(data):
    return Image.open(io.BytesIO(data))


def test_card_is_a_png_of_the_expected_size():
    data = asyncio.run(lc.levelup_card_png(_species(2), level_before=4, level_after=5, xp=3))
    img = _png(data)
    assert img.format == "PNG" and img.size == (720, lc.LEVELUP_H)


def test_card_changes_with_levels_xp_and_rarity_flags():
    base = dict(level_before=4, level_after=5, xp=3)
    a = asyncio.run(lc.levelup_card_png(_species(2), **base))
    assert a != asyncio.run(lc.levelup_card_png(_species(2), level_before=5, level_after=6, xp=3))
    assert a != asyncio.run(lc.levelup_card_png(_species(2), level_before=4, level_after=5, xp=40))
    assert a != asyncio.run(lc.levelup_card_png(_species(2), shiny=True, **base))
    assert a != asyncio.run(lc.levelup_card_png(_species(1), **base))


def test_evolution_ready_pill_is_drawn_only_when_the_level_is_reached(monkeypatch):
    sp = _species(1)  # Cindrop evolves at level 16
    assert sp["evolves_to"] is not None and sp["evolve_level"] == 16
    assert lc.evolve_ready(sp, 16) and not lc.evolve_ready(sp, 15)
    with_pill = asyncio.run(lc.levelup_card_png(sp, level_before=15, level_after=16))
    lc.clear_levelup_cache()
    monkeypatch.setattr(lc, "evolve_ready", lambda *a: False)
    without_pill = asyncio.run(lc.levelup_card_png(sp, level_before=15, level_after=16))
    assert with_pill != without_pill  # the pill is the only difference


def test_species_without_a_level_evolution_never_shows_the_pill():
    assert not lc.evolve_ready({"evolves_to": None, "evolve_level": None}, 99)
    assert not lc.evolve_ready({"evolves_to": 5, "evolve_level": None}, 99)  # item-based


def test_max_level_and_zero_xp_still_draw():
    from modules.catch_game import LEVEL_MAX
    data = asyncio.run(lc.levelup_card_png(_species(16), level_before=LEVEL_MAX - 1, level_after=LEVEL_MAX, xp=0))
    assert _png(data).size == (720, lc.LEVELUP_H)


def test_bad_species_raises_so_callers_fall_back():
    with pytest.raises(Exception):
        asyncio.run(lc.levelup_card_png({"id": 1}, level_before=1, level_after=2))


def test_each_send_gets_a_fresh_file():
    data = asyncio.run(lc.levelup_card_png(_species(2), level_before=4, level_after=5))
    a, b = lc.levelup_file(data), lc.levelup_file(data)
    assert a is not b and a.filename == lc.LEVELUP_FILE


def test_clear_cache_empties_it():
    asyncio.run(lc.levelup_card_png(_species(2), level_before=4, level_after=5))
    assert lc._cached.cache_info().currsize >= 1
    lc.clear_levelup_cache()
    assert lc._cached.cache_info().currsize == 0


# --- the message --------------------------------------------------------------------

class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=77), client=SimpleNamespace(clone_id=None), guild_id=None, followup=_Followup(),
    )


def _buddy(level=5, species_id=2, **kw):
    return ProfileCreature(9, "Pyrrock", "uncommon", level, "Nick", kw.get("shiny", False),
                           kw.get("special", False), "ember", "stone", species_id)


def _send(monkeypatch, xp, buddy):
    async def fake_profile(user_id, clone_id, **kw):
        if isinstance(buddy, Exception):
            raise buddy
        return SimpleNamespace(buddy=buddy)

    monkeypatch.setattr(cog, "load_profile", fake_profile)
    it = _interaction()
    asyncio.run(cog.SpawnClaimView._send_levelup(object(), it, xp))
    return it.followup.sent


LEVELED = XpResult(True, None, 25, 4, 5, 3)


def test_level_up_sends_one_ephemeral_card_message(monkeypatch):
    sent = _send(monkeypatch, LEVELED, _buddy())
    assert len(sent) == 1
    _, kw = sent[0]
    assert kw["ephemeral"] is True and isinstance(kw["file"], discord.File)
    assert kw["file"].filename == lc.LEVELUP_FILE
    assert kw["embed"].image.url == f"attachment://{lc.LEVELUP_FILE}"
    assert "level 5" in kw["embed"].description


@pytest.mark.parametrize("xp", [
    None,
    XpResult(True, None, 10, 4, 4, 10),       # gained XP, no level
    XpResult(False, "capped"),
])
def test_no_message_without_a_level_up(monkeypatch, xp):
    assert _send(monkeypatch, xp, _buddy()) == []


def test_no_message_for_plain_xp_even_if_the_buddy_level_matches(monkeypatch):
    # leaky input: the buddy level equals level_after, so only the leveled check stops the card
    assert _send(monkeypatch, XpResult(True, None, 10, 4, 4, 10), _buddy(level=4)) == []


@pytest.mark.parametrize("buddy", [
    None,
    _buddy(level=9),           # buddy moved on since: card would show the wrong creature
    _buddy(species_id=99999),  # species no longer in the table
])
def test_no_message_when_the_buddy_does_not_match(monkeypatch, buddy):
    assert _send(monkeypatch, LEVELED, buddy) == []


def test_profile_failure_is_swallowed(monkeypatch):
    assert _send(monkeypatch, LEVELED, RuntimeError("db down")) == []


def test_render_failure_is_swallowed_and_sends_nothing(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no font")

    monkeypatch.setattr(cog, "levelup_card_png", boom)
    assert _send(monkeypatch, LEVELED, _buddy()) == []


def test_send_failure_is_swallowed(monkeypatch):
    async def fake_profile(*a, **k):
        return SimpleNamespace(buddy=_buddy())

    async def bad_send(*a, **k):
        raise RuntimeError("discord down")

    monkeypatch.setattr(cog, "load_profile", fake_profile)
    it = _interaction()
    it.followup.send = bad_send
    asyncio.run(cog.SpawnClaimView._send_levelup(object(), it, LEVELED))  # must not raise


def test_shiny_flag_reaches_the_renderer(monkeypatch):
    seen = {}

    async def spy(species, **kw):
        seen.update(kw)
        return b"x"

    monkeypatch.setattr(cog, "levelup_card_png", spy)
    monkeypatch.setattr(cog, "levelup_file", lambda data: discord.File(io.BytesIO(data), filename="levelup.png"))
    _send(monkeypatch, LEVELED, _buddy(shiny=True))
    assert seen["shiny"] is True and seen["level_before"] == 4 and seen["level_after"] == 5 and seen["xp"] == 3


# --- claim flow ---------------------------------------------------------------------

def test_add_buddy_xp_returns_the_result_only_on_a_real_gain(monkeypatch):
    class _Embed:
        def add_field(self, **kw):
            pass

    async def run(outcome):
        async def fake(*a, **k):
            return outcome

        monkeypatch.setattr(cog, "grant_buddy_catch_xp", fake)
        it = _interaction()
        return await cog.SpawnClaimView._add_buddy_xp(object(), _Embed(), it, SimpleNamespace(owned_id=5, new_species=False))

    assert asyncio.run(run(LEVELED)) is LEVELED
    for none in (None, XpResult(False, "capped"), XpResult(True, None, 0, 4, 4, 0)):
        assert asyncio.run(run(none)) is None


def test_profile_creature_keeps_old_construction_and_fills_species_id():
    old = ProfileCreature(1, "A", "common", 1, None, False, False)
    assert old.species_id == 0
    made = catch_profile._creature({"id": 3, "species_id": 2, "level": 4, "nickname": None, "shiny": False, "special": False})
    assert made.species_id == 2 and made.element == "ember"


def _claim_flow(monkeypatch, xp, buddy):
    from tests.unit.test_catch_card_screens import run_claim

    async def fake_xp(*a, **k):
        return xp

    async def fake_profile(*a, **k):
        return SimpleNamespace(buddy=buddy)

    monkeypatch.setattr(cog, "grant_buddy_catch_xp", fake_xp)
    monkeypatch.setattr(cog, "load_profile", fake_profile)
    return run_claim(monkeypatch, SimpleNamespace(
        claimed=True, species_id=2, level=12, shiny=False, new_species=False, replay=False, owned_id=5))


def test_claim_with_level_up_sends_the_catch_card_first_then_the_level_up_card(monkeypatch):
    sent = _claim_flow(monkeypatch, LEVELED, _buddy())
    assert [m["file"].filename for m in sent] == [cog.SPAWN_CARD_FILE, lc.LEVELUP_FILE]
    assert all(m["ephemeral"] for m in sent)


def test_claim_without_level_up_sends_only_the_catch_card(monkeypatch):
    sent = _claim_flow(monkeypatch, XpResult(True, None, 10, 4, 4, 10), _buddy())
    assert [m["file"].filename for m in sent] == [cog.SPAWN_CARD_FILE]


def test_claim_keeps_the_plain_level_up_text_in_the_catch_embed(monkeypatch):
    sent = _claim_flow(monkeypatch, LEVELED, _buddy())
    assert any("level 5" in f.value for f in sent[0]["embed"].fields)


def test_claim_still_succeeds_when_the_level_up_card_fails(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no font")

    monkeypatch.setattr(cog, "levelup_card_png", boom)
    sent = _claim_flow(monkeypatch, LEVELED, _buddy())
    assert [m["file"].filename for m in sent] == [cog.SPAWN_CARD_FILE]
