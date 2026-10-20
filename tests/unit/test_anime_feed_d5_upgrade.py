"""Anime feed D5: the upgrade. has_anime_plus = server premium OR personal entitlement; fail-safe; payment completes exactly once;
gated features; persistent Upgrade/Pay/Verify buttons. No network, no database."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord

from modules import anime_plus as plus

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def _reset():
    plus._cache.clear()


def patch_state(monkeypatch, premium=False, expiry=None, boom=False):
    _reset()

    async def prem(guild_id, clone_id=None):
        return premium
    monkeypatch.setattr(plus, "server_premium", prem)

    async def exp(user_id, clone_id=None):
        if boom:
            return None
        return expiry
    monkeypatch.setattr(plus, "personal_expiry", exp)


FUTURE = datetime.now(timezone.utc) + timedelta(days=10)


# -- the one check ---------------------------------------------------------------------------

def test_server_premium_or_personal_unlocks_and_neither_does_not(monkeypatch):
    patch_state(monkeypatch, premium=True)
    assert run(plus.has_anime_plus(1, 2, 0)) and run(plus.status(1, 2, 0))[0] == "server"
    patch_state(monkeypatch, expiry=FUTURE)
    assert run(plus.has_anime_plus(1, 2, 0)) and run(plus.status(1, 2, 0))[0] == "personal"
    patch_state(monkeypatch)
    assert not run(plus.has_anime_plus(1, 2, 0)) and run(plus.status(1, 2, 0)) == ("none", None)


def test_follow_limit_is_five_free_and_fifty_with_the_upgrade(monkeypatch):
    patch_state(monkeypatch)
    assert run(plus.follow_limit(1, 2, 0)) == 5 == plus.FREE_FOLLOW_LIMIT
    patch_state(monkeypatch, expiry=FUTURE)
    assert run(plus.follow_limit(1, 2, 0)) == 50 == plus.PLUS_FOLLOW_LIMIT


def test_server_premium_wins_and_a_personal_expiry_is_still_reported(monkeypatch):
    patch_state(monkeypatch, premium=True, expiry=FUTURE)
    assert run(plus.status(1, 2, 0)) == ("server", FUTURE)


def test_a_broken_check_grants_nothing(monkeypatch):
    _reset()

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(plus, "_pool", boom)
    assert run(plus.personal_expiry(5, 0)) is None and plus._cache == {}          # fail safe, and the failure is not cached
    from modules import antiraid_pro

    async def prem_boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(antiraid_pro, "is_premium", prem_boom)
    assert run(plus.server_premium(1, 0)) is False and run(plus.server_premium(None, 0)) is False


# -- payments: exactly once ------------------------------------------------------------------

class FakeConn:
    def __init__(self, db):
        self.db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchrow(self, sql, *args):
        assert "status = 'pending'" in sql
        pay = self.db["payments"].get(args[0])
        if pay and pay["status"] == "pending":
            pay["status"] = "completed"
            return {"user_id": pay["user_id"], "clone_id": pay["clone_id"], "days": pay["days"]}
        return None

    async def fetchval(self, sql, *args):
        assert "GREATEST(anime_plus_entitlements.expires_at, NOW())" in sql         # renewing extends from the later of now and the expiry
        self.db["grants"].append(args)
        return FUTURE


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return FakeConn(self.db)


def test_webhook_and_verify_together_add_the_days_once(monkeypatch):
    _reset()
    db = {"payments": {"ref1": {"user_id": 7, "clone_id": 0, "days": 30, "status": "pending"}}, "grants": []}

    async def pool():
        return FakePool(db)
    monkeypatch.setattr(plus, "_pool", pool)
    first = run(plus.complete_payment("ref1"))
    second = run(plus.complete_payment("ref1"))                                      # the other path arrives later
    assert first == {"user_id": 7, "clone_id": 0, "expires_at": FUTURE} and second is None
    assert len(db["grants"]) == 1 and db["grants"][0][3] == 30
    assert run(plus.complete_payment("nope")) is None


# -- the card and the buttons ----------------------------------------------------------------

def test_upgrade_text_covers_each_case_and_lists_only_what_exists():
    from discord_bot.cogs import _views_anime_plus as v
    none = v.upgrade_text("none", None, "$3 USD", False)
    assert "$3 USD" in none and "DM alerts" in none and "Quiet hours" in none and "50" in none and "never makes a server premium" in none
    server = v.upgrade_text("server", None, "$3 USD", False)
    assert "This server is premium" in server and "Nothing to buy" in server
    personal = v.upgrade_text("personal", FUTURE, "$3 USD", True)
    assert "active until <t:" in personal and "I've paid" in personal


def _buttons(view):
    return [getattr(k, "item", k) for k in view.walk_children() if isinstance(k, (discord.ui.Button, discord.ui.DynamicItem))]


def test_server_premium_card_has_no_pay_button_and_free_card_does():
    from discord_bot.cogs import _views_anime_plus as v
    assert [b.label for b in _buttons(v.upgrade_view("server", None, "$3 USD", False))] == []
    assert [b.label for b in _buttons(v.upgrade_view("none", None, "$3 USD", False))] == ["Upgrade"]
    assert [b.label for b in _buttons(v.upgrade_view("personal", FUTURE, "$3 USD", True))] == ["Add more days", "I've paid"]


def test_upgrade_buttons_are_persistent_registered_and_rebuild_from_the_custom_id_alone():
    from discord_bot.cogs import _views_anime_feed as vf, _views_anime_plus as v
    from discord_bot.cogs import _views_join_dm as jd
    for cls in v.PLUS_ITEMS:
        assert cls in vf._all_dynamic_items() and cls in jd.DYNAMIC_ITEMS
        cid = cls().custom_id if cls is not v._PayButton else v._PayButton().custom_id
        assert len(cid) <= 100 and re.match(cls.__discord_ui_compiled_template__, cid)
        item = run(cls.from_custom_id(None, None, cls.__discord_ui_compiled_template__.match(cid)))
        assert item.custom_id == cid
    ids = {k.custom_id for k in vf.post_view(_P()).walk_children() if isinstance(k, discord.ui.DynamicItem)}
    assert "anime_plus" in ids


def _P():
    from modules import anime_feed as af
    return af.Preview(1, "Show", 1, 0, "s", None, "https://anilist.co/anime/1")


def test_pay_is_refused_when_the_server_is_premium(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    patch_state(monkeypatch, premium=True)
    sent = []

    async def send(*a, **k):
        sent.append((a, k))

    async def defer(**k):
        pass
    inter = SimpleNamespace(user=SimpleNamespace(id=3), guild=SimpleNamespace(id=9), client=SimpleNamespace(clone_id=0),
                            response=SimpleNamespace(defer=defer, is_done=lambda: True), followup=SimpleNamespace(send=send))
    run(v._PayButton().callback(inter))
    assert sent and "Nothing to buy" in sent[0][0][0] and sent[0][1]["ephemeral"] is True


# -- gating of the exclusive features --------------------------------------------------------

def test_quiet_hours_are_gated_but_turning_them_off_is_not(monkeypatch):
    from discord_bot.cogs import _views_anime_actions as aa
    saved, panels = [], []

    async def has_plus(*a, **k):
        return False
    monkeypatch.setattr(plus, "has_anime_plus", has_plus)

    async def save_pref(interaction, anime_id, save, note):
        panels.append((save is None, note))
        if save is not None:
            saved.append(note)
    monkeypatch.setattr(aa, "_save_pref", save_pref)

    class I:
        user = SimpleNamespace(id=1)
        guild = None
        client = SimpleNamespace(clone_id=0)

        def __init__(self, v):
            self.data = {"values": [v]}
    from modules import anime_feed_alerts as al
    on = next(k for k in al.QUIET_PRESETS if k != "off")
    run(aa._QuietSelect(42).callback(I(on)))
    assert panels[-1] == (True, aa.LOCKED_QUIET)                                       # nothing saved
    run(aa._QuietSelect(42).callback(I("off")))
    assert panels[-1][0] is False and len(saved) == 1


# -- data, config, webhook -------------------------------------------------------------------

def test_migration_is_additive_wired_and_price_is_a_config_value():
    sql = (ROOT / "database/migrations/076_anime_plus.sql").read_text()
    for needle in ("anime_plus_entitlements", "anime_plus_payments", "payment_reference TEXT UNIQUE NOT NULL", "PRIMARY KEY (user_id, clone_id)"):
        assert needle in sql
    assert "DROP " not in sql.upper() and "DELETE " not in sql.upper() and "IF NOT EXISTS" in sql
    assert "076_anime_plus.sql" in (ROOT / "database.py").read_text()
    import config
    assert config.ANIME_PLUS_FEE_USD > 0 and config.ANIME_PLUS_PRODUCT_NAME and "anime_plus" not in config.PRICE_REGISTRY      # priced in USD, not GHS
    assert v_price() == f"${config.ANIME_PLUS_FEE_USD:g} USD"
    hook = (ROOT / "api/paystack_webhook.py").read_text()
    assert "payment_type == 'anime_plus'" in hook and "anime_plus.complete_payment(reference)" in hook


def v_price():
    from discord_bot.cogs import _views_anime_plus as v
    return v._price_text()


def test_the_payment_is_checked_automatically_and_completed_once(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    import payments
    monkeypatch.setattr(v, "WATCH_EVERY", 0)
    states, completes, dms = ["pending", "pending"], [], []

    async def status(ref):
        return states.pop(0) if states else "completed"

    async def complete(ref):
        completes.append(ref)
        return {"user_id": 1}

    async def expiry(uid, clone=None):
        return FUTURE

    async def gw(clone, provider, platform=None):
        return SimpleNamespace(verify_payment=lambda ref, api_key=None: {"status": "success" if completes == [] and len(states) == 0 else "pending"}), "k"
    monkeypatch.setattr(plus, "payment_status", status)
    monkeypatch.setattr(plus, "complete_payment", complete)
    monkeypatch.setattr(plus, "personal_expiry", expiry)
    monkeypatch.setattr(payments, "resolve_gateway_for_provider", gw)

    async def send(text):
        dms.append(text)
    user = SimpleNamespace(id=1, send=send)
    run(v._watch(user, "refA", "stripe", 0))
    assert completes == ["refA"] and len(dms) == 1 and "active until" in dms[0]
