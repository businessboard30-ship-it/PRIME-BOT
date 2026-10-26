import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules import anime_feed_nudge as nudge
from modules import anime_feed as af
from discord_bot.cogs import _views_anime_feed as v
from discord_bot.cogs import _views_anime_nudge as nv


def run(c):
    return asyncio.run(c)


def test_old_enough_and_set_up_rules():
    now = datetime.now(timezone.utc)
    assert nudge.old_enough(None) and nudge.old_enough(now - timedelta(days=2))
    assert not nudge.old_enough(now - timedelta(hours=2))
    assert nudge.is_set_up(True, None) and nudge.is_set_up(False, 5) and not nudge.is_set_up(False, None)


def test_nudge_card_has_one_persistent_setup_button_and_is_registered():
    view = nv.nudge_view("Prime", 42)
    ids = [c.custom_id for c in view.walk_children() if isinstance(c, discord.ui.Button)] or \
          [c.item.custom_id for c in view.walk_children() if hasattr(c, "item")]
    assert ids == ["anime_nudge:42"]
    assert nv.NudgeSetupButton in v.DYNAMIC_ITEMS
    assert not v.is_stale_anime_id("anime_nudge:42")
    assert "Manage Server" in nudge.nudge_text("Prime")


def test_wizard_has_quick_setup_create_channel_and_create_role():
    guild = SimpleNamespace(id=1, me=SimpleNamespace())
    view = v.AnimeSetupView(guild, None, 9, af.FeedConfig(1))
    labels = {c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)}
    assert {"Quick setup", "Create channel", "Create ping role", "Preview", "Close"} <= labels


def test_nudge_pass_posts_only_in_modlog_once_and_never_dms(monkeypatch):
    from discord_bot.cogs import anime_feed as cog

    class Chan(discord.TextChannel):
        def __init__(self): pass
    ch = Chan.__new__(Chan)
    ch.send = AsyncMock()
    ch.permissions_for = lambda m: SimpleNamespace(view_channel=True, send_messages=True)
    me = SimpleNamespace(joined_at=datetime.now(timezone.utc) - timedelta(days=3))
    g = SimpleNamespace(id=7, me=me, get_channel=lambda i: ch)
    g_no_log = SimpleNamespace(id=8, me=me, get_channel=lambda i: None)
    bot = SimpleNamespace(clone_id=None, guilds=[g, g_no_log], user=SimpleNamespace(display_name="Prime"))
    c = cog.AnimeFeedCog.__new__(cog.AnimeFeedCog)
    c.bot = bot
    monkeypatch.setattr(nudge, "nudged_guild_ids", AsyncMock(return_value=set()))
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(nudge, "claim", claim)
    monkeypatch.setattr(af, "get_config", AsyncMock(return_value=af.FeedConfig(7)))
    monkeypatch.setattr(cog.db, "get_automod_config", AsyncMock(side_effect=lambda gid, cid: {"log_channel_id": 99 if gid == 7 else None}))
    monkeypatch.setattr(cog.asyncio, "sleep", AsyncMock())
    assert run(c.nudge_pass()) == 1
    ch.send.assert_awaited_once()
    claim.assert_awaited_once_with(7, None)
    # a server that already set up is skipped
    ch.send.reset_mock()
    monkeypatch.setattr(af, "get_config", AsyncMock(return_value=af.FeedConfig(7, enabled=True, channel_id=5)))
    assert run(c.nudge_pass()) == 0
    ch.send.assert_not_awaited()
