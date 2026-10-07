"""Guide: static screen, limits, timing, gate, and hub routing."""
import asyncio
import re
from types import SimpleNamespace

import discord

import discord_bot.cogs._views_catch_guide as views
import discord_bot.cogs.catch as catch
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.notice_helpers import body


def sent_to(inter):
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    return sent


def test_every_guide_key_exists_and_embed_fits_discord_limits():
    embed = views.guide_embed()
    assert len(embed) <= 6000 and len(embed.description) <= 4096 and len(embed.fields) == len(views.GUIDE_SECTIONS)
    for field in embed.fields:
        assert field.name and len(field.name) <= 256 and field.value and len(field.value) <= 1024
        assert not field.name.startswith("catch.") and not field.value.startswith("catch.")
    assert not embed.title.startswith("catch.") and not embed.description.startswith("catch.")


def test_guide_only_mentions_features_that_exist():
    body = " ".join(f.value for f in views.guide_embed().fields)
    for built in ("Wild zone", "Encounter", "Daily", "Shop", "Inventory", "Sell", "Wallet", "Collection", "Dex", "Status"):
        assert built in body
    for placeholder in ("Trade", "Gift", "Battle", "Leaderboard", "Team", "Moves"):
        assert placeholder not in body


def test_open_guide_responds_before_gate_and_is_ephemeral(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))
    inter = make_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_guide(inter))
    assert_response_first(r)
    assert sent[0][1]["ephemeral"] is True and isinstance(sent[0][1]["embed"], discord.Embed)


def test_open_guide_refuses_when_game_is_off(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    inter = make_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_guide(inter))
    assert body(sent[0]) == catch.text("catch.unavailable", reason="game_disabled")


def test_guide_button_opens_the_real_screen_live_and_after_restart(monkeypatch):
    assert catch.REAL_ACTIONS["guide"] is views.open_guide
    buttons = {b.label: b for b in catch.CatchHubView(category="info").children if isinstance(b, discord.ui.Button)}
    assert buttons["Guide"].callback is views.open_guide and buttons["Guide"].custom_id == "catch:hub:info:guide"
    seen = []

    async def mark(interaction):
        seen.append("guide")

    monkeypatch.setitem(catch.REAL_ACTIONS, "guide", mark)
    pattern = catch.CatchHubDynamicButton.__discord_ui_compiled_template__.pattern
    match = re.fullmatch(pattern, "catch:hub:info:guide")
    item = asyncio.run(catch.CatchHubDynamicButton.from_custom_id(None, discord.ui.Button(custom_id="catch:hub:info:guide"), match))
    asyncio.run(item.callback(SimpleNamespace()))
    assert seen == ["guide"]
