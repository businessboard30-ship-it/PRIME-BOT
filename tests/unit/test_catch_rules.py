"""Rules: shows this server's setup, caps long lists, timing, gate, errors, hub routing."""
import asyncio
import re
from types import SimpleNamespace

import discord

import discord_bot.cogs._views_catch_rules as views
import discord_bot.cogs.catch as catch
from modules.catch_setup import CatchSetup
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow
from tests.unit.notice_helpers import body


def values(embed):
    return " | ".join(f"{f.name}: {f.value}" for f in embed.fields)


def sent_to(inter):
    sent = []

    async def followup_send(*args, **kwargs):
        sent.append((args, kwargs))

    inter.followup = SimpleNamespace(send=followup_send)
    return sent


def rules_interaction(rec, guild_id=1):
    inter = make_interaction(rec)
    inter.guild_id = guild_id
    return inter


def test_embed_shows_the_configured_setup():
    setup = CatchSetup(enabled=True, spawn_channel_ids=(11, 12), encounter_channel_ids=(13,), announce_channel_id=14,
                       rare_ping_role_id=15, speed_preset="fast", spawn_every_n_messages=12,
                       min_seconds_between_spawns=45, despawn_seconds=300)
    text_ = values(views.rules_embed(setup))
    assert "<#11> <#12>" in text_ and "<#13>" in text_ and "<#14>" in text_ and "<@&15>" in text_
    assert "Fast" in text_ and "12 messages" in text_ and "45 sec" in text_ and "5 min" in text_
    assert catch.text("rules.on") in text_


def test_default_setup_reads_as_off_with_nothing_set():
    embed = views.rules_embed(CatchSetup())
    text_ = values(embed)
    assert catch.text("rules.off") in text_ and text_.count(catch.text("rules.none")) == 2
    assert "Rare ping" not in text_ and "Announcements" not in text_


def test_long_channel_lists_are_capped_and_fit_discord_limits():
    ids = tuple(range(1, 201))
    embed = views.rules_embed(CatchSetup(enabled=True, spawn_channel_ids=ids, encounter_channel_ids=ids))
    assert len(embed) <= 6000 and all(len(f.value) <= 1024 for f in embed.fields)
    assert embed.fields[3].value.count("<#") == views.MAX_CHANNELS_SHOWN
    assert catch.text("rules.more", count=200 - views.MAX_CHANNELS_SHOWN) in embed.fields[3].value


def test_open_rules_responds_before_gate_and_db_and_is_ephemeral(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(views, "load_setup", slow(r, "load", CatchSetup()))
    inter = rules_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_rules(inter))
    assert_response_first(r)
    assert r.calls.index("gate") < r.calls.index("load")
    assert sent[0][1]["ephemeral"] is True and isinstance(sent[0][1]["embed"], discord.Embed)


def test_open_rules_loads_this_servers_setup_for_this_clone(monkeypatch):
    seen = []

    async def gate(*args, **kwargs):
        return SimpleNamespace(allowed=True, reason=None)

    async def load(guild_id, clone_id):
        seen.append((guild_id, clone_id))
        return CatchSetup()

    monkeypatch.setattr(views, "check_player_allowed", gate)
    monkeypatch.setattr(views, "load_setup", load)
    inter = rules_interaction(Recorder(), guild_id=42)
    inter.client = SimpleNamespace(clone_id=3)
    sent_to(inter)
    asyncio.run(views.open_rules(inter))
    assert seen == [(42, 3)]


def test_open_rules_refuses_when_game_is_off_and_outside_servers(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=False, reason="game_disabled")))
    monkeypatch.setattr(views, "load_setup", slow(r, "load", CatchSetup()))
    inter = rules_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_rules(inter))
    assert body(sent[0]) == catch.text("catch.unavailable", reason="game_disabled") and "load" not in r.calls
    inter.guild_id = None
    asyncio.run(views.open_rules(inter))
    assert body(sent[1]) == catch.text("encounter.server_only")


def test_load_failure_is_reported_politely(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(views, "check_player_allowed", slow(r, "gate", SimpleNamespace(allowed=True, reason=None)))

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "load_setup", boom)
    inter = rules_interaction(r)
    sent = sent_to(inter)
    asyncio.run(views.open_rules(inter))
    assert body(sent[0]) == catch.text("rules.error")


def test_rules_button_opens_the_real_screen_live_and_after_restart(monkeypatch):
    assert catch.REAL_ACTIONS["rules"] is views.open_rules
    buttons = {b.label: b for b in catch.CatchHubView(category="info").children if isinstance(b, discord.ui.Button)}
    assert buttons["Rules"].callback is views.open_rules and buttons["Rules"].custom_id == "catch:hub:info:rules"
    seen = []

    async def mark(interaction):
        seen.append("rules")

    monkeypatch.setitem(catch.REAL_ACTIONS, "rules", mark)
    pattern = catch.CatchHubDynamicButton.__discord_ui_compiled_template__.pattern
    match = re.fullmatch(pattern, "catch:hub:info:rules")
    item = asyncio.run(catch.CatchHubDynamicButton.from_custom_id(None, discord.ui.Button(custom_id="catch:hub:info:rules"), match))
    asyncio.run(item.callback(SimpleNamespace()))
    assert seen == ["rules"]
