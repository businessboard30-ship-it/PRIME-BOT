"""The Spider Realm Pro 'Try it free (3 days)' announcement is switched off; old messages' buttons keep working."""
import asyncio
import inspect
from types import SimpleNamespace

from discord_bot.cogs import welcome as w


def test_flag_is_off():
    assert w.SPIDER_ANNOUNCE_ENABLED is False


def test_cog_load_does_not_start_the_loop_while_off():
    src = inspect.getsource(w.WelcomeCog.cog_load)
    assert "if SPIDER_ANNOUNCE_ENABLED:" in src
    assert src.index("if SPIDER_ANNOUNCE_ENABLED:") < src.index("self._announce_spider_pro.start()")
    assert src.count("_announce_spider_pro.start()") == 1


def test_even_if_the_loop_body_runs_nothing_is_sent_or_read():
    cog = object.__new__(w.WelcomeCog)
    calls = []

    async def sender(*a, **k):
        calls.append("send"); return True

    async def ids(*a, **k):
        calls.append("ids"); return set()
    cog._send_spider_post, cog._spider_announced_ids = sender, ids
    cog.bot = SimpleNamespace(guilds=[SimpleNamespace(id=1)], clone_id=None)
    asyncio.run(w.WelcomeCog._announce_spider_pro.coro(cog))
    assert calls == []


def test_buttons_on_already_posted_messages_still_work():
    src = inspect.getsource(w.WelcomeCog)
    assert "elif custom_id.startswith(SPIDER_PREFIX):" in src and "_handle_spider_button" in src
    assert hasattr(w.WelcomeCog, "_handle_spider_button") and hasattr(w.WelcomeCog, "_ensure_spider_table")
