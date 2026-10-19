"""Cat Life: earning, rent, walks, heists, lucky games (pure rules) and the screen texts."""
import random
from datetime import datetime, timedelta, timezone

import pytest

from discord_bot.cogs import _views_cat_life as v
from modules import cat_help, cat_life as cl, cat_wallet

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def cat(rarity="common", rank=0, hours_ago=2, hungry=False):
    return cl.EarnCat(rarity, rank, NOW - timedelta(hours=hours_ago), hungry)


def test_hourly_rate_grows_with_rarity_and_rank():
    assert cl.hourly_rate("common", 0) == 10
    assert cl.hourly_rate("legendary", 0) == 50
    assert cl.hourly_rate("common", 5) == pytest.approx(15)
    assert cl.hourly_rate("common", 99) == cl.hourly_rate("common", 5)       # rank is clamped


def test_earnings_full_when_rent_covers_everything():
    assert cl.earnings([cat(hours_ago=2)], NOW, NOW - timedelta(days=1), NOW + timedelta(days=1)) == 20


def test_earnings_reduced_without_rent():
    assert cl.earnings([cat(hours_ago=4)], NOW, None, None) == int(10 * 4 * cl.HOME_OFF_FACTOR)


def test_earnings_partly_covered_by_rent():
    since, until = NOW - timedelta(hours=2), NOW + timedelta(days=1)
    got = cl.earnings([cat(hours_ago=4)], NOW, since, until)
    assert got == int(10 * (2 + 2 * cl.HOME_OFF_FACTOR))


def test_earnings_capped_and_hungry_cats_earn_nothing():
    assert cl.earnings([cat(hours_ago=100)], NOW, NOW - timedelta(days=9), NOW + timedelta(days=9)) == 10 * cl.HOURS_CAP
    assert cl.earnings([cat(hungry=True)], NOW, NOW - timedelta(days=1), NOW + timedelta(days=1)) == 0


def test_earn_since_uses_the_later_of_pickup_and_adoption():
    old, new = NOW - timedelta(days=3), NOW - timedelta(hours=1)
    assert cl.earn_since(old, new, NOW) == new
    assert cl.earn_since(new, old, NOW) == new
    assert cl.earn_since(None, new, NOW) == new


def test_rent_grows_with_cats_and_is_capped():
    assert cl.rent_cost(1) == 250
    assert cl.rent_cost(3, 2) == (200 + 150) * 2
    assert cl.rent_cost(500) == cl.rent_cost(cl.RENT_CAT_CAP)


def test_a_group_of_cats_pays_for_the_rent():
    cats = [cat("common") for _ in range(4)]
    assert cl.daily_earning_estimate(cats) > cl.rent_cost(4)


def test_walk_payout_range_and_bonuses():
    rng = random.Random(1)
    walk = cl.WALKS["park"]
    for _ in range(300):
        coins, gems = cl.walk_payout(walk, "common", 0, rng)
        assert int(walk.fee * walk.low) - 1 <= coins <= int(walk.fee * walk.high) + 1
        assert gems == 0                                         # only the moonlight walk can find a diamond
    top = cl.walk_payout(cl.WALKS["garden"], "legendary", 5, random.Random(7))[0]
    base = cl.walk_payout(cl.WALKS["garden"], "common", 0, random.Random(7))[0]
    assert top > base


def test_walks_are_fair_on_average_for_common_cats():
    rng = random.Random(3)
    for w in cl.WALKS.values():
        avg = sum(cl.walk_payout(w, "common", 0, rng)[0] for _ in range(4000)) / 4000
        assert 0.9 * w.fee <= avg <= 1.2 * w.fee


def test_moonlight_walk_can_find_a_diamond_rarely():
    rng = random.Random(5)
    found = sum(cl.walk_payout(cl.WALKS["moon"], "common", 0, rng)[1] for _ in range(5000))
    assert 10 < found < 120


def test_heist_chance_rises_with_the_cat_but_is_capped():
    h = cl.HEISTS["bakery"]
    assert cl.heist_chance(h, "common", 0) == pytest.approx(0.70)
    assert cl.heist_chance(h, "legendary", 5) > cl.heist_chance(h, "common", 0)
    assert cl.heist_chance(h, "legendary", 5) <= cl.HEIST_MAX_SUCCESS
    assert cl.heist_chance(cl.HEISTS["bank"], "legendary", 5) == pytest.approx(0.42)


def test_heists_cost_a_little_more_than_they_pay_on_average():
    rng = random.Random(11)
    for h in cl.HEISTS.values():
        n = 20000
        total = sum(cl.heist_roll(h, "common", 0, rng)[1] for _ in range(n))
        assert total / n < h.fee * 1.05            # no free money for a plain cat
        assert 0.8 * h.fee < h.fee * h.success * h.multiplier + 1


def test_heist_failure_pays_nothing_and_success_pays_the_multiplier():
    class Always:
        def __init__(self, v): self.v = v
        def random(self): return self.v
    h = cl.HEISTS["jewel"]
    assert cl.heist_roll(h, "common", 0, Always(0.0)) == (True, int(h.fee * h.multiplier))
    assert cl.heist_roll(h, "common", 0, Always(0.999)) == (False, 0)


def test_lucky_games_have_a_house_edge():
    assert sum(w for _, w in cl.SPIN) == 100
    assert cl.spin_expected_multiplier() < 1.0
    assert cl.FLIP_WIN_CHANCE * cl.FLIP_MULTIPLIER < 1.0
    rng = random.Random(2)
    n = 50000
    flips = sum(cl.FLIP_MULTIPLIER for _ in range(n) if cl.flip_roll(rng)) / n
    spins = sum(cl.spin_roll(rng) for _ in range(n)) / n
    assert 0.85 < flips < 1.0 and 0.85 < spins < 1.02


def test_bets_and_limit_are_sane():
    assert list(cl.BETS) == sorted(cl.BETS)
    assert cl.DAILY_LOSS_LIMIT >= cl.BETS[-1]


def test_stories_mention_the_cat_and_the_coins():
    assert "Mochi" in cl.walk_story("Mochi", 150, 100)
    assert "100" in cl.walk_story("Mochi", 100, 100) or "🪙" in cl.walk_story("Mochi", 100, 100)
    assert "Mochi" in cl.heist_story("Mochi", False, 0)
    assert "1,350" in cl.heist_story("Mochi", True, 1350) or "Mochi" in cl.heist_story("Mochi", True, 1350)


# ── screens ───────────────────────────────────────────────────────────────

def ov(**kw):
    base = dict(cats=3, pending=450, estimate_per_day=720, rent_per_day=350, home_until=None, home_on=False,
                outings_out=0, outings_ready=0, heists_left=6, loss_left=20000)
    base.update(kw)
    return cl.Overview(**base)


def test_hub_text_without_cats_invites_adopting():
    text = v.hub_description(ov(cats=0), cat_wallet.Wallet(10, 0))
    assert "Adopt" in text and "Collect" not in text


def test_hub_text_warns_when_rent_is_not_paid():
    text = v.hub_description(ov(), cat_wallet.Wallet(10, 0))
    assert "rent not paid" in text and "350" in text and "450" in text


def test_hub_text_shows_rent_paid_and_waiting_cats():
    text = v.hub_description(ov(home_on=True, home_until=NOW, outings_out=1, outings_ready=2), cat_wallet.Wallet(5, 1), note="Hi!")
    assert text.startswith("Hi!") and "rent paid until" in text and "2" in text


def test_notes_for_every_rent_status():
    assert "Rent paid" in v.rent_note(cl.RentResult("ok", cost=250, until=NOW, coins=10))
    assert "picked up" in v.rent_note(cl.RentResult("ok", cost=250, until=NOW, coins=10, settled=99))
    for s in ("poor", "too_far", "no_cats"):
        assert v.rent_note(cl.RentResult(s, cost=250))


def test_notes_for_every_outing_status():
    ok_w = cl.OutingStart("ok", cost=100, coins=5, ends_at=NOW, cat_name="Mochi")
    assert "Mochi" in v.outing_start_note(ok_w, "walk", "garden")
    ok_h = cl.OutingStart("ok", cost=200, coins=5, ends_at=NOW, cat_name="Mochi", chance=0.7)
    assert "70%" in v.outing_start_note(ok_h, "heist", "bakery")
    for s in ("poor", "hungry", "busy", "gone", "daily_limit", "bad_route"):
        assert v.outing_start_note(cl.OutingStart(s, cat_name="Mochi", cost=5), "walk", "garden")


def test_claim_note_lists_stories_and_totals():
    items = [cl.Outing(1, "Mochi", "walk", "garden", 100, 150, 0, True, "Mochi had fun", NOW),
             cl.Outing(2, "Mochi", "heist", "bakery", 200, 0, 0, False, "Caught!", NOW),
             cl.Outing(3, "Mochi", "walk", "moon", 1500, 1000, 1, True, "Shiny!", NOW)]
    text = v.claim_note(items)
    assert "Mochi had fun" in text and "Caught!" in text and "1,150" in text and "💎" in text
    assert "Nobody is back" in v.claim_note([])


def test_gamble_notes():
    assert "won" in v.gamble_note(cl.GambleResult("ok", "flip", 100, 200, 2.0, 500, 19900))
    assert "nothing this time" in v.gamble_note(cl.GambleResult("ok", "spin", 100, 0, 0.0, 400, 19900))
    assert "enough luck" in v.gamble_note(cl.GambleResult("limit", "flip", 5000, loss_left=1000))
    assert v.gamble_note(cl.GambleResult("poor", "flip", 500))


def test_help_mentions_cat_life():
    assert any("Cat Life" in t for t, _ in cat_help.sections())


def test_views_build_without_a_database():
    import asyncio

    async def build():
        hub = v.HubView(ov(), "## hub")
        assert len([c for c in hub.walk_children() if hasattr(c, "label")]) == 6
        rows = [{"id": 1, "name": "Mochi", "rarity": "rare", "cat_rank": 2}]
        assert v.OutingView("walk", rows).go.disabled
        assert len(v.OutingView("heist", rows).place.options) == len(cl.HEISTS)
        assert len([c for c in v.GamesView(500).walk_children() if hasattr(c, "label") or hasattr(c, "options")]) == 4
    asyncio.run(build())


def test_the_cat_life_migration_is_run_at_startup():
    """It shipped once without being wired in, so the tables never existed (PR #274). Pin it."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    db = (root / "database.py").read_text()
    assert "051_cat_life.sql" in db
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 98
    sql = (root / "database" / "migrations" / "051_cat_life.sql").read_text()
    for table in ("cat_life_state", "cat_life_daily", "cat_outings"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
