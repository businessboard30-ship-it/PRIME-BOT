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
    monkeypatch.setattr(plus, "_subscription_row", boom)
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
    assert "$3 USD" in none and "per month" in none and "cancel any time" in none.lower()
    assert "DM alerts" in none and "Quiet hours" in none and "50" in none and "never makes a server premium" in none
    server = v.upgrade_text("server", None, "$3 USD", False)
    assert "This server is premium" in server and "Nothing to buy" in server
    active = v.upgrade_text("personal", FUTURE, "$3 USD", False, "active")
    assert "subscribed" in active and "renews on <t:" in active
    cancelled = v.upgrade_text("personal", FUTURE, "$3 USD", False, "cancelled")
    assert "ends on <t:" in cancelled and "will not renew" in cancelled
    due = v.upgrade_text("personal", FUTURE, "$3 USD", False, "past_due")
    assert "renewal payment failed" in due
    legacy = v.upgrade_text("personal", FUTURE, "$3 USD", True, "legacy")
    assert "active until <t:" in legacy and "I've paid" in legacy


def _buttons(view):
    return [getattr(k, "item", k) for k in view.walk_children() if isinstance(k, (discord.ui.Button, discord.ui.DynamicItem))]


def test_only_people_who_can_still_subscribe_see_the_subscribe_button():
    from discord_bot.cogs import _views_anime_plus as v
    labels = lambda *a: [b.label for b in _buttons(v.upgrade_view(*a))]
    assert labels("server", None, "$3 USD", False) == []                                  # premium server: nothing to buy
    assert labels("none", None, "$3 USD", False) == ["Subscribe"]
    assert labels("personal", FUTURE, "$3 USD", False, "active") == []                   # a live subscription is never sold twice
    assert labels("personal", FUTURE, "$3 USD", False, "cancelled") == ["Subscribe again"]
    assert labels("personal", FUTURE, "$3 USD", False, "past_due") == ["Subscribe again"]
    assert labels("personal", FUTURE, "$3 USD", True, "legacy") == ["Subscribe again", "I've paid"]


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


def test_the_watcher_dms_once_when_the_subscription_goes_live(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    monkeypatch.setattr(v, "WATCH_EVERY", 0)
    states, dms = ["none", "none", "active"], []

    async def state(uid, clone=None, fresh=False):
        assert fresh is True                                                          # it must bypass the 30 s cache
        return states.pop(0)

    async def expiry(uid, clone=None):
        return FUTURE
    monkeypatch.setattr(plus, "personal_state", state)
    monkeypatch.setattr(plus, "personal_expiry", expiry)

    async def send(text):
        dms.append(text)
    run(v._watch(SimpleNamespace(id=1, send=send), 0))
    assert len(dms) == 1 and "renews on" in dms[0] and states == []


# -- the subscription itself -----------------------------------------------------------------

def _sub(status, delta_days):
    return {"status": status, "expires_at": datetime.now(timezone.utc) + timedelta(days=delta_days)}


def _personal(monkeypatch, sub=None, legacy=None):
    _reset()

    async def row(uid):
        return sub

    async def old(uid, clone):
        return legacy
    monkeypatch.setattr(plus, "_subscription_row", row)
    monkeypatch.setattr(plus, "_legacy_expiry", old)


def test_an_active_subscription_unlocks_and_a_cancelled_one_keeps_access_to_the_period_end(monkeypatch):
    _personal(monkeypatch, sub=_sub("active", 20))
    exp, st = run(plus._personal(1, 0))
    assert st == "active" and exp is not None
    _personal(monkeypatch, sub=_sub("cancelled", 5))
    assert run(plus._personal(1, 0))[1] == "cancelled" and run(plus.personal_expiry(1, 0)) is not None


def test_a_failed_renewal_has_a_short_grace_then_nothing(monkeypatch):
    _personal(monkeypatch, sub=_sub("past_due", -1))                                     # inside the 3-day grace
    assert run(plus._personal(1, 0))[1] == "past_due"
    _personal(monkeypatch, sub=_sub("past_due", -10))
    assert run(plus._personal(1, 0)) == (None, "none")
    _personal(monkeypatch, sub=_sub("expired", -1))
    assert run(plus._personal(1, 0)) == (None, "none")
    _personal(monkeypatch, sub=_sub("active", -1))                                       # an active row past its date never grants
    assert run(plus._personal(1, 0)) == (None, "none")


def test_days_bought_before_the_subscription_are_still_honoured_and_the_later_date_wins(monkeypatch):
    legacy = datetime.now(timezone.utc) + timedelta(days=8)
    _personal(monkeypatch, legacy=legacy)
    assert run(plus._personal(1, 0)) == (legacy, "legacy")
    sub = _sub("active", 20)
    _personal(monkeypatch, sub=sub, legacy=legacy)
    exp, st = run(plus._personal(1, 0))
    assert exp == sub["expires_at"] and st == "active"


def test_one_side_failing_never_grants_and_is_not_cached(monkeypatch):
    _reset()

    async def boom(*a, **k):
        raise RuntimeError("down")

    async def old(uid, clone):
        return None
    monkeypatch.setattr(plus, "_subscription_row", boom)
    monkeypatch.setattr(plus, "_legacy_expiry", old)
    assert run(plus._personal(1, 0)) == (None, "none") and plus._cache == {}


def test_subscribing_is_refused_when_already_subscribed(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    patch_state(monkeypatch, expiry=FUTURE)
    sent = []

    async def state(uid, clone=None, fresh=False):
        return "active"
    monkeypatch.setattr(plus, "personal_state", state)

    async def send(*a, **k):
        sent.append(a)

    async def defer(**k):
        pass
    import payments_manual
    called = []

    async def geo(*a, **k):
        called.append(k)
    monkeypatch.setattr(payments_manual, "start_geo_payment", geo)
    inter = SimpleNamespace(user=SimpleNamespace(id=3), guild=SimpleNamespace(id=9), client=SimpleNamespace(clone_id=0),
                            response=SimpleNamespace(defer=defer, is_done=lambda: True), followup=SimpleNamespace(send=send))
    run(v._PayButton().callback(inter))
    assert sent and "already subscribed" in sent[0][0] and called == []


def test_subscribe_starts_the_shared_recurring_checkout_at_the_config_price(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    import config, payments_manual
    patch_state(monkeypatch)

    async def state(uid, clone=None, fresh=False):
        return "none"
    monkeypatch.setattr(plus, "personal_state", state)
    monkeypatch.setattr(v, "_start_watch", lambda *a, **k: None)
    called = []

    async def geo(interaction, **k):
        called.append(k)
    monkeypatch.setattr(payments_manual, "start_geo_payment", geo)

    async def defer(**k):
        pass
    inter = SimpleNamespace(user=SimpleNamespace(id=3), guild=SimpleNamespace(id=9), client=SimpleNamespace(clone_id=0),
                            response=SimpleNamespace(defer=defer, is_done=lambda: True), followup=SimpleNamespace(send=None))
    run(v._PayButton().callback(inter))
    assert len(called) == 1 and called[0]["payment_type"] == "anime_plus" and called[0]["price_usd"] == config.ANIME_PLUS_FEE_USD
    assert "month" in called[0]["amount_display"]


def test_anime_plus_is_registered_everywhere_a_recurring_plan_must_be():
    import config, gumroad_autocreate as ga, paystack_autocreate as pa, payments_manual as pm
    from modules import entitlements as ent, user_subs
    assert user_subs.is_plan("anime_plus") and user_subs.price_usd("anime_plus") == float(config.ANIME_PLUS_FEE_USD) and user_subs.PLANS["anime_plus"]["period_days"] >= 30
    assert "anime_plus" in ent.PRODUCTS and "anime_plus" in pa.PLAN_SPECS and pa.PLAN_SPECS["anime_plus"]["interval"] == "monthly"
    assert ga.PRODUCTS["anime_plus"]["kind"] == "membership"
    assert "anime_plus" in config.USER_PLAN_PAYSTACK_CODES and "anime_plus" in pm.UNLOCK_HANDLERS
    assert "anime_plus" in config.GUMROAD_PRODUCT_LINKS and "anime_plus" in config.GUMROAD_PRODUCT_IDS
    # the webhook routes a paid anime_plus charge to the shared per-user plan handler, not a bespoke grant
    from modules import paystack_user_plans as pup
    assert pup._product({"metadata": {"type": "anime_plus"}}) == "anime_plus"


def test_full_subscription_lifecycle_through_the_shared_paystack_handler(monkeypatch):
    """charge -> renewal -> cancel (keeps access) -> failed renewal -> ended, each read back through the anime check."""
    import paystack_autocreate
    from modules import paystack_user_plans as psp
    from tests.unit.test_user_billing import FakeDB

    class DB(FakeDB):
        def __init__(self):
            super().__init__()
            self.pay = {"ref1": {"provider": "paystack", "user_id": 77, "payment_type": "anime_plus", "status": "pending"}}

        async def get_payment_by_reference(self, ref):
            return self.pay.get(ref)

        async def mark_payment_paid(self, ref):
            self.pay[ref]["status"] = "completed"

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(paystack_autocreate, "load_runtime", noop)
    db = DB()

    async def row(uid):
        return db.rows.get((str(uid), "anime_plus"))

    async def old(uid, clone):
        return None
    monkeypatch.setattr(plus, "_subscription_row", row)
    monkeypatch.setattr(plus, "_legacy_expiry", old)
    meta = {"type": "anime_plus", "user_id": "77"}

    def handle(event, data):
        return run(psp.handle(db, event, data, now=datetime.now(timezone.utc)))      # the real clock: access checks use it too

    def state():
        _reset()
        return run(plus._personal(77, 0))[1]

    assert state() == "none"
    assert handle("charge.success", {"status": "success", "reference": "ref1", "id": 1, "metadata": meta}) == "applied"
    assert state() == "active" and db.pay["ref1"]["status"] == "completed"
    # a renewal later: no pending row, but the entitlement exists, so it extends from the later date (never loses paid time)
    before = db.rows[("77", "anime_plus")]["expires_at"]
    assert handle("charge.success", {"status": "success", "reference": "renew1", "id": 2, "metadata": meta}) == "applied"
    assert db.rows[("77", "anime_plus")]["expires_at"] > before
    # a renewal for someone who never bought is ignored, it can never create access
    other = {"status": "success", "reference": "x", "id": 9, "metadata": {"type": "anime_plus", "user_id": "99"}}
    assert handle("charge.success", other) == "ignored" and ("99", "anime_plus") not in db.rows
    # the person cancels: access runs to the end of the period
    assert handle("subscription.not_renew", {"metadata": meta, "subscription_code": "SUB_1", "next_payment_date": "d"}) == "applied"
    assert db.rows[("77", "anime_plus")]["status"] == "cancelled"
    _reset()
    assert run(plus._personal(77, 0))[1] == "cancelled" and run(plus.personal_expiry(77, 0)) is not None
