"""Setup screen actions: "Create wild-zone" and "Test spawn" must do real work."""
import asyncio
from types import SimpleNamespace

import discord

import discord_bot.cogs.catch as catch
from tests.unit.test_catch_interaction_timing import Recorder, assert_response_first, make_interaction, slow


class _Chan:
    def __init__(self, cid, name="wild-zone"):
        self.id, self.name, self.mention = cid, name, f"<#{cid}>"
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=555)


def _guild(*, channels=(), can_manage_channels=True):
    created = []
    chans = list(channels)

    async def create_text_channel(name, reason=None):
        c = _Chan(900, name)
        created.append(c)
        chans.append(c)
        return c

    return SimpleNamespace(
        id=1, text_channels=chans, created=created, channels=chans, owner_id=7,
        get_channel=lambda cid: next((c for c in chans if c.id == cid), None),
        me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_channels=can_manage_channels)),
        create_text_channel=create_text_channel,
    )


def _setup_env(monkeypatch, rec, *, allowed=True):
    monkeypatch.setattr(catch, "user_can_manage_guild", slow(rec, "perm", allowed))
    saved = {}

    async def fake_save(guild_id, setup, clone_id=None, **kw):
        rec.add("save")
        saved["setup"], saved["clone_id"] = setup, clone_id

    monkeypatch.setattr(catch, "save_setup", fake_save)
    return saved


def test_wild_zone_creates_channel_saves_it_and_responds_first(monkeypatch):
    rec = Recorder()
    saved = _setup_env(monkeypatch, rec)
    inter = make_interaction(rec)
    inter.guild = _guild()
    view = catch.CatchSetupView(catch.CatchSetup(), guild_id=1, clone_id=4)
    asyncio.run(view._wild_zone(inter))
    assert_response_first(rec)
    assert [c.name for c in inter.guild.created] == ["wild-zone"]
    assert saved["setup"].spawn_channel_ids == (900,) and saved["clone_id"] == 4


def test_wild_zone_reuses_existing_channel_without_creating(monkeypatch):
    rec = Recorder()
    saved = _setup_env(monkeypatch, rec)
    inter = make_interaction(rec)
    inter.guild = _guild(channels=[_Chan(33)])
    view = catch.CatchSetupView(catch.CatchSetup(), guild_id=1)
    asyncio.run(view._wild_zone(inter))
    assert inter.guild.created == []
    assert saved["setup"].spawn_channel_ids == (33,)


def test_wild_zone_needs_manage_channels(monkeypatch):
    rec = Recorder()
    saved = _setup_env(monkeypatch, rec)
    inter = make_interaction(rec)
    inter.guild = _guild(can_manage_channels=False)
    asyncio.run(catch.CatchSetupView(catch.CatchSetup(), guild_id=1)._wild_zone(inter))
    assert inter.guild.created == [] and "setup" not in saved


def test_wild_zone_denied_without_manage_server(monkeypatch):
    rec = Recorder()
    saved = _setup_env(monkeypatch, rec, allowed=False)
    inter = make_interaction(rec)
    inter.guild = _guild()
    asyncio.run(catch.CatchSetupView(catch.CatchSetup(), guild_id=1)._wild_zone(inter))
    assert inter.guild.created == [] and "setup" not in saved


def test_test_spawn_publishes_a_real_persisted_test_spawn(monkeypatch):
    rec = Recorder()
    _setup_env(monkeypatch, rec)
    seen = {}

    async def fake_publish(channel, **kw):
        rec.add("publish")
        seen["channel"] = channel
        seen.update(kw)
        return 1

    monkeypatch.setattr(catch, "publish_spawn", fake_publish)
    inter = make_interaction(rec)
    channel = _Chan(33)
    inter.guild = _guild(channels=[channel])
    setup = catch.CatchSetup(spawn_channel_ids=(33,))
    asyncio.run(catch.CatchSetupView(setup, guild_id=1, clone_id=4)._test_spawn(inter))
    assert_response_first(rec)
    assert seen["channel"] is channel and seen["source"] == "test" and seen["clone_id"] == 4


def test_test_spawn_without_channel_publishes_nothing(monkeypatch):
    rec = Recorder()
    _setup_env(monkeypatch, rec)
    monkeypatch.setattr(catch, "publish_spawn", slow(rec, "publish"))
    inter = make_interaction(rec)
    inter.guild = _guild()
    asyncio.run(catch.CatchSetupView(catch.CatchSetup(), guild_id=1)._test_spawn(inter))
    assert "publish" not in rec.calls


def test_test_spawn_denied_without_manage_server(monkeypatch):
    rec = Recorder()
    _setup_env(monkeypatch, rec, allowed=False)
    monkeypatch.setattr(catch, "publish_spawn", slow(rec, "publish"))
    inter = make_interaction(rec)
    inter.guild = _guild(channels=[_Chan(33)])
    setup = catch.CatchSetup(spawn_channel_ids=(33,))
    asyncio.run(catch.CatchSetupView(setup, guild_id=1)._test_spawn(inter))
    assert "publish" not in rec.calls


def test_publish_spawn_persists_posts_and_links_message(monkeypatch):
    calls = []

    async def fake_create(**kw):
        calls.append(("create", kw["source"], kw["channel_id"]))
        return 77

    async def fake_attach(spawn_id, message_id):
        calls.append(("attach", spawn_id, message_id))

    monkeypatch.setattr(catch, "create_spawn", fake_create)
    monkeypatch.setattr(catch, "attach_spawn_message", fake_attach)
    channel = _Chan(33)
    spawn_id = asyncio.run(catch.publish_spawn(channel, guild_id=1, clone_id=None, setup=catch.CatchSetup(), source="test"))
    assert spawn_id == 77 and len(channel.sent) == 1
    assert calls == [("create", "test", 33), ("attach", 77, 555)]
    assert isinstance(channel.sent[0]["embed"], discord.Embed)
