import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules import payment_watch as pw


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


def facts(**kw):
    base = dict(now=2_000_000_000, base_url="https://api.example", secret_set=True, token_set=True, token_status="ok",
                api_ok=True, api_detail="HTTP 200", pending=[], matches={}, sales_checked=True, last_ping_at=1_999_999_000)
    base.update(kw)
    return base


def keys(issues):
    return {i.key for i in issues}


# ── matching a Gumroad sale to a pending checkout ─────────────────────────

def test_a_sale_carrying_the_reference_matches_whatever_field_it_is_in():
    pending = [{"paystack_reference": "REF123"}, {"paystack_reference": "OTHER"}]
    sales = [{"id": "s1", "product_name": "Premium", "price": 500, "url_params": {"reference": "REF123"}}]
    assert set(pw.find_paid_matches(pending, sales)) == {"REF123"}
    sales = [{"id": "s1", "custom_fields": [{"reference": "REF123"}]}]
    assert set(pw.find_paid_matches(pending, sales)) == {"REF123"}


def test_refunded_or_disputed_sales_never_match():
    pending = [{"paystack_reference": "REF123"}]
    for flag in ("refunded", "disputed", "chargedback"):
        assert pw.find_paid_matches(pending, [{"x": "REF123", flag: True}]) == {}


def test_no_sales_or_no_reference_matches_nothing():
    assert pw.find_paid_matches([{"paystack_reference": "A"}], []) == {}
    assert pw.find_paid_matches([{"paystack_reference": None}], [{"x": "A"}]) == {}


# ── classify ──────────────────────────────────────────────────────────────

def test_everything_fine_is_green():
    assert pw.classify(facts()) == []
    assert "healthy" in pw.render([])


def test_missing_secret_and_dead_api_are_red():
    ks = keys(pw.classify(facts(secret_set=False, api_ok=False, api_detail="TimeoutError")))
    assert {"no_secret", "api_down"} <= ks


def test_paid_but_stuck_is_red_and_blames_the_ping_url_when_no_ping_arrived():
    sale = {"id": "S9", "product_name": "Premium", "price": 500}
    issues = pw.classify(facts(pending=[{"paystack_reference": "REF1"}], matches={"REF1": sale}, last_ping_at=None))
    ks = keys(issues)
    assert "paid:REF1" in ks and "ping_url" in ks
    text = pw.render(issues)
    assert "REF1" in text and "$5.00" in text and "Ping URL" in text and "/api/gumroad_webhook?secret=" in text
    assert all(i.level == "red" for i in issues if i.key in ("paid:REF1", "ping_url"))


def test_recent_pings_mean_no_ping_url_blame():
    sale = {"id": "S9", "product_name": "P", "price": 100}
    ks = keys(pw.classify(facts(pending=[{"paystack_reference": "R"}], matches={"R": sale}, last_ping_at=1_999_999_990)))
    assert "paid:R" in ks and "ping_url" not in ks


def test_unmatched_pending_checkouts_are_only_yellow():
    issues = pw.classify(facts(pending=[{"paystack_reference": "A"}, {"paystack_reference": "B"}]))
    assert [(i.level, i.key) for i in issues] == [("yellow", "pending")]


def test_unchecked_sales_say_so():
    issues = pw.classify(facts(pending=[{"paystack_reference": "A"}], sales_checked=False))
    assert "couldn't cross-check" in issues[0].text


def test_token_problems():
    assert keys(pw.classify(facts(token_set=False, token_status=None))) == {"no_token"}
    assert keys(pw.classify(facts(token_status="rejected"))) == {"token_bad"}
    assert keys(pw.classify(facts(token_status="no_scope"))) == {"token_scope"}
    assert keys(pw.classify(facts(token_status="unreachable"))) == {"gumroad_unreachable"}


def test_render_is_capped_for_discord():
    many = {f"R{i}": {"id": f"s{i}", "product_name": "x" * 40, "price": 100} for i in range(60)}
    assert len(pw.render(pw.classify(facts(pending=[{"paystack_reference": k} for k in many], matches=many)))) <= 1900


def test_render_never_contains_secrets():
    text = pw.render(pw.classify(facts(secret_set=False)))
    assert "whs_" not in text and "<GUMROAD_WEBHOOK_SECRET>" not in text.replace("`GUMROAD_WEBHOOK_SECRET`", "")


# ── when to DM ────────────────────────────────────────────────────────────

def test_decide_dm():
    assert pw.decide_dm(None, "ok", False, 1000)                                   # first report ever
    assert not pw.decide_dm({"sig": "ok", "at": 0}, "ok", False, 10 ** 9)          # steady green: silent
    assert pw.decide_dm({"sig": "ok", "at": 0}, "api_down", True, 10)              # turned red
    assert pw.decide_dm({"sig": "api_down", "at": 0}, "ok", False, 10)             # recovered
    assert not pw.decide_dm({"sig": "api_down", "at": 1000}, "api_down", True, 1000 + 60)
    assert pw.decide_dm({"sig": "api_down", "at": 1000}, "api_down", True, 1000 + pw.REMIND_SECONDS)


# ── a full scheduled run (database + network mocked) ──────────────────────

class _Db:
    def __init__(self, state=None):
        self.store = {} if state is None else {pw.STATE_KEY: json.dumps(state)}
        self.get_global_setting = AsyncMock(side_effect=lambda k: self.store.get(k))
        self.set_global_setting = AsyncMock(side_effect=lambda k, v: self.store.__setitem__(k, v))


def _patch(monkeypatch, db, f):
    import database
    monkeypatch.setattr(database, "db", db)
    monkeypatch.setattr(pw, "gather_facts", AsyncMock(return_value=f))


def test_first_run_reports_even_when_green_then_goes_quiet(monkeypatch):
    db = _Db()
    _patch(monkeypatch, db, facts())
    _, text, send = run(pw.run_check())
    assert send and "healthy" in text
    assert json.loads(db.store[pw.STATE_KEY])["sig"] == "ok"
    _, _, send = run(pw.run_check())
    assert not send


def test_turning_red_then_green_sends_both(monkeypatch):
    db = _Db({"sig": "ok", "at": 0, "red": False})
    _patch(monkeypatch, db, facts(api_ok=False, api_detail="TimeoutError"))
    _, text, send = run(pw.run_check())
    assert send and text.startswith("🔴")
    _patch(monkeypatch, db, facts())
    _, text, send = run(pw.run_check())
    assert send and "healthy again" in text


def test_manual_check_never_sends_or_saves(monkeypatch):
    db = _Db()
    _patch(monkeypatch, db, facts(secret_set=False))
    issues, text, send = run(pw.run_check(force=True))
    assert not send and text.startswith("🔴") and pw.STATE_KEY not in db.store


def test_record_ping_survives_a_database_failure(monkeypatch):
    import database
    monkeypatch.setattr(database, "db", SimpleNamespace(set_global_setting=AsyncMock(side_effect=RuntimeError("down"))))
    run(pw.record_ping())    # must not raise


# ── where a setting lives (the DM must not claim "not set" for a database-saved key) ─────────────────

def test_key_source_prefers_the_database_then_railway_then_none():
    assert pw.key_source("K", {"K": "v"}, {"K": "x"}) == "database"
    assert pw.key_source("K", {}, {"K": "x"}) == "railway"
    assert pw.key_source("K", {"K": None}, {}) is None
    assert pw.key_source("K", None, None) is None


def test_gather_facts_counts_a_database_saved_key_as_set(monkeypatch):
    import os
    from modules import owner_env as oe
    monkeypatch.delenv("GUMROAD_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GUMROAD_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(oe, "refresh_live", AsyncMock())
    monkeypatch.setattr(oe, "load_all", AsyncMock(return_value={"GUMROAD_WEBHOOK_SECRET": "s3cret"}))
    monkeypatch.setattr(pw.config, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(pw, "_pending_rows", AsyncMock(return_value=[]))
    import database
    monkeypatch.setattr(database, "db", SimpleNamespace(get_global_setting=AsyncMock(return_value=None)))
    f = run(pw.gather_facts())
    assert f["secret_set"] is True and f["secret_source"] == "database"
    assert f["token_set"] is False and f["token_source"] is None
    assert "no_secret" not in {i.key for i in pw.classify(f)}
