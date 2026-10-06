"""Fled card: renderer and the edit that replaces the wild card when a spawn expires."""
import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from discord_bot.cogs import catch
from modules import catch_fled_card as fc
from modules.catch_card import encounter_card_png
from modules.catch_species import all_species


def _species(sid):
    return all_species()[sid]


def _fled(sid=2):
    return asyncio.run(fc.fled_card_png(_species(sid)))


def test_card_is_a_png_of_the_wild_card_size():
    img = Image.open(io.BytesIO(_fled()))
    assert img.format == "PNG" and img.size == (720, 400)


def test_card_changes_with_species_and_second_type():
    assert _fled(1) != _fled(2)
    one_type = dict(_species(2), element2=None)
    assert asyncio.run(fc.fled_card_png(one_type)) != _fled(2)


def test_card_is_not_the_wild_card():
    wild = asyncio.run(encounter_card_png("wild", _species(2), level=12))
    assert _fled(2) != wild


def test_bad_species_raises_so_callers_fall_back():
    with pytest.raises(Exception):
        asyncio.run(fc.fled_card_png({"id": 1}))


def test_each_send_gets_a_fresh_file_and_cache_clears():
    data = _fled()
    a, b = fc.fled_file(data), fc.fled_file(data)
    assert a is not b and a.filename == fc.FLED_FILE
    assert fc._cached.cache_info().currsize >= 1
    fc.clear_fled_cache()
    assert fc._cached.cache_info().currsize == 0


# --- the expiry edit ----------------------------------------------------------------

def _expire(monkeypatch=None, *, row_extra=None, edit_errors=0, card=None):
    """Run _mark_spawn_expired; returns the list of edit kwargs. ``edit_errors`` first edits raise."""
    edits, state = [], {"left": edit_errors}

    async def edit(**kwargs):
        edits.append(kwargs)
        if state["left"] > 0:
            state["left"] -= 1
            raise discord.DiscordException("edit failed")

    async def fetch_message(_id):
        return SimpleNamespace(edit=edit)

    channel = SimpleNamespace(fetch_message=fetch_message)
    fake_self = SimpleNamespace(bot=SimpleNamespace(get_channel=lambda _id: channel))
    row = {"channel_id": 5, "message_id": 6, "id": 7, "created_at": datetime.now(timezone.utc)}
    row.update(row_extra or {})
    asyncio.run(catch.CatchCog._mark_spawn_expired(fake_self, row))
    return edits


def test_expired_spawn_with_a_species_gets_the_fled_card():
    edits = _expire(row_extra={"species_id": 2})
    assert len(edits) == 1
    kw = edits[0]
    assert len(kw["attachments"]) == 1 and isinstance(kw["attachments"][0], discord.File)
    assert kw["attachments"][0].filename == fc.FLED_FILE
    assert kw["embed"].image.url == f"attachment://{fc.FLED_FILE}"
    assert kw["embed"].title == catch.text("claim.fled.title")  # the plain text is unchanged
    assert all(item.disabled for item in kw["view"].children)


@pytest.mark.parametrize("extra", [{}, {"species_id": None}, {"species_id": 99999}])
def test_expired_spawn_without_a_usable_species_stays_plain_and_clears_the_old_card(extra):
    edits = _expire(row_extra=extra)
    assert len(edits) == 1 and edits[0]["attachments"] == [] and edits[0]["embed"].image.url is None


def test_render_failure_falls_back_to_the_plain_embed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("no font")

    monkeypatch.setattr(catch, "fled_card_png", boom)
    edits = _expire(row_extra={"species_id": 2})
    assert len(edits) == 1 and edits[0]["attachments"] == [] and edits[0]["embed"].image.url is None


def test_failed_card_edit_is_retried_once_as_plain():
    edits = _expire(row_extra={"species_id": 2}, edit_errors=1)
    assert len(edits) == 2
    assert len(edits[0]["attachments"]) == 1
    assert edits[1]["attachments"] == [] and edits[1]["embed"].image.url is None
    assert all(item.disabled for item in edits[1]["view"].children)


def test_edit_that_always_fails_never_raises():
    edits = _expire(row_extra={"species_id": 2}, edit_errors=5)
    assert len(edits) == 2  # card attempt, one plain retry, then give up quietly


def test_row_without_message_data_edits_nothing():
    assert _expire(row_extra={"message_id": None, "species_id": 2}) == []
