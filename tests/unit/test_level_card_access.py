"""45-day card, boost packs unlock the card, Premium servers need no Level Pass (a server boost does not count)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import config
import payments_manual as pm
from discord_bot.cogs import _views_level_card as vlc
from modules import level_card_design as lcd
from modules import user_subs

from tests.unit.test_level_pass import FakeDB


def run(c):
    return asyncio.run(c)


class GuildDB(FakeDB):
    """FakeDB + the guild lookups and a saved design."""

    def __init__(self, premium=False, boost=False, pass_days=None):
        super().__init__(pass_days=pass_days)
        self.premium, self.boost = premium, boost
        self.cards["7"] = '{"accent":"#FF0000"}'

    async def is_guild_premium_active(self, guild_id, clone_id=None):
        return self.premium

    async def get_active_guild_xp_boost(self, guild_id, clone_id=None):
        return {"multiplier": 2} if self.boost else None

    async def get_xp(self, *a, **k):
        return None


def days_left(fake):
    return (fake.ent["card_plan"]["expires_at"] - datetime.now(timezone.utc)).total_seconds() / 86400


# ── 45 days ──────────────────────────────────────────────────────────────────
def test_period_override_only_for_one_time_grants():
    now = datetime.now(timezone.utc)
    assert user_subs.transition(None, "card_plan", "charge", now)["expires_at"] == now + timedelta(days=31)   # monthly plan
    assert user_subs.transition(None, "card_plan", "charge", now, period_days=45)["expires_at"] == now + timedelta(days=45)


def test_pass_gives_45_days_and_stacks_on_time_left(monkeypatch):
    fake = FakeDB(pass_days=10)
    monkeypatch.setattr(pm, "db", fake)
    run(pm._unlock_level_pass("p1", 7, 555, None))
    assert 54.9 < days_left(fake) < 55.1                      # 10 days left + 45


# ── packs also unlock the card ───────────────────────────────────────────────
def test_boost_pack_grants_card_and_boosts(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(pm, "db", fake)
    run(pm.UNLOCK_HANDLERS["xp_boost_3"]("r1", 7, 555, None))
    assert 44.9 < days_left(fake) < 45.1 and len(fake.boosts) == 1


def test_boost_pack_retry_after_failed_boost_is_safe(monkeypatch):
    fake = FakeDB()
    fake.boost_fail = True
    monkeypatch.setattr(pm, "db", fake)
    try:
        run(pm.UNLOCK_HANDLERS["xp_boost_5"]("r2", 7, 555, None))
        assert False
    except RuntimeError:
        pass
    first = fake.ent["card_plan"]["expires_at"]
    fake.boost_fail = False
    run(pm.UNLOCK_HANDLERS["xp_boost_5"]("r2", 7, 555, None))
    assert fake.ent["card_plan"]["expires_at"] == first and len(fake.boosts) == 1


def test_boost_pack_without_server_changes_nothing(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(pm, "db", fake)
    try:
        run(pm.UNLOCK_HANDLERS["xp_boost_3"]("r3", 7, None, None))
        assert False
    except RuntimeError:
        pass
    assert "card_plan" not in fake.ent


# ── Premium / boosted servers ────────────────────────────────────────────────
def test_guild_access_truth_table():
    assert run(lcd.guild_card_access(GuildDB(premium=True), 1)) is True
    assert run(lcd.guild_card_access(GuildDB(boost=True), 1)) is False                 # a server boost is not Premium
    assert run(lcd.guild_card_access(GuildDB(), 1)) is False
    assert run(lcd.guild_card_access(GuildDB(premium=True), None)) is False            # no server (DM)

    class Broken(GuildDB):
        async def is_guild_premium_active(self, *a, **k):
            raise RuntimeError("db down")
    assert run(lcd.guild_card_access(Broken(), 1)) is False                            # fails closed


def test_design_shows_in_a_premium_server_only_without_a_pass():
    fake = GuildDB(premium=True)
    assert run(lcd.design_for_user(fake, 7, guild_id=1)) is not None
    assert run(lcd.design_for_user(fake, 7)) is None                                    # elsewhere: still needs the pass
    assert run(lcd.design_for_user(GuildDB(boost=True), 7, guild_id=1)) is None         # boosted but not Premium: needs a pass
    assert run(lcd.design_for_user(GuildDB(), 7, guild_id=1)) is None                   # ordinary server: no pass, no card
    assert run(lcd.card_for_user(GuildDB(premium=True), 7, guild_id=1, clone_id=None)) is not None


def test_own_pass_still_works_in_any_server():
    assert run(lcd.design_for_user(GuildDB(pass_days=5), 7, guild_id=1)) is not None


def test_designer_is_free_in_a_premium_server():
    import database
    fake = GuildDB(premium=True)
    vlc_db, vlc.db = vlc.db, fake
    try:
        d = run(vlc.draft_for(7, guild_id=1))
        assert d.has_pass and d.free_guild and d.expires is None and not d.trial_available
        e = vlc.build_embed(d)
        assert "free here" in e.description
        elsewhere = run(vlc.draft_for(7, guild_id=None))
        assert not elsewhere.has_pass and not elsewhere.free_guild
        paid = GuildDB(pass_days=9)
        vlc.db = paid
        mine = run(vlc.draft_for(7, guild_id=1))
        assert mine.has_pass and not mine.free_guild and mine.expires is not None       # own pass is shown as the pass
    finally:
        vlc.db = vlc_db


def test_callers_pass_the_server():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    lv = (root / "discord_bot/cogs/leveling.py").read_text(encoding="utf-8")
    assert "card_for_user(db, member.id, guild_id=member.guild.id, clone_id=clone_id)" in lv
    assert lv.count("clone_id=clone_id)") >= 2
