"""Daily "not ready" card: rendering, the optional-card rules, and how open_daily uses it."""

import asyncio
import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import discord_bot.cogs._views_catch_items as items_views
from modules import catch_daily_wait_card as wait
from modules.catch_coin_card import DAILY_FILE, DAILY_H
from modules.catch_game import BALLS
from modules.catch_i18n import text
from modules.catch_items import DailyResult, DailyReward
from tests.unit.test_catch_interaction_timing import Recorder, make_interaction, slow

REAL_CACHED = wait._cached_wait   # the monkeypatched failure tests replace the module attribute
PNG = b"\x89PNG\r\n\x1a\n"
BALL = next(iter(BALLS))
PIPS = (280, 180, 500, 208)      # the streak pip row
TAG = (600, 180, 700, 208)       # the "DAY n" tag


def run(coro):
    return asyncio.run(coro)


def crop(data: bytes, box):
    return Image.open(io.BytesIO(data)).convert("RGB").crop(box).tobytes()


def sends_to(inter):
    sent = []

    async def send(*args, **kwargs):
        sent.append({"args": args, **kwargs})

    inter.followup = SimpleNamespace(send=send)
    return sent


def daily_inter(monkeypatch, result):
    rec = Recorder()
    monkeypatch.setattr(items_views, "check_player_allowed", slow(rec, "gate", SimpleNamespace(allowed=True, reason=None)))
    monkeypatch.setattr(items_views, "ensure_starter_kit", slow(rec, "starter", False))
    monkeypatch.setattr(items_views, "claim_daily", slow(rec, "claim", result))
    inter = make_interaction(rec)
    return inter, sends_to(inter)


def waiting(streak=2):
    return DailyResult(False, streak, datetime.now(timezone.utc) + timedelta(hours=5))


@pytest.fixture(autouse=True)
def fresh_cache():
    REAL_CACHED.cache_clear()
    yield
    REAL_CACHED.cache_clear()


# ---------------------------------------------------------------- rendering

@pytest.mark.parametrize("streak", [0, 1, 7, 8, 120, 10**9, -3])
def test_card_renders_for_any_streak(streak):
    data = wait._cached_wait(streak)
    assert data.startswith(PNG) and Image.open(io.BytesIO(data)).size == (720, DAILY_H)


def test_pips_follow_the_streak_and_wrap_every_seven_days():
    assert crop(wait._cached_wait(2), PIPS) != crop(wait._cached_wait(1), PIPS)
    assert crop(wait._cached_wait(7), PIPS) != crop(wait._cached_wait(6), PIPS)
    assert crop(wait._cached_wait(8), PIPS) == crop(wait._cached_wait(1), PIPS)   # wraps back to one pip
    assert crop(wait._cached_wait(0), PIPS) != crop(wait._cached_wait(1), PIPS)


def test_a_filled_pip_is_gold_and_an_empty_one_is_dark():
    def centre(streak, i=0):
        return Image.open(io.BytesIO(wait._cached_wait(streak))).convert("RGB").getpixel((292 + i * 28, 194))

    gold = wait._gold()
    assert all(abs(a - b) <= 3 for a, b in zip(centre(1), gold))
    assert all(abs(a - b) <= 3 for a, b in zip(centre(3, 2), gold))
    assert max(centre(1, 1)) < 60 and max(centre(0)) < 60


def test_day_tag_is_drawn_for_a_streak_and_absent_without_one():
    assert crop(wait._cached_wait(8), TAG) != crop(wait._cached_wait(1), TAG)      # same pips, different number
    assert crop(wait._cached_wait(0), TAG) != crop(wait._cached_wait(1), TAG)
    assert crop(wait._cached_wait(0), TAG) == crop(wait._cached_wait(-4), TAG)
    assert Image.open(io.BytesIO(wait._cached_wait(0))).convert("RGB").crop(TAG).getextrema()[0][1] < 120  # nothing bright: no text
    assert Image.open(io.BytesIO(wait._cached_wait(1))).convert("RGB").crop(TAG).getextrema()[0][1] > 200  # white text


def test_negative_streak_is_drawn_as_zero():
    assert wait._cached_wait(-5) == wait._cached_wait(0)


def test_card_differs_from_the_claimed_reward_card():
    from modules import catch_coin_card as coin
    assert wait._cached_wait(3) != coin._cached_daily(250, 3, ((BALL, 1),))


def test_renders_are_cached_per_streak():
    assert wait._cached_wait(3) is wait._cached_wait(3)
    first = wait._cached_wait(3)
    wait.clear_daily_wait_cache()
    again = wait._cached_wait(3)
    assert again == first and again is not first


# ---------------------------------------------------------------- wrapper

def test_art_points_the_embed_at_the_card_and_keeps_every_text_part():
    embed = discord.Embed(title="T", description="ready <t:1:R>")
    embed.add_field(name="F", value="v")
    file = run(wait.daily_wait_art(embed, streak=3))
    assert file.filename == wait.WAIT_FILE and embed.image.url == f"attachment://{wait.WAIT_FILE}"
    assert embed.title == "T" and embed.description == "ready <t:1:R>" and [f.name for f in embed.fields] == ["F"]
    assert file.fp.getvalue() == wait._cached_wait(3)


def test_negative_streaks_share_the_zero_cache_entry():
    run(wait.daily_wait_art(discord.Embed(), streak=-3))
    run(wait.daily_wait_art(discord.Embed(), streak=0))
    assert REAL_CACHED.cache_info().currsize == 1


def test_each_call_returns_a_fresh_file():
    one = run(wait.daily_wait_art(discord.Embed(), streak=3))
    two = run(wait.daily_wait_art(discord.Embed(), streak=3))
    assert one is not two and one.fp is not two.fp and one.fp.getvalue() == two.fp.getvalue()


def test_failure_leaves_the_embed_untouched(monkeypatch):
    def boom(streak):
        raise RuntimeError("no font")

    monkeypatch.setattr(wait, "_cached_wait", boom)
    embed = discord.Embed(title="T", description="d")
    assert run(wait.daily_wait_art(embed, streak=1)) is None
    assert embed.image.url is None and embed.description == "d"


# ---------------------------------------------------------------- the screen

def test_not_ready_daily_sends_the_card_with_the_unchanged_embed_text(monkeypatch):
    result = waiting(4)
    inter, sent = daily_inter(monkeypatch, result)
    run(items_views.open_daily(inter))
    assert len(sent) == 1 and sent[0]["ephemeral"] is True
    assert sent[0]["file"].filename == wait.WAIT_FILE and sent[0]["embed"].image.url == f"attachment://{wait.WAIT_FILE}"
    plain = items_views.daily_embed(result)
    assert sent[0]["embed"].title == plain.title == text("daily.wait_title")
    assert sent[0]["embed"].description == plain.description and "<t:" in sent[0]["embed"].description


def test_view_hands_the_renderer_the_real_streak(monkeypatch):
    seen = []
    real = items_views.daily_wait_art

    async def spy(embed, *, streak):
        seen.append(streak)
        return await real(embed, streak=streak)

    monkeypatch.setattr(items_views, "daily_wait_art", spy)
    inter, sent = daily_inter(monkeypatch, waiting(6))
    run(items_views.open_daily(inter))
    assert seen == [6] and sent[0]["file"].fp.getvalue() == wait._cached_wait(6)


def test_a_failed_render_still_sends_the_plain_embed(monkeypatch):
    def boom(streak):
        raise RuntimeError("no font")

    monkeypatch.setattr(wait, "_cached_wait", boom)
    inter, sent = daily_inter(monkeypatch, waiting(2))
    run(items_views.open_daily(inter))
    assert "file" not in sent[0] and sent[0]["embed"].image.url is None
    assert sent[0]["embed"].description == items_views.daily_embed(waiting(2)).description


def test_a_claimed_daily_still_uses_the_reward_card_not_the_wait_card(monkeypatch):
    called = []

    async def spy(embed, *, streak):
        called.append(streak)

    monkeypatch.setattr(items_views, "daily_wait_art", spy)
    soon = datetime.now(timezone.utc) + timedelta(hours=20)
    inter, sent = daily_inter(monkeypatch, DailyResult(True, 3, soon, DailyReward(250, {BALL: 3})))
    run(items_views.open_daily(inter))
    assert called == [] and sent[0]["file"].filename == DAILY_FILE
