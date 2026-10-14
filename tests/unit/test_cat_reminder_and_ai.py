"""Cat game reminder (max 2, one week, only while the game is off) and the AI chat's cat picture + setup knowledge."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import ai_tools
from discord_bot.cogs import cat
from modules import ai_features as af
from modules import cat_reminder as cr

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def d(days):
    return NOW - timedelta(days=days)


# ── the rule: 2 reminders, one week, then never ──────────────────────────
def test_first_reminder_is_due_when_nothing_was_sent():
    assert cr.is_due(None, NOW)


def test_second_reminder_waits_three_days_and_stays_inside_the_week():
    assert not cr.is_due((1, d(1), d(1)), NOW)                  # too soon
    assert cr.is_due((1, d(3), d(3)), NOW)                      # 3 days later
    assert cr.is_due((1, d(6.5), d(6.5)), NOW)                  # still inside the week
    assert not cr.is_due((1, d(7), d(7)), NOW)                  # the week is over: no more


def test_never_more_than_two_and_never_after_the_week():
    assert not cr.is_due((2, d(4), d(1)), NOW)
    assert not cr.is_due((2, d(6), d(3)), NOW)
    for days in (7, 8, 30, 365):
        assert not cr.is_due((1, d(days), d(days)), NOW)


def test_simulated_week_sends_exactly_two():
    state, sent = None, 0
    for hour in range(0, 24 * 40, 6):                          # a tick every 6 h for 40 days
        t = NOW + timedelta(hours=hour)
        if cr.is_due(state, t):
            sent += 1
            state = (1 if state is None else state[0] + 1, state[1] if state else t, t)
    assert sent == 2


def _g(gid, joined_days_ago=30):
    return SimpleNamespace(id=gid, me=SimpleNamespace(joined_at=d(joined_days_ago)), name=f"g{gid}", owner_id=gid * 10)


def test_candidates_skip_servers_with_the_game_on_new_servers_and_done_servers():
    guilds = [_g(1), _g(2), _g(3, joined_days_ago=1), _g(4), _g(5)]
    off = {1: True, 2: False, 3: True, 4: True, 5: True}
    known = {4: (2, d(5), d(2))}
    assert [g.id for g in cr.pick_candidates(guilds, off, known, NOW)] == [1, 5]


def test_candidates_are_limited_per_tick():
    guilds = [_g(i) for i in range(1, 30)]
    assert len(cr.pick_candidates(guilds, {}, {}, NOW, limit=5)) == 5


def test_game_is_off_means_no_setup_or_no_channel():
    assert cr.game_is_off(False, (5,)) and cr.game_is_off(True, ()) and not cr.game_is_off(True, (5,))


def test_text_explains_setup_and_promises_no_more():
    t = cr.reminder_text("My Server")
    assert "/catsetup" in t and "Turn on" in t and "only two reminders" in t


def test_migration_and_wiring():
    sql = (ROOT / "database/migrations/038_cat_setup_reminders.sql").read_text()
    assert "cat_setup_reminders" in sql and "UNIQUE INDEX" in sql
    db = (ROOT / "database.py").read_text()
    assert "038_cat_setup_reminders.sql" in db
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 83


# ── the Cat game never DMs and never runs the reminder loop ──────────────
def test_cat_cog_has_no_reminder_loop_and_no_dms():
    src = (ROOT / "discord_bot/cogs/cat.py").read_text()
    assert "_reminders" not in src and "remind_due" not in src
    assert "owner.send(" not in src and "user.send(" not in src and "_dm(" not in src


def test_cat_token_is_deleted_and_nothing_is_attached():
    out = af.render_cat_image("Set it up with /catsetup. [[CAT]]")
    assert out == "Set it up with /catsetup."
    assert af.render_cat_image("hi [[cat]] and [[ CAT ]]") == "hi and"
    assert af.render_cat_image("no token here") == "no token here"


def test_ai_has_no_cat_picture_code_left():
    src = (ROOT / "discord_bot/cogs/ai_tools.py").read_text()
    for gone in ("strip_cat_marker", "cat_image_embed", "send_with_cat", "CAT_IMAGE_MARKER"):
        assert gone not in src
    assert not hasattr(af, "CAT_IMAGE_MARKER")


def test_ai_knows_how_to_set_up_the_game_but_never_sends_a_cat():
    src = (ROOT / "modules/ai_features.py").read_text()
    for needle in ("/catsetup", "spawn hours", "Turn on", "trade ID", "Adopt me!"):
        assert needle in src
    assert "never send, show, attach or produce a cat picture" in src
    assert "{CAT_TOKEN} at the very end" not in src
