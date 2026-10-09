"""Automod must not log two INFO lines per message; only a slow config fetch is logged (WARNING)."""
import asyncio
import logging
import types

from discord_bot.cogs import automod as am


def _cog():
    cog = am.AutomodCog.__new__(am.AutomodCog)
    cog.bot = types.SimpleNamespace(clone_id=None)
    return cog


def _msg():
    guild = types.SimpleNamespace(id=7)
    author = types.SimpleNamespace(bot=False)          # not a Member -> skips the manage_messages shortcut
    return types.SimpleNamespace(author=author, guild=guild, id=99, content="hello", mentions=[], role_mentions=[])


def _patch(monkeypatch, delay=0.0):
    async def cfg(gid, clone_id=None):
        if delay:
            await asyncio.sleep(delay)
        return {}

    async def flood(*a, **k):
        return None
    monkeypatch.setattr(am.db, "get_automod_config", cfg)
    monkeypatch.setattr(am.modx, "record_flood_event", flood, raising=False)


def test_normal_message_logs_nothing_at_info(monkeypatch, caplog):
    _patch(monkeypatch)
    m = _msg()
    m.author = types.SimpleNamespace(bot=False, id=1)
    with caplog.at_level(logging.INFO, logger=am.logger.name):
        try:
            asyncio.run(_cog().on_message(m))
        except Exception:
            pass                                    # later filters may need more of a real message; only logging matters here
    assert not [r for r in caplog.records if "[automod]" in r.getMessage()]


def test_slow_config_fetch_warns(monkeypatch, caplog):
    _patch(monkeypatch)
    monkeypatch.setattr(am, "SLOW_CONFIG_FETCH_SECONDS", 0.01)
    _patch(monkeypatch, delay=0.05)
    m = _msg()
    m.author = types.SimpleNamespace(bot=False, id=1)
    with caplog.at_level(logging.INFO, logger=am.logger.name):
        try:
            asyncio.run(_cog().on_message(m))
        except Exception:
            pass
    warns = [r for r in caplog.records if "slow config fetch" in r.getMessage()]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING
