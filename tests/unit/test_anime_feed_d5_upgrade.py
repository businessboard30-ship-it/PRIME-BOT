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


# -- granting: one payment = 30 days, the same reference never twice ------------------------

class GrantConn:
    def __init__(self, db):
        self.db = db

    async def fetchval(self, sql, *args):
        if sql.startswith("SELECT"):
            return self.db["expiry"]
        assert "GREATEST(anime_plus_entitlements.expires_at, NOW())" in sql            # renewing extends from the later of now and the expiry
        assert "payment_reference IS DISTINCT FROM EXCLUDED.payment_reference" in sql   # same reference twice changes nothing
        ref = args[2]
        if ref in self.db["applied"]:
            return None
        self.db["applied"].add(ref)
        self.db["grants"].append(args)
        return FUTURE

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class GrantPool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        return GrantConn(self.db)


def test_grant_adds_the_days_once_per_reference(monkeypatch):
    _reset()
    db = {"applied": set(), "grants": [], "expiry": FUTURE}

    async def pool():
        return GrantPool(db)
    monkeypatch.setattr(plus, "_pool", pool)
    assert run(plus.grant(7, 0, 30, "gum_1")) == FUTURE
    assert run(plus.grant(7, 0, 30, "gum_1")) == FUTURE                               # replay: nothing added, still answers
    assert len(db["grants"]) == 1 and db["grants"][0][3] == 30
    run(plus.grant(7, 0, 30, "gum_2"))
    assert len(db["grants"]) == 2


def test_the_shared_payment_system_knows_the_product_in_every_mode():
    import payments_manual as pm
    import gumroad_payments as gp
    import gumroad_autocreate as ga
    import config
    assert plus.PAYMENT_TYPE in pm.UNLOCK_HANDLERS                                     # Gumroad ping, Paystack verify and admin approve all land here
    assert gp.expected_price_usd(plus.PAYMENT_TYPE) == float(config.ANIME_PLUS_FEE_USD)
    spec = ga.PRODUCTS[plus.PAYMENT_TYPE]                                              # the bot creates it, or DMs the owner this name/price once
    assert spec["kind"] == "one_time" and spec["name"] and spec["price"]() == float(config.ANIME_PLUS_FEE_USD)
    assert plus.PAYMENT_TYPE not in __import__("gumroad_payments")._GUILD_SCOPED_TYPES  # account-level: no server prompt


def test_unlock_handler_grants_thirty_days_to_the_buyer(monkeypatch):
    import payments_manual as pm
    got = []

    async def grant(user_id, clone_id, days, reference):
        got.append((user_id, clone_id, days, reference))
    monkeypatch.setattr(plus, "grant", grant)
    run(pm.UNLOCK_HANDLERS["anime_plus"]("gum_anime_plus_5_ab", 5, None, None))
    assert got == [(5, 0, 30, "gum_anime_plus_5_ab")]


# -- the card and the buttons ----------------------------------------------------------------

def test_upgrade_text_covers_each_case_and_lists_only_what_exists():
    from discord_bot.cogs import _views_anime_plus as v
    none = v.upgrade_text("none", None, "$3 USD")
    assert "$3 USD" in none and "DM alerts" in none and "Quiet hours" in none and "50" in none and "never makes a server premium" in none
    server = v.upgrade_text("server", None, "$3 USD")
    assert "This server is premium" in server and "Nothing to buy" in server
    personal = v.upgrade_text("personal", FUTURE, "$3 USD")
    assert "active until <t:" in personal


def _buttons(view):
    return [getattr(k, "item", k) for k in view.walk_children() if isinstance(k, (discord.ui.Button, discord.ui.DynamicItem))]


def test_server_premium_card_has_no_pay_button_and_free_card_does():
    from discord_bot.cogs import _views_anime_plus as v
    assert [b.label for b in _buttons(v.upgrade_view("server", None, "$3 USD"))] == []
    assert [b.label for b in _buttons(v.upgrade_view("none", None, "$3 USD"))] == ["Upgrade"]
    assert [b.label for b in _buttons(v.upgrade_view("personal", FUTURE, "$3 USD"))] == ["Add more days"]


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
    assert "payment_type == 'anime_plus'" not in (ROOT / "api/paystack_webhook.py").read_text()   # no private payment path any more


def v_price():
    from discord_bot.cogs import _views_anime_plus as v
    return v._price_text()


def test_pay_uses_the_shared_mode_aware_payment_entry_not_a_private_gateway(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    import config
    import payments_manual
    patch_state(monkeypatch)
    calls = []

    async def fake_start(interaction, **kw):
        calls.append(kw)
    monkeypatch.setattr(payments_manual, "start_dual_mode_payment", fake_start)

    async def defer(**k):
        pass
    inter = SimpleNamespace(user=SimpleNamespace(id=3), guild=SimpleNamespace(id=9), client=SimpleNamespace(clone_id=0),
                            response=SimpleNamespace(defer=defer, is_done=lambda: True), followup=SimpleNamespace(send=None))
    run(v._PayButton().callback(inter))
    assert len(calls) == 1
    k = calls[0]
    assert k["payment_type"] == "anime_plus" and k["price_usd"] == float(config.ANIME_PLUS_FEE_USD) and k["guild_id"] is None
    assert k["product_title"] and k["product_description"] and "USD" in k["amount_display_manual"]


def test_old_ive_paid_buttons_still_answer_with_the_current_status(monkeypatch):
    from discord_bot.cogs import _views_anime_plus as v
    patch_state(monkeypatch, expiry=FUTURE)
    sent = []

    async def send(text, **k):
        sent.append(text)

    async def defer(**k):
        pass
    inter = SimpleNamespace(user=SimpleNamespace(id=3), guild=None, client=SimpleNamespace(clone_id=0),
                            response=SimpleNamespace(defer=defer, is_done=lambda: True), followup=SimpleNamespace(send=send))
    run(v._VerifyButton().callback(inter))
    assert "active until" in sent[0]
