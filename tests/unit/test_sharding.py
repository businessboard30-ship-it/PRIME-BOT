"""Sharding: configuration, health report, and the real bot class."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from discord.ext import commands

from modules import sharding as sh


# ---- configuration ----------------------------------------------------------------------------

def test_default_is_automatic():
    cfg = sh.resolve_shard_config({})
    assert cfg.shard_count is None and cfg.is_auto and cfg.source == "auto"


@pytest.mark.parametrize("raw", ["", "  ", "auto", "AUTO"])
def test_blank_or_auto_is_automatic(raw):
    assert sh.resolve_shard_config({sh.ENV_SHARD_COUNT: raw}).is_auto


def test_override_is_honoured_for_main_bot():
    cfg = sh.resolve_shard_config({sh.ENV_SHARD_COUNT: "4"})
    assert cfg.shard_count == 4 and cfg.source == "env"


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "2.5", "1001"])
def test_bad_override_stops_startup_with_a_clear_message(raw):
    with pytest.raises(sh.ShardConfigError) as err:
        sh.resolve_shard_config({sh.ENV_SHARD_COUNT: raw})
    assert sh.ENV_SHARD_COUNT in str(err.value)


def test_clones_ignore_the_override():
    assert sh.resolve_shard_config({sh.ENV_SHARD_COUNT: "9"}, clone_id=3).is_auto


def test_threshold_is_2500_not_1000():
    assert sh.MANDATORY_SHARD_THRESHOLD == 2500


# ---- status and health ------------------------------------------------------------------------

def _shard(closed=False, limited=False, latency=0.05):
    return SimpleNamespace(is_closed=lambda: closed, is_ws_ratelimited=lambda: limited, latency=latency)


def _bot(shards, guild_shards):
    return SimpleNamespace(shards=shards, guilds=[SimpleNamespace(shard_id=i) for i in guild_shards], latency=0.05)


def test_snapshot_counts_guilds_per_shard():
    bot = _bot({0: _shard(), 1: _shard()}, [0, 0, 1])
    out = sh.snapshot(bot)
    assert [(s.shard_id, s.guilds, s.latency_ms) for s in out] == [(0, 2, 50), (1, 1, 50)]


def test_unsharded_bot_reports_one_shard():
    bot = SimpleNamespace(guilds=[SimpleNamespace(shard_id=None)] * 3, latency=0.1)
    out = sh.snapshot(bot)
    assert len(out) == 1 and out[0].guilds == 3 and out[0].shard_id == 0


def test_one_down_shard_is_not_reported_healthy():
    out = sh.snapshot(_bot({0: _shard(), 1: _shard(closed=True)}, [0, 1]))
    summary = sh.health_summary(out)
    assert summary["all_healthy"] is False and summary["down_shards"] == [1]
    assert summary["total_guilds"] == 2


def test_nan_latency_is_handled():
    out = sh.snapshot(_bot({0: _shard(latency=float("nan"))}, [0]))
    assert out[0].latency_ms is None


def test_broken_shard_object_counts_as_down_not_crash():
    broken = SimpleNamespace(is_closed=MagicMock(side_effect=RuntimeError("x")), is_ws_ratelimited=lambda: False, latency=0.1)
    assert sh.snapshot(_bot({0: broken}, [0]))[0].closed is True


def test_capacity_warnings():
    assert sh.capacity_warning(1000, 1) is None
    assert sh.capacity_warning(2600, 1) and "requires sharding" in sh.capacity_warning(2600, 1)
    assert sh.capacity_warning(4500, 2) and "per shard" in sh.capacity_warning(4500, 2)
    assert sh.capacity_warning(5000, 5) is None


def test_report_has_no_secrets_and_lists_down_shards():
    out = sh.snapshot(_bot({0: _shard(), 1: _shard(closed=True)}, [0, 1]))
    text = sh.format_report(out, sh.resolve_shard_config({}))
    assert "shard(s) down: 1" in text and "automatic" in text


def test_report_truncates_many_shards():
    shards = {i: _shard() for i in range(40)}
    text = sh.format_report(sh.snapshot(_bot(shards, [])), max_rows=10)
    assert "30 more shard" in text


# ---- the real bot class -----------------------------------------------------------------------

def test_bot_is_auto_sharded_with_automatic_count(monkeypatch):
    monkeypatch.delenv(sh.ENV_SHARD_COUNT, raising=False)
    from discord_bot.bot import AnimeBotDiscord

    async def go():
        bot = AnimeBotDiscord()  # never logged in, so there is nothing to close
        assert isinstance(bot, commands.AutoShardedBot)
        assert bot.shard_count is None  # resolved by discord.py from Discord at login
    asyncio.run(go())


def test_bot_honours_override_and_clone_ignores_it(monkeypatch):
    monkeypatch.setenv(sh.ENV_SHARD_COUNT, "3")
    from discord_bot.bot import AnimeBotDiscord

    async def go():
        main_bot, clone = AnimeBotDiscord(), AnimeBotDiscord(clone_id=7)
        assert main_bot.shard_count == 3
        assert clone.shard_count is None
    asyncio.run(go())


def test_shard_events_log_ids_without_crashing(monkeypatch, caplog):
    monkeypatch.delenv(sh.ENV_SHARD_COUNT, raising=False)
    from discord_bot.bot import AnimeBotDiscord

    async def go():
        bot = AnimeBotDiscord()
        with caplog.at_level("INFO", logger="discord_bot"):
            await bot.on_shard_connect(2)
            await bot.on_shard_ready(2)
            await bot.on_shard_resumed(2)
            await bot.on_shard_disconnect(2)
            bot._log_shard_summary()
    asyncio.run(go())
    text = caplog.text
    assert "shard 2 connected" in text and "shard 2 disconnected" in text and "1 shard(s)" in text
    assert "token" not in text.lower()


# ---- owner-only diagnostic ---------------------------------------------------------------------

def test_admin_shards_command_is_owner_only(monkeypatch):
    from discord_bot.cogs import admin as adminmod
    monkeypatch.setattr(adminmod, "_is_bot_admin", lambda uid: uid == 1)
    denied = AsyncMock()
    monkeypatch.setattr(adminmod, "_deny", denied)
    cog = adminmod.AdminCog.__new__(adminmod.AdminCog)
    cog.bot = _bot({0: _shard()}, [0])

    def inter(uid):
        return SimpleNamespace(user=SimpleNamespace(id=uid), response=SimpleNamespace(send_message=AsyncMock()))

    async def go():
        stranger, owner = inter(2), inter(1)
        await adminmod.AdminCog.shards.callback(cog, stranger)
        denied.assert_awaited_once()
        stranger.response.send_message.assert_not_called()
        await adminmod.AdminCog.shards.callback(cog, owner)
        owner.response.send_message.assert_awaited_once()
        assert owner.response.send_message.call_args.kwargs["ephemeral"] is True
    asyncio.run(go())
