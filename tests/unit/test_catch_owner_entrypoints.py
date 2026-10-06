"""P1-08 owner entry points: the join-DM "Creature catching" toggle and the
Server Owners Panel entry."""
import asyncio
from types import SimpleNamespace

import discord

from discord_bot.cogs import _views_join_dm as join_dm
from discord_bot.cogs._views_server_panel_p6 import FeaturesView
from modules.catch_setup import CatchSetup


def _all_buttons(view):
    found = []

    def walk(item):
        if isinstance(item, discord.ui.Button):
            found.append(item)
        for child in getattr(item, "children", []) or []:
            walk(child)
    walk(view)
    return found


def test_join_dm_offers_creature_catching_before_build_bot():
    keys = list(join_dm.FEATURE_TOGGLES)
    assert "catch" in keys
    assert keys.index("catch") < keys.index("build_bot")
    label, emoji, handler, options_view, blurb = join_dm.FEATURE_TOGGLES["catch"]
    assert label == "Creature catching" and handler is join_dm._enable_catch and blurb


def test_server_panel_features_hub_has_creature_catching_button():
    view = FeaturesView(1, None, 7, {"bump": {}, "role": {}, "auto": {}, "catch": CatchSetup()})
    labels = [b.label for b in _all_buttons(view)]
    assert "Creature catching" in labels


def test_server_panel_features_hub_survives_catch_lookup_failure():
    view = FeaturesView(1, None, 7, {"bump": {}, "role": {}, "auto": {}, "catch": None})
    assert any(b.label == "Creature catching" for b in _all_buttons(view))


class _Channel:
    def __init__(self, cid, name):
        self.id, self.name, self.mention = cid, name, f"<#{cid}>"


def _guild(*, can_manage_channels, text_channels=()):
    created = []
    texts = list(text_channels)

    async def create_text_channel(name, reason=None):
        channel = _Channel(900, name)
        created.append(channel)
        return channel

    guild = SimpleNamespace(
        id=5, text_channels=texts, created=created,
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=can_manage_channels)),
        get_channel=lambda cid: next((c for c in texts if c.id == cid), None),
        create_text_channel=create_text_channel,
    )
    return guild


def _run_enable(monkeypatch, guild, clone_id=3, saved=None):
    saved = {} if saved is None else saved

    async def fake_load(guild_id, cid=None, **kw):
        return CatchSetup()

    async def fake_save(guild_id, setup, cid=None, **kw):
        saved["setup"], saved["save_clone"] = setup, cid

    async def fake_flag(guild_id, cid, feature, enabled, **kw):
        saved["flag"] = (feature, enabled, cid)

    import modules.catch_gate as gate
    import modules.catch_setup as cs
    monkeypatch.setattr(cs, "load_setup", fake_load)
    monkeypatch.setattr(cs, "save_setup", fake_save)
    monkeypatch.setattr(gate, "set_feature_flag", fake_flag)
    interaction = SimpleNamespace(user=SimpleNamespace(id=7))
    return asyncio.run(join_dm._enable_catch(interaction, guild, clone_id)), saved


def test_enable_catch_creates_wild_zone_and_turns_game_on(monkeypatch):
    guild = _guild(can_manage_channels=True)
    (ok, message), saved = _run_enable(monkeypatch, guild)
    assert ok and "<#900>" in message
    assert [c.name for c in guild.created] == ["wild-zone"]
    assert saved["setup"].enabled and saved["setup"].spawn_channel_ids == (900,)
    assert saved["save_clone"] == 3 and saved["flag"] == ("game", True, 3)


def test_enable_catch_reuses_existing_wild_zone(monkeypatch):
    guild = _guild(can_manage_channels=False, text_channels=[_Channel(42, "wild-zone")])
    (ok, _), saved = _run_enable(monkeypatch, guild)
    assert ok and guild.created == []
    assert saved["setup"].spawn_channel_ids == (42,)


def test_enable_catch_without_manage_channels_changes_nothing(monkeypatch):
    guild = _guild(can_manage_channels=False, text_channels=[_Channel(1, "general")])
    (ok, message), saved = _run_enable(monkeypatch, guild)
    assert not ok and "Manage Channels" in message
    assert saved == {}, "must not enable the game or fall back to #general"
