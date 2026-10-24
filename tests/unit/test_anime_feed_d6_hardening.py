"""Anime feed D6: kill switches, per-pass counters, and the safety net for old buttons. No network, no database."""
import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest

from modules import anime_feed as af
from modules import anime_feed_alerts as al
from modules import anime_feed_worker as aw


def run(c):
    return asyncio.run(c)


# -- kill switches ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(None, True), ("", True), ("  ", True), ("1", True), ("true", True), ("yes", True), ("anything", True),
                                            ("0", False), ("false", False), ("OFF", False), (" No ", False), ("disabled", False)])
def test_switch_values(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ANIME_FEED_ENABLED", raising=False)
        monkeypatch.delenv("ANIME_ALERTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("ANIME_FEED_ENABLED", value)
        monkeypatch.setenv("ANIME_ALERTS_ENABLED", value)
    assert af.feed_enabled() is expected and af.alerts_enabled() is expected


def lifecycle(monkeypatch, dm_work, guilds, items=None):
    calls = SimpleNamespace(provider=0, guilds=0, dm=0)

    async def enabled(clone_id=None):
        calls.guilds += 1
        return guilds

    async def airing():
        calls.provider += 1
        return items or [], True

    async def has_dm():
        calls.dm += 1
        return dm_work
    monkeypatch.setattr(af, "enabled_guilds", enabled)
    monkeypatch.setattr(af, "airing_soon", airing)
    monkeypatch.setattr(al, "has_dm_followers", has_dm)
    bot = SimpleNamespace(get_guild=lambda g: object(), wait_until_ready=None)
    return aw.Worker(bot, None, post_gap=0), calls


def test_both_switches_off_means_idle_with_no_database_and_no_provider_requests(monkeypatch, caplog):
    monkeypatch.setenv("ANIME_FEED_ENABLED", "0")
    monkeypatch.setenv("ANIME_ALERTS_ENABLED", "0")
    w, calls = lifecycle(monkeypatch, True, [af.FeedConfig(1, None, True, 100, None, None, "both", 60, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))])
    with caplog.at_level(logging.WARNING):
        assert run(w.cycle()) is None and run(w.cycle()) is None
    assert (calls.provider, calls.guilds, calls.dm) == (0, 0, 0)
    assert sum("paused by" in r.getMessage() for r in caplog.records) == 1            # logged once, not every pass


def test_feed_off_stops_server_posts_but_dm_alerts_still_run(monkeypatch):
    monkeypatch.setenv("ANIME_FEED_ENABLED", "0")
    monkeypatch.delenv("ANIME_ALERTS_ENABLED", raising=False)
    w, calls = lifecycle(monkeypatch, True, [af.FeedConfig(1, None, True, 100, None, None, "both", 60, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))])
    assert run(w.cycle()) is not None
    assert calls.guilds == 0 and calls.dm == 1 and calls.provider == 1               # servers never read; the DM pass still happens


def test_alerts_off_stops_dms_but_server_posts_still_run(monkeypatch):
    monkeypatch.setenv("ANIME_ALERTS_ENABLED", "0")
    monkeypatch.delenv("ANIME_FEED_ENABLED", raising=False)
    w, calls = lifecycle(monkeypatch, True, [af.FeedConfig(1, None, True, 100, None, None, "both", 60, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))])
    assert run(w.cycle()) is not None
    assert calls.dm == 0 and calls.guilds == 1 and calls.provider == 0               # DM state is not even read


def test_switches_default_on_so_nothing_changes_without_the_variables(monkeypatch):
    monkeypatch.delenv("ANIME_FEED_ENABLED", raising=False)
    monkeypatch.delenv("ANIME_ALERTS_ENABLED", raising=False)
    w, calls = lifecycle(monkeypatch, False, [])
    assert run(w.cycle()) is None and (calls.guilds, calls.dm, calls.provider) == (1, 1, 0)      # idle only because nobody has anything on


# -- per-pass counters -----------------------------------------------------------------------

def test_a_pass_counts_what_it_did_and_logs_one_line(monkeypatch, caplog):
    monkeypatch.delenv("ANIME_FEED_ENABLED", raising=False)
    monkeypatch.delenv("ANIME_ALERTS_ENABLED", raising=False)
    w, calls = lifecycle(monkeypatch, True, [af.FeedConfig(1, None, True, 100, None, None, "both", 60, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))])

    async def dms(items, now):
        return 3, True
    monkeypatch.setattr(w, "_dm_alerts", dms)
    with caplog.at_level(logging.INFO):
        assert run(w.cycle()) == aw.MIN_SLEEP                                         # a capped DM queue means a short sleep
    s = w.last_pass
    assert (s.servers, s.posted, s.dms, s.dm_backlog) == (1, 0, 3, True) and s.busy and s.ms >= 0
    lines = [r.getMessage() for r in caplog.records if "pass:" in r.getMessage()]
    assert len(lines) == 1 and "dms=3" in lines[0] and "dm_backlog=1" in lines[0] and "servers=1" in lines[0]


def test_a_quiet_pass_logs_at_debug_not_info(monkeypatch, caplog):
    monkeypatch.delenv("ANIME_FEED_ENABLED", raising=False)
    monkeypatch.delenv("ANIME_ALERTS_ENABLED", raising=False)
    w, _ = lifecycle(monkeypatch, False, [af.FeedConfig(1, None, True, 100, None, None, "both", 60, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))])
    with caplog.at_level(logging.INFO):
        run(w.cycle())
    assert not w.last_pass.busy and not [r for r in caplog.records if "pass:" in r.getMessage()]


# -- old buttons -----------------------------------------------------------------------------

def test_every_registered_item_family_is_known_to_the_net():
    import re
    from discord_bot.cogs import _views_anime_feed as v
    fams = {re.match(r"\^?([a-z_]+)", c.__discord_ui_compiled_template__.pattern).group(1) for c in v._all_dynamic_items()}
    assert fams <= v.LIVE_FAMILIES                                                    # a new button family must be added there too


def test_live_ids_are_never_stale_and_unmatched_ids_of_ours_are():
    from discord_bot.cogs import _views_anime_feed as v
    for live in ("anime_follow:12", "anime_more:5", "anime_alert:7", "anime_search", "anime_cats", "anime_catsel", "anime_plus", "anime_plus_pay",
                 "anime_plus_verify", "anime_pg:c:1:romance", "anime_amode:3"):
        assert v.is_stale_anime_id(live) is False, live
    assert v.is_stale_anime_id("anime_follow:notanumber") is True                    # our family, matches no template
    assert v.is_stale_anime_id("anime_pg:x:1:key") is True
    assert v.is_stale_anime_id("something_else:1") is False and v.is_stale_anime_id("anime_unrelated") is False   # never another module's ids
    assert v.is_stale_anime_id(None) is False and v.is_stale_anime_id("") is False


def test_a_retired_family_gets_the_message(monkeypatch):
    from discord_bot.cogs import _views_anime_feed as v
    assert v.is_stale_anime_id("anime_oldthing:9") is False
    monkeypatch.setattr(v, "RETIRED_FAMILIES", frozenset({"anime_oldthing"}))
    assert v.is_stale_anime_id("anime_oldthing:9") is True


def _inter(custom_id, kind=discord.InteractionType.component, done=False):
    sent = []

    async def send_message(text, **k):
        sent.append((text, k))
    return SimpleNamespace(type=kind, data={"custom_id": custom_id}, response=SimpleNamespace(is_done=lambda: done, send_message=send_message)), sent


def test_the_listener_answers_only_stale_presses_privately_and_leaves_everything_else_alone():
    from discord_bot.cogs import _views_anime_feed as v
    i, sent = _inter("anime_follow:oops")
    run(v.stale_button_listener(i))
    assert len(sent) == 1 and sent[0][0] == v.STALE_TEXT and sent[0][1]["ephemeral"] is True
    assert isinstance(sent[0][1]["allowed_mentions"], discord.AllowedMentions)
    for cid, kind, done in (("anime_follow:12", discord.InteractionType.component, False),      # live: the item answers it
                            ("other:1", discord.InteractionType.component, False),               # not ours
                            ("anime_follow:oops", discord.InteractionType.application_command, False),   # not a button press
                            ("anime_follow:oops", discord.InteractionType.component, True)):     # already answered
        i, sent = _inter(cid, kind, done)
        run(v.stale_button_listener(i))
        assert sent == [], (cid, kind, done)


def test_the_listener_never_raises():
    from discord_bot.cogs import _views_anime_feed as v
    run(v.stale_button_listener(SimpleNamespace(type=None, data=None, response=None)))


def test_the_bot_registers_the_listener():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "discord_bot/bot.py").read_text()
    assert "stale_button_listener" in src and 'add_listener(stale_button_listener, "on_interaction")' in src
