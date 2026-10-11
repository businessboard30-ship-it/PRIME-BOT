"""Unadopted cats wander off after a while: the Adopt button closes and the post says so."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_cat_adopt as v
from discord_bot.cogs import cat
from modules import cat_adopt as ca, cat_help
from tests.unit.test_cat_adopt import FI


def run(c):
    return asyncio.run(c)


def test_a_gone_spawn_shows_a_disabled_ran_away_button():
    btn = v.spawn_view(5, gone=True).children[0].item
    assert btn.disabled and btn.label.startswith("Ran away") and btn.custom_id == "cat_adopt:5"


def test_tapping_an_expired_spawn_says_so_and_closes_the_post(monkeypatch):
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("expired")))
    msg = SimpleNamespace(embeds=[discord.Embed(title="x")], edit=AsyncMock())
    i = FI(10, msg)
    run(v.AdoptButton(5).callback(i))
    assert "wandered off" in i.sent[0][0] and i.sent[0][1]["ephemeral"] is True and not i.edits
    kw = msg.edit.await_args.kwargs
    assert kw["embed"].description == ca.EXPIRED_NOTE and kw["view"].children[0].item.disabled


def test_fade_spawns_edits_old_posts_and_skips_missing_ones(monkeypatch):
    rows = [dict(id=1, guild_id=7, channel_id=2, message_id=30, pool_id=9),
            dict(id=2, guild_id=7, channel_id=2, message_id=None, pool_id=10),
            dict(id=3, guild_id=8, channel_id=2, message_id=31, pool_id=11)]
    monkeypatch.setattr(ca, "expire_spawns", AsyncMock(return_value=rows))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    message = SimpleNamespace(embeds=[discord.Embed(title="cat")], edit=AsyncMock())
    ch = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    guild = SimpleNamespace(get_channel=lambda cid: ch)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 7 else None)
    c = cat.CatCog.__new__(cat.CatCog)
    c.bot = bot
    assert run(c.fade_spawns(None)) == 3
    ch.fetch_message.assert_awaited_once_with(30)
    assert message.edit.await_args.kwargs["embed"].description == ca.EXPIRED_NOTE


def test_a_message_that_cannot_be_edited_does_not_stop_the_sweep(monkeypatch):
    rows = [dict(id=1, guild_id=7, channel_id=2, message_id=30, pool_id=9)]
    monkeypatch.setattr(ca, "expire_spawns", AsyncMock(return_value=rows))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    resp = MagicMock(status=404, reason="Not Found")
    ch = SimpleNamespace(fetch_message=AsyncMock(side_effect=discord.NotFound(resp, "gone")))
    c = cat.CatCog.__new__(cat.CatCog)
    c.bot = SimpleNamespace(get_guild=lambda gid: SimpleNamespace(get_channel=lambda cid: ch))
    assert run(c.fade_spawns(None)) == 1


def test_help_mentions_the_time_limit():
    assert f"{ca.SPAWN_EXPIRE_HOURS} hours" in cat_help.build_text()


def test_sql_closes_only_old_open_spawns():
    import inspect
    src = inspect.getsource(ca.expire_spawns) + inspect.getsource(ca.adopt)
    assert "adopted_by IS NULL" in src and "make_interval(hours" in src
