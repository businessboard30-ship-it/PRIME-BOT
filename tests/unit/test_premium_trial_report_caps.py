"""Premium trial, 'what Premium did' report, tier-aware bulk caps and the limit-hit trial hint."""
import asyncio
import types

import config
from modules import ai_bulk, premium_perks, premium_report


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── bulk caps ────────────────────────────────────────────────────────────
def test_caps_free_match_the_module_constants_and_premium_is_larger():
    free, prem = ai_bulk.caps_for(False), ai_bulk.caps_for(True)
    assert free == {"create": ai_bulk.MAX_CREATE, "delete_channels": ai_bulk.MAX_DELETE_CHANNELS,
                    "create_roles": ai_bulk.MAX_CREATE_ROLES, "delete_roles": ai_bulk.MAX_DELETE_ROLES}
    assert all(prem[k] > free[k] for k in free)


def test_plan_creation_uses_the_cap_given():
    names = [f"c{i}" for i in range(40)]
    assert len(ai_bulk.plan_creation(names, [])["create"]) == ai_bulk.MAX_CREATE            # default = free
    plan = ai_bulk.plan_creation(names, [], cap=ai_bulk.PREMIUM_CAPS["create"])
    assert len(plan["create"]) == ai_bulk.PREMIUM_CAPS["create"]
    assert len(plan["over_cap"]) == 40 - ai_bulk.PREMIUM_CAPS["create"]


def test_plan_role_creation_cap_and_room_both_apply():
    names = [f"r{i}" for i in range(40)]
    assert len(ai_bulk.plan_role_creation(names, [], cap=30)["create"]) == 30
    assert len(ai_bulk.plan_role_creation(names, [], room=7, cap=30)["create"]) == 7   # server room still wins


def test_cap_upsell_only_for_free_servers():
    assert ai_bulk.cap_upsell(True, "create") == ""
    text = ai_bulk.cap_upsell(False, "create")
    assert str(ai_bulk.PREMIUM_CAPS["create"]) in text and "Premium" in text


# ── trial hint ───────────────────────────────────────────────────────────
def test_trial_hint_follows_the_switch(monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_TRIAL_ENABLED", True)
    monkeypatch.setattr(config, "PREMIUM_TRIAL_DAYS", 7)
    assert "7 days" in premium_perks.trial_hint() and "/premium" in premium_perks.trial_hint()
    monkeypatch.setattr(config, "PREMIUM_TRIAL_ENABLED", False)
    assert premium_perks.trial_hint() == ""


# ── report ───────────────────────────────────────────────────────────────
def test_report_lists_only_real_numbers():
    text = premium_report.render({"scams": 12, "honeypot": 1, "raiders": 0, "tickets": None, "giveaways": 0, "cats": 0})
    assert "**12** scam messages caught" in text
    assert "**1** bot/raider trapped by the honeypot" in text          # singular for 1
    assert "tickets" not in text and "giveaway" not in text
    assert "last 30 days" in text


def test_report_trial_wording_and_empty_cases():
    assert "during your trial" in premium_report.render({"scams": 3}, is_trial=True)
    quiet = premium_report.render({"scams": 0, "tickets": 0})
    assert quiet.startswith("**Nothing to report")
    assert "couldn't read" in premium_report.render({"scams": None, "tickets": None})


def test_worth_it_line_counts_only_threat_metrics():
    assert "5" in premium_report.worth_it_line({"scams": 3, "honeypot": 1, "raiders": 1, "tickets": 99})
    assert premium_report.worth_it_line({"tickets": 9}) == ""


def test_gather_never_raises_when_the_database_is_down(monkeypatch):
    import database

    async def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(database, "get_pool", boom)
    out = run(premium_report.gather(1))
    assert set(out) == {"scams", "honeypot", "raiders", "tickets", "giveaways", "cats"} and all(v is None for v in out.values())


# ── wizard status ────────────────────────────────────────────────────────
def test_status_text_for_trial_states(monkeypatch):
    from discord_bot.cogs import _views_premium_v2 as v2
    monkeypatch.setattr(config, "PREMIUM_TRIAL_DAYS", 7)
    assert "free for 7 days" in v2.status_text(v2.PremiumStatus(trial_available=True))
    assert "free Premium trial is running" in v2.status_text(
        v2.PremiumStatus(active=True, is_trial=True, expires_ts=1_800_000_000))
    assert "free for" not in v2.status_text(v2.PremiumStatus())


def test_load_status_offers_a_trial_only_to_servers_that_never_had_premium(monkeypatch):
    from discord_bot.cogs import _views_premium_v2 as v2
    monkeypatch.setattr(config, "PREMIUM_TRIAL_ENABLED", True)
    monkeypatch.setattr(config, "PREMIUM_TRIAL_DAYS", 7)

    class FakeDB:
        def __init__(self, available):
            self.available = available

        async def get_guild_premium(self, gid, clone_id=None):
            return None

        async def is_guild_premium_active(self, gid, clone_id=None):
            return False

        async def premium_trial_available(self, gid):
            return self.available

    monkeypatch.setattr(v2, "db", FakeDB(True))
    assert run(v2.load_status(1, None)).trial_available is True
    monkeypatch.setattr(v2, "db", FakeDB(False))
    assert run(v2.load_status(1, None)).trial_available is False
    monkeypatch.setattr(config, "PREMIUM_TRIAL_ENABLED", False)
    monkeypatch.setattr(v2, "db", FakeDB(True))
    assert run(v2.load_status(1, None)).trial_available is False
