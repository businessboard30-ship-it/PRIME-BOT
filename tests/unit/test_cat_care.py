"""Cat care: water, bed, food, random hunger, and cats that wander off (play coins only)."""
import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from discord_bot.cogs import _views_cat_adopt as v
from discord_bot.cogs import cat
from modules import cat_adopt as ca
from modules import cat_care as cc
from modules import cat_pool, cat_wallet as cw
from tests.unit.test_cat_adopt import FI, mkcat

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def no_images(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value="https://cdn.discordapp.com/a/b/c.jpg"))


# ── rules ─────────────────────────────────────────────────────────────────
def test_the_design_numbers():
    assert (cc.WATER_COST, cc.BED_COST, cc.FOOD_COST) == (30, 120, 80)
    assert cc.LEAVE_AFTER_HOURS == 48 and cc.WARN_AFTER_HOURS < cc.LEAVE_AFTER_HOURS


def test_water_and_bed_each_halve_the_hunger_chance():
    base = cc.hunger_chance(False, False)
    assert cc.hunger_chance(True, False) == base / 2 == cc.hunger_chance(False, True)
    assert cc.hunger_chance(True, True) == base / 4


def test_hungry_cat_leaves_48_hours_after_it_got_hungry():
    c = dataclasses.replace(mkcat(), hungry_since=NOW)
    assert cc.is_hungry(c) and cc.leaves_at(c) == NOW + timedelta(hours=48)
    assert not cc.is_hungry(mkcat()) and cc.leaves_at(mkcat()) is None


def test_status_lines_for_a_happy_cat_and_a_hungry_one():
    happy = cc.status_lines(mkcat(), NOW)
    assert "Happy and fed" in happy and "none right now" in happy
    hungry = cc.status_lines(dataclasses.replace(mkcat(), hungry_since=NOW, bed_until=NOW + timedelta(hours=5)), NOW)
    assert "Hungry" in hungry and "wander off" in hungry and "<t:" in hungry and "rented until" in hungry


def test_owner_texts_are_sad_but_never_hurtful():
    for text in (cc.runaway_owner_text(["Mochi"]), cc.runaway_owner_text(["A", "B", "C"]), cc.warning_text(["Mochi"]),
                 cc.returned_embed_text("Mochi")):
        low = text.lower()
        assert not any(w in low for w in ("neglect", "abandon", "cruel", "bad owner", "your fault", "shame"))
    assert "Mochi" in cc.runaway_owner_text(["Mochi"]) and "3 of your cats" in cc.runaway_owner_text(["A", "B", "C"])
    assert "Feed" in cc.warning_text(["Mochi"]) and "80" in cc.warning_text(["Mochi"])


# ── database statements are safe by construction ─────────────────────────
def test_sql_never_touches_cats_for_sale_and_spends_atomically():
    src = (ROOT / "modules/cat_care.py").read_text()
    assert src.count("_NOT_FOR_SALE") >= 3                       # hunger, runaway, and the definition
    assert "coins >= $3" in src and "FOR UPDATE" in src          # one statement, cat row locked
    assert "SKIP LOCKED" in src                                  # two servers can't post the same runaway


def test_migration_is_additive_and_loaded():
    sql = (ROOT / "database/migrations/047_cat_care.sql").read_text()
    assert "DROP" not in sql.upper() and "DELETE" not in sql.upper()
    assert "ADD COLUMN IF NOT EXISTS hungry_since" in sql and "CREATE TABLE IF NOT EXISTS cat_runaways" in sql
    assert "047_cat_care.sql" in (ROOT / "database.py").read_text()


def test_adopting_a_cat_that_came_back_keeps_its_identity_and_gives_no_reward():
    src = (ROOT / "modules/cat_adopt.py").read_text()
    assert "UPDATE cat_runaways SET claimed_at" in src and "came_back" in src
    assert "if not came_back" in src


# ── My cats screen ───────────────────────────────────────────────────────
def _view(monkeypatch, cats):
    monkeypatch.setattr(cw, "safe_wallet", AsyncMock(return_value=cw.Wallet(500, 0)))
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), cats)
    run(view.refresh_wallet()); view._build()
    return view


def _care_buttons(view):
    return {c.label: c for c in view.children if isinstance(c, discord.ui.Button) and c.row == 4}


def test_care_buttons_sit_on_the_last_row_and_feed_only_works_when_hungry(monkeypatch):
    b = _care_buttons(_view(monkeypatch, [mkcat(1), mkcat(2)]))
    assert set(b) == {"Feed · 80", "Water · 30", "Bed · 120"}
    assert b["Feed · 80"].disabled and not b["Water · 30"].disabled and not b["Bed · 120"].disabled
    hungry = dataclasses.replace(mkcat(1), hungry_since=NOW)
    b = _care_buttons(_view(monkeypatch, [hungry, mkcat(2)]))
    assert not b["Feed · 80"].disabled and b["Feed · 80"].style == discord.ButtonStyle.danger


def test_a_cat_card_shows_its_care_status(monkeypatch):
    view = _view(monkeypatch, [dataclasses.replace(mkcat(1), hungry_since=NOW)])
    assert "Hungry" in run(view.embed()).description


def test_feeding_spends_coins_and_refreshes(monkeypatch):
    calls = []

    async def feed(cat_id, owner, clone):
        calls.append((cat_id, owner, clone)); return cc.CareResult("ok", coins=420, cost=80)
    monkeypatch.setattr(cc, "feed", feed)
    hungry = dataclasses.replace(mkcat(1), hungry_since=NOW)
    view = _view(monkeypatch, [hungry, mkcat(2)])
    fed = mkcat(1)
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[fed, mkcat(2)]))
    i = FI(uid=10)
    run(view._feed(i))
    assert calls == [(1, 10, None)]
    edit = i.edits[0]
    assert "ate happily" in edit["content"] and "420" in edit["content"]
    assert "Happy and fed" in edit["embed"].description


@pytest.mark.parametrize("status,needle", [("poor", "You need 30 coins and have 5"), ("gone", "isn't yours"),
                                           ("already", "already set")])
def test_other_outcomes_say_so(monkeypatch, status, needle):
    monkeypatch.setattr(cc, "water", AsyncMock(return_value=cc.CareResult(status, coins=5, cost=30,
                                                                         until=NOW + timedelta(hours=20))))
    view = _view(monkeypatch, [mkcat(1), mkcat(2)])
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1), mkcat(2)]))
    i = FI(uid=10)
    run(view._water(i))
    assert needle in i.edits[0]["content"]


def test_if_the_last_cat_wandered_off_the_screen_closes_politely(monkeypatch):
    monkeypatch.setattr(cc, "water", AsyncMock(return_value=cc.CareResult("gone")))
    view = _view(monkeypatch, [mkcat(1)])
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[]))
    i = FI(uid=10)
    run(view._water(i))
    assert i.edits[0]["view"] is None and "don't have any cats" in i.edits[0]["content"]


def test_buying_bed_calls_rent_bed(monkeypatch):
    rent = AsyncMock(return_value=cc.CareResult("ok", coins=380, cost=120, until=NOW + timedelta(hours=24)))
    monkeypatch.setattr(cc, "rent_bed", rent)
    view = _view(monkeypatch, [mkcat(1), mkcat(2)])
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1), mkcat(2)]))
    i = FI(uid=10)
    run(view._bed(i))
    assert rent.await_args.args == (1, 10, None) and "cosy bed" in i.edits[0]["content"]


# ── the care loop and runaways ───────────────────────────────────────────
def _cog(monkeypatch, **steps):
    bot = SimpleNamespace(clone_id=None, get_guild=lambda gid: SimpleNamespace(name="Srv"), wait_until_ready=AsyncMock(),
                          fetch_user=AsyncMock(return_value=SimpleNamespace(bot=False, send=AsyncMock())))
    monkeypatch.setattr(cc, "make_hungry", steps.get("hungry", AsyncMock(return_value=2)))
    monkeypatch.setattr(cc, "due_warnings", steps.get("warn", AsyncMock(return_value={5: ["Mochi"]})))
    monkeypatch.setattr(cc, "make_runaways", steps.get("left", AsyncMock(return_value=[])))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    return cat.CatCog.__new__(cat.CatCog), bot


def test_care_tick_runs_every_step_even_when_one_fails(monkeypatch):
    c, bot = _cog(monkeypatch, hungry=AsyncMock(side_effect=RuntimeError("db")))
    c.bot = bot
    out = run(c.care_tick())
    assert out == {"hungry": 0, "warned": 0, "left": 0, "faded": 0}


def test_care_tick_never_dms_about_hunger(monkeypatch):
    warn = AsyncMock(return_value={5: ["Mochi"]})
    c, bot = _cog(monkeypatch, warn=warn)
    c.bot = bot
    out = run(c.care_tick())
    assert out["warned"] == 0 and warn.await_count == 0
    bot.fetch_user.return_value.send.assert_not_awaited()


def test_runaways_are_logged_and_owners_told_kindly(monkeypatch):
    gone = [dict(owner_id=5, pool_id=9, cat_name="Mochi", rarity="rare", trait="Sleepy", guild_id=77)]
    c, bot = _cog(monkeypatch, left=AsyncMock(return_value=gone), warn=AsyncMock(return_value={}))
    c.bot = bot
    rec = []
    monkeypatch.setattr(cat.cat_log, "record", lambda b, kind, **f: rec.append((kind, f)))
    out = run(c.care_tick())
    assert out["left"] == 1 and rec[0][0] == "runaway" and rec[0][1]["cat_name"] == "Mochi"
    sent = bot.fetch_user.return_value.send.await_args.args[0]
    assert "Mochi" in sent and "wandered off" in sent


def test_a_runaway_cat_comes_back_in_a_spawn_with_a_kind_message(monkeypatch):
    rw = cc.Runaway(9, "Mochi", "rare", "Sleepy", 77)
    monkeypatch.setattr(cc, "take_runaway", AsyncMock(return_value=rw))
    monkeypatch.setattr(cat.cat_perms, "missing_names", lambda p: [])
    monkeypatch.setattr(ca, "create_spawn", AsyncMock(return_value=31), raising=False)
    from modules import cat_adopt
    monkeypatch.setattr(cat_adopt, "create_spawn", AsyncMock(return_value=31))
    monkeypatch.setattr(cat_adopt, "set_spawn_message", AsyncMock())
    rec = []
    monkeypatch.setattr(cat.cat_log, "record", lambda b, kind, **f: rec.append(kind))
    sent = {}

    async def send(**k):
        sent.update(k); return SimpleNamespace(id=1)
    ch = SimpleNamespace(send=send, permissions_for=lambda m: None)
    monkeypatch.setattr(discord, "TextChannel", type(ch), raising=False)
    guild = SimpleNamespace(id=77, name="Srv", me=object(), get_channel=lambda cid: ch)
    assert run(cat.post_cat(SimpleNamespace(clone_id=None), guild, 5)) is True
    e = sent["embed"]
    assert "Mochi" in e.description and "Adopt me!" in e.description and e.title == "🐾 A familiar face!"
    assert rec == ["returned"]


def test_a_runaway_that_cannot_be_posted_is_requeued(monkeypatch):
    rw = cc.Runaway(9, "Mochi", "rare", "Sleepy", 77)
    monkeypatch.setattr(cc, "take_runaway", AsyncMock(return_value=rw))
    undo = AsyncMock()
    monkeypatch.setattr(cc, "undo_return", undo)
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value=None))      # picture is gone
    monkeypatch.setattr(cat.cat_perms, "missing_names", lambda p: [])
    monkeypatch.setattr(cat, "fetch_cat_url", AsyncMock(return_value=None))
    ch = SimpleNamespace(permissions_for=lambda m: None)
    monkeypatch.setattr(discord, "TextChannel", type(ch), raising=False)
    guild = SimpleNamespace(id=77, name="Srv", me=object(), get_channel=lambda cid: ch)
    assert run(cat.post_cat(SimpleNamespace(clone_id=None), guild, 5)) is False
    undo.assert_awaited_once_with(9)
