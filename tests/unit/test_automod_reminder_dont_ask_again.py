"""'Don't ask again' on the automod reminder must stick: even when its batch row is gone, and even if a stale cached
config is sitting in memory when something else saves the automod row."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import database
from database import db
from discord_bot.cogs import _views_automod_reminders as rem


def run(c):
    return asyncio.run(c)


def _interaction(guild_id=10, clone_id=None, done=False):
    resp = MagicMock()
    resp.is_done.return_value = done
    resp.edit_message = AsyncMock()
    resp.send_message = AsyncMock()
    msg = SimpleNamespace(components=[])
    return SimpleNamespace(guild_id=guild_id, client=SimpleNamespace(clone_id=clone_id), response=resp,
                           followup=SimpleNamespace(send=AsyncMock()), message=msg)


def test_dont_ask_again_still_applies_when_batch_row_is_gone(monkeypatch):
    monkeypatch.setattr(db, "get_automod_reminder_batch", AsyncMock(return_value=None), raising=False)
    setter = AsyncMock()
    monkeypatch.setattr(db, "set_automod_config", setter, raising=False)
    i = _interaction(guild_id=10, clone_id=3)
    run(rem._ReminderDismissButton(99).callback(i))
    kwargs = setter.await_args.kwargs
    assert setter.await_args.args == (10,) and kwargs["clone_id"] == 3
    assert kwargs["wordfilter_notice_count"] >= 3 and kwargs["log_channel_notice_count"] >= 3


def test_remind_later_applies_without_batch_too(monkeypatch):
    monkeypatch.setattr(db, "get_automod_reminder_batch", AsyncMock(return_value=None), raising=False)
    setter = AsyncMock()
    monkeypatch.setattr(db, "set_automod_config", setter, raising=False)
    run(rem._ReminderLaterButton(99).callback(_interaction()))
    assert "wordfilter_last_notice_at" in setter.await_args.kwargs


def test_dont_ask_again_does_not_claim_success_when_nothing_could_be_saved(monkeypatch):
    monkeypatch.setattr(db, "get_automod_reminder_batch", AsyncMock(return_value=None), raising=False)
    setter = AsyncMock()
    monkeypatch.setattr(db, "set_automod_config", setter, raising=False)
    i = _interaction(guild_id=None)
    run(rem._ReminderDismissButton(99).callback(i))
    setter.assert_not_awaited()
    assert "couldn't save" in i.response.send_message.await_args.args[0]


def test_set_automod_config_merges_into_a_fresh_read_not_the_cache(monkeypatch):
    getter = AsyncMock(return_value={**db._AUTOMOD_DEFAULTS, "banned_words": []})
    monkeypatch.setattr(db, "get_automod_config", getter)

    class Conn:
        async def execute(self, *a, **k): return None

    class Acquire:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acquire()

    async def get_pool(): return Pool()
    monkeypatch.setattr(database, "get_pool", get_pool)
    run(database.Database.set_automod_config(db, 10, None, wordfilter_notice_count=3)
        if hasattr(database, "Database") else db.set_automod_config(10, None, wordfilter_notice_count=3))
    assert getter.await_args.kwargs.get("fresh") is True
