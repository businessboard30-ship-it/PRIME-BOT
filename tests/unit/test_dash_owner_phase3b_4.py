"""Owner area Phase 3 remainder (Ads, Marketplace, Bump) and Phase 4 (safety): permissions, step-up,
typed confirm, validation, fail-closed audit, output safety."""
import importlib
import time
from datetime import datetime, timezone

import pytest

from tests.unit.test_dash_api import env, call  # noqa: F401
from tests.unit.test_dash_owner import own, OWNER_ID  # noqa: F401

BIG = 1534574875274903562
NOW = datetime.now(timezone.utc)
AD = {"id": 5, "company_name": "Acme", "ad_title": "Buy now", "ad_description": "<script>alert(1)</script> hi",
      "target_url": "javascript:alert(1)", "budget_usd": 10, "status": "pending", "user_id": BIG,
      "submitted_at": NOW, "rejection_reason": None, "image_message_id": 123, "secret_internal": "NOPE"}
LISTING = {"id": "abc-123", "service_name": "svc", "service_title": "Great", "description": "d", "price_usd": 5,
           "category": "tools", "user_id": BIG, "clicks": 3, "status": "active", "created_at": NOW, "api_key": "NOPE"}


@pytest.fixture
def g(own, monkeypatch):
    ads = importlib.import_module("modules.ads_marketplace")
    calls, state = [], {"ad": dict(AD), "listing": dict(LISTING), "dm": []}
    async def get_ad(i): return dict(state["ad"]) if i == 5 else None
    async def approve(i): calls.append(("approve", i)); return True
    async def reject(i, r): calls.append(("reject", i, r)); return True
    async def deact(i): calls.append(("deactivate", i)); return True
    async def react(i): calls.append(("reactivate", i)); return True
    async def search(uid, q, st, lim): return [{"id": 5, "company_name": "Acme", "ad_title": "Buy now", "status": "pending"}]
    async def counts(): return {"pending": 1}
    async def listings(limit, offset): return [dict(state["listing"])]
    async def get_listing(i): return dict(state["listing"]) if i == "abc-123" else None
    async def rm(uid, lid): calls.append(("rm", uid, lid)); return True
    for k, v in dict(get_ad=get_ad, approve_ad=approve, reject_ad=reject, deactivate_ad=deact, reactivate_ad=react,
                     search_ads=search, count_ads_by_status=counts, get_marketplace_listings=listings,
                     get_listing=get_listing, deactivate_listing=rm).items():
        monkeypatch.setattr(ads, k, v)
    cron = importlib.import_module("api.cron_discord_owner_broadcast")
    async def dm(session, token, uid, text, *a, **k): state["dm"].append((uid, text)); return None
    monkeypatch.setattr(cron, "_dm_user", dm)
    import config
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "tok", raising=False)
    async def get_config(k): return "600"
    async def update_config(k, v): calls.append(("cfg", k, v))
    async def bl(cid): return [{"guild_id": BIG, "bump_channel_id": BIG, "receives_bumps": True, "is_premium": False}]
    async def bp(cid): return [{"id": 1, "name": "Bot", "description": "x" * 900, "guild_id": BIG, "verified_owner_id": BIG, "application_id": BIG, "created_at": NOW}]
    own.get_config, own.update_config, own.bump_list_configured_guilds, own.bump_list_pending_listings = get_config, update_config, bl, bp
    own.calls, own.state = calls, state
    own.sessions["owner"]["fresh_until"] = 0
    return own


@pytest.fixture
def sf(own, monkeypatch):
    s = importlib.import_module("modules.admin_safety")
    ss = importlib.import_module("modules.scam_shield")
    calls = []
    st = {"enabled": True, "rules": [{"id": 1, "kind": "word", "pattern": "fatowin", "note": None, "created_at": NOW},
                                     {"id": 2, "kind": "image", "pattern": "0123456789abcdef", "note": "n", "created_at": NOW}]}
    async def watch(kind, days, limit=10): calls.append(("watch", kind, days)); return [{"target_id": BIG, "hits": 4, "guilds": 2, "last_at": NOW}]
    async def rc(): return {"new": 1}
    async def lnr(limit=10): return [{"id": 3, "guild_id": BIG, "reason": "bad\x00 " + "x" * 900, "created_at": NOW}]
    async def resolve(i, status, by): calls.append(("resolve", i, status, by)); return i != 99
    async def lse(): return [{"id": 1, "kind": "playing", "text": "hi", "created_by": 1, "created_at": NOW}]
    async def gp(): return "online"
    async def add(kind, text, by): calls.append(("sadd", kind, text)); return text != "FULL"
    async def rem(i): calls.append(("srem", i)); return True
    async def setp(v, by): calls.append(("presence", v))
    async def reset(by): calls.append(("reset",))
    async def hp(limit=15): return {"configured": 2, "enabled": 1, "triggers": 9}, [{"guild_id": BIG, "clone_id": None, "enabled": True, "action": "ban", "channel_id": BIG, "triggered_count": 9, "last_triggered_at": NOW}]
    for k, v in dict(watchlist=watch, report_counts=rc, list_new_reports=lnr, resolve_report=resolve, list_status_entries=lse,
                     get_presence=gp, add_status_entry=add, remove_status_entry=rem, set_presence=setp, reset_status=reset,
                     honeypot_overview=hp).items():
        monkeypatch.setattr(s, k, v)
    async def load(force=False): pass
    async def lr(): return [dict(r) for r in st["rules"]]
    async def rh(n=10): return [{"guild_id": BIG, "user_id": BIG, "kind": "word", "matched": "m", "deleted": True, "created_at": NOW, "_total": 1}]
    async def ht(): return 1
    async def ar(kind, pattern, by, note=None): calls.append(("sa", kind, pattern, by)); return None if pattern == "dupe.com" else 9
    async def rr(i): calls.append(("sr", i)); return i == 1
    async def se(on): calls.append(("enabled", on))
    for k, v in dict(load=load, list_rules=lr, recent_hits=rh, hit_total=ht, add_rule=ar, remove_rule=rr, set_enabled=se).items():
        monkeypatch.setattr(ss, k, v)
    own.calls = calls
    own.sessions["owner"]["fresh_until"] = 0
    return own


def fresh(m):
    m.sessions["owner"]["fresh_until"] = time.time() + 300


G_READS = [{"action": "owner_ads"}, {"action": "owner_ad", "ad_id": "5"}, {"action": "owner_market"}, {"action": "owner_bump"}]
G_WRITES = [{"action": "owner_ad_approve", "ad_id": "5"},
            {"action": "owner_ad_reject", "ad_id": "5", "reason": "no"},
            {"action": "owner_ad_deactivate", "ad_id": "5"},
            {"action": "owner_ad_reactivate", "ad_id": "5"},
            {"action": "owner_listing_remove", "listing_id": "abc-123", "confirm": "REMOVE"},
            {"action": "owner_bump_cooldown", "minutes": "30"}]
S_READS = [{"action": "owner_watchlist"}, {"action": "owner_reports"}, {"action": "owner_status"},
           {"action": "owner_honeypot"}, {"action": "owner_scamshield"}]
S_WRITES = [{"action": "owner_report_resolve", "report_id": "3", "status": "reviewed"},
            {"action": "owner_status_add", "kind": "playing", "text": "hi"},
            {"action": "owner_status_remove", "id": "1"},
            {"action": "owner_presence_set", "presence": "idle"},
            {"action": "owner_status_reset", "confirm": "RESET"},
            {"action": "owner_scam_toggle", "enabled": False, "confirm": "DISABLE"},
            {"action": "owner_scam_add", "text": "free nitro"},
            {"action": "owner_scam_remove", "id": "1"}]


# ── permission matrix ──────────────────────────────────────────────────────
@pytest.mark.parametrize("query", G_READS + S_READS)
def test_read_matrix(g, sf, query):
    assert call("GET", query, token=None)[0] == 401
    assert call("GET", query, token="sid")[0] == 403          # normal guild admin
    assert call("GET", query, token="stale")[0] == 401        # owner session too old
    assert call("GET", query, token="owner")[0] == 200


@pytest.mark.parametrize("body", G_WRITES + S_WRITES)
def test_write_matrix(g, sf, body):
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", token="sid", body=body)[0] == 403
    assert call("POST", token="stale", body=body)[0] == 401
    assert g.calls == [] and g.audit_rows == []


@pytest.mark.parametrize("body", G_WRITES + S_WRITES)
def test_helper_grant_does_not_open_these_sections(g, sf, monkeypatch, body):
    g.sessions["helper"] = {"kind": "dash", "iat": int(time.time()), "guilds": [], "user": {"id": "88", "username": "h"}}
    ac = importlib.import_module("modules.admin_controls")
    monkeypatch.setitem(ac._helpers, "map", {88: {"premium", "audit", "blacklist", "logs"}})
    assert call("POST", token="helper", body=body)[0] == 403


def test_no_duplicate_actions_between_owner_modules():
    assert dash._owner_routes() and dash._owner_writes()          # raises on a duplicate name
    from api import dash as d
    assert d  # noqa


from api import dash  # noqa: E402


# ── ads ────────────────────────────────────────────────────────────────────
def test_ad_detail_is_whitelisted_and_ids_are_strings(g):
    _, p, _ = call("GET", {"action": "owner_ad", "ad_id": "5"}, token="owner")
    assert "secret_internal" not in p["ad"] and p["ad"]["user_id"] == str(BIG) and p["ad"]["has_image"] is True
    assert "image_message_id" not in p["ad"]


@pytest.mark.parametrize("bad", ["", "x", "-1", "0", "9" * 30])
def test_ad_detail_rejects_bad_id(g, bad):
    assert call("GET", {"action": "owner_ad", "ad_id": bad}, token="owner")[0] == 422


def test_ad_unknown_is_404_and_bad_filter_422(g):
    assert call("GET", {"action": "owner_ad", "ad_id": "6"}, token="owner")[0] == 404
    assert call("GET", {"action": "owner_ads", "status": "weird"}, token="owner")[0] == 422


def test_ad_approve_runs_and_is_audited(g):
    st, p, _ = call("POST", token="owner", body={"action": "owner_ad_approve", "ad_id": "5"})
    assert st == 200 and p["changed"] is True and g.calls == [("approve", 5)]
    a = g.audit_rows[-1]
    assert (a["section"], a["action"], a["target"], a["result"]) == ("ads", "owner_ad_approve", "5", "ok")


@pytest.mark.parametrize("status,action", [("approved", "owner_ad_approve"), ("pending", "owner_ad_deactivate"),
                                           ("pending", "owner_ad_reactivate"), ("rejected", "owner_ad_reject")])
def test_ad_wrong_state_is_409_and_nothing_runs(g, status, action):
    g.state["ad"]["status"] = status
    st, _, _ = call("POST", token="owner", body={"action": action, "ad_id": "5", "reason": "r"})
    assert st == 409 and g.calls == []


def test_ad_reject_needs_reason_runs_and_dms(g):
    assert call("POST", token="owner", body={"action": "owner_ad_reject", "ad_id": "5", "reason": "  "})[0] == 422
    assert g.calls == []
    st, p, _ = call("POST", token="owner", body={"action": "owner_ad_reject", "ad_id": "5", "reason": "x" * 500})
    assert st == 200 and p["dm_sent"] is True
    assert g.calls[0][0] == "reject" and len(g.calls[0][2]) == 200            # capped like Discord
    assert g.state["dm"][0][0] == BIG and "Reason" in g.state["dm"][0][1]


def test_ad_reject_dm_failure_never_undoes_rejection(g, monkeypatch):
    cron = importlib.import_module("api.cron_discord_owner_broadcast")
    async def boom(*a, **k): raise RuntimeError("discord down")
    monkeypatch.setattr(cron, "_dm_user", boom)
    st, p, _ = call("POST", token="owner", body={"action": "owner_ad_reject", "ad_id": "5", "reason": "spam"})
    assert st == 200 and p["changed"] is True and p["dm_sent"] is False


def test_ad_actions_fail_closed_when_audit_down(g):
    g.fail_audit = True
    assert call("POST", token="owner", body={"action": "owner_ad_approve", "ad_id": "5"})[0] == 503
    assert g.calls == []


# ── marketplace ────────────────────────────────────────────────────────────
def test_market_list_whitelisted(g):
    _, p, _ = call("GET", {"action": "owner_market"}, token="owner")
    assert "api_key" not in p["rows"][0] and p["rows"][0]["user_id"] == str(BIG)


def test_listing_remove_needs_stepup_then_confirm_then_uses_real_seller(g):
    body = {"action": "owner_listing_remove", "listing_id": "abc-123"}
    assert call("POST", token="owner", body={**body, "confirm": "REMOVE"})[1]["code"] == "stepup_required"
    fresh(g)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "remove"})[0] == 422
    assert g.calls == []
    assert call("POST", token="owner", body={**body, "confirm": "REMOVE"})[0] == 200
    assert g.calls == [("rm", BIG, "abc-123")]


@pytest.mark.parametrize("bad", ["", "a b", "x" * 80, "a/b", "'; drop"])
def test_listing_remove_validation(g, bad):
    fresh(g)
    assert call("POST", token="owner", body={"action": "owner_listing_remove", "listing_id": bad, "confirm": "REMOVE"})[0] == 422


def test_listing_remove_unknown_or_gone(g):
    fresh(g)
    assert call("POST", token="owner", body={"action": "owner_listing_remove", "listing_id": "nope", "confirm": "REMOVE"})[0] == 404
    g.state["listing"]["status"] = "removed"
    assert call("POST", token="owner", body={"action": "owner_listing_remove", "listing_id": "abc-123", "confirm": "REMOVE"})[0] == 404


# ── bump ───────────────────────────────────────────────────────────────────
def test_bump_read_clips_and_stringifies(g):
    _, p, _ = call("GET", {"action": "owner_bump"}, token="owner")
    assert p["cooldown_seconds"] == 600 and p["guild_total"] == 1 and p["guilds"][0]["guild_id"] == str(BIG)
    assert len(p["pending"][0]["description"]) == 500


def test_bump_cooldown_runs_and_validates(g):
    assert call("POST", token="owner", body={"action": "owner_bump_cooldown", "minutes": "30"})[0] == 200
    assert g.calls == [("cfg", "bump_cooldown_seconds", 1800)]
    for bad in ("0", "-5", "x", "", "99999999", "1.5"):
        assert call("POST", token="owner", body={"action": "owner_bump_cooldown", "minutes": bad})[0] == 422
    assert len(g.calls) == 1


# ── safety reads ───────────────────────────────────────────────────────────
def test_watchlist_validation_and_ids(g, sf):
    _, p, _ = call("GET", {"action": "owner_watchlist", "kind": "guild", "days": "30"}, token="owner")
    assert p["kind"] == "guild" and p["days"] == 30 and p["rows"][0]["target_id"] == str(BIG)
    assert call("GET", {"action": "owner_watchlist", "kind": "x"}, token="owner")[0] == 422
    _, p, _ = call("GET", {"action": "owner_watchlist", "days": "9999"}, token="owner")
    assert p["days"] == 7                                           # unknown window falls back, like Discord


def test_reports_text_is_clipped_and_control_chars_removed(sf):
    _, p, _ = call("GET", {"action": "owner_reports"}, token="owner")
    assert len(p["rows"][0]["reason"]) == 500 and "\x00" not in p["rows"][0]["reason"]


def test_scamshield_read_hides_image_hash_tail_and_hit_total_key(sf):
    _, p, _ = call("GET", {"action": "owner_scamshield"}, token="owner")
    img = [r for r in p["rules"] if r["kind"] == "image"][0]
    assert img["pattern"] == "01234567" and "_total" not in p["hits"][0] and p["counts"]["word"] == 1
    assert p["hits"][0]["user_id"] == str(BIG)


def test_honeypot_and_status_read(sf):
    assert call("GET", {"action": "owner_honeypot"}, token="owner")[1]["totals"]["triggers"] == 9
    assert call("GET", {"action": "owner_status"}, token="owner")[1]["presence"] == "online"


# ── safety writes ──────────────────────────────────────────────────────────
def test_report_resolve(sf):
    assert call("POST", token="owner", body=S_WRITES[0])[0] == 200 and sf.calls == [("resolve", 3, "reviewed", OWNER_ID)]
    for bad in ({"status": "new"}, {"status": "x"}, {"report_id": "x"}, {"report_id": "0"}):
        assert call("POST", token="owner", body={**S_WRITES[0], **bad})[0] == 422
    _, p, _ = call("POST", token="owner", body={**S_WRITES[0], "report_id": "99"})
    assert p["changed"] is False                                    # double press is a no-op


def test_status_add_remove_presence_validation(sf):
    assert call("POST", token="owner", body={"action": "owner_status_add", "kind": "playing", "text": "  hello   world "})[0] == 200
    assert sf.calls[-1] == ("sadd", "playing", "hello world")
    for bad in ({"kind": "x"}, {"text": "   "}, {"text": ""}):
        assert call("POST", token="owner", body={"action": "owner_status_add", "kind": "playing", "text": "a", **bad})[0] == 422
    _, p, _ = call("POST", token="owner", body={"action": "owner_status_add", "kind": "playing", "text": "FULL"})
    assert p["added"] is False
    assert call("POST", token="owner", body={"action": "owner_status_remove", "id": "x"})[0] == 422
    assert call("POST", token="owner", body={"action": "owner_presence_set", "presence": "invisible"})[0] == 422
    assert call("POST", token="owner", body={"action": "owner_presence_set", "presence": "dnd"})[0] == 200


def test_status_reset_needs_stepup_and_confirm(sf):
    body = {"action": "owner_status_reset"}
    assert call("POST", token="owner", body={**body, "confirm": "RESET"})[1]["code"] == "stepup_required"
    fresh(sf)
    assert call("POST", token="owner", body=body)[0] == 422
    assert call("POST", token="owner", body={**body, "confirm": "RESET"})[0] == 200 and sf.calls == [("reset",)]


def test_scam_turn_off_needs_stepup_turn_on_does_not(sf):
    off = {"action": "owner_scam_toggle", "enabled": False}
    assert call("POST", token="owner", body={**off, "confirm": "DISABLE"})[1]["code"] == "stepup_required"
    fresh(sf)
    assert call("POST", token="owner", body=off)[0] == 422
    assert call("POST", token="owner", body={**off, "confirm": "DISABLE"})[0] == 200
    sf.sessions["owner"]["fresh_until"] = 0
    assert call("POST", token="owner", body={"action": "owner_scam_toggle", "enabled": True})[0] == 200
    assert sf.calls == [("enabled", False), ("enabled", True)]
    assert call("POST", token="owner", body={"action": "owner_scam_toggle", "enabled": "yes"})[0] == 422


def test_scam_add_classifies_and_enforces_min_length(sf):
    assert call("POST", token="owner", body={"action": "owner_scam_add", "text": "FatoWin.com"})[0] == 200
    assert sf.calls[-1][1] == "domain"
    for bad in ("abc", "", "   ", "ab"):
        assert call("POST", token="owner", body={"action": "owner_scam_add", "text": bad})[0] == 422
    _, p, _ = call("POST", token="owner", body={"action": "owner_scam_add", "text": "dupe.com"})
    assert p["added"] is False


def test_scam_remove(sf):
    assert call("POST", token="owner", body={"action": "owner_scam_remove", "id": "1"})[1]["removed"] is True
    assert call("POST", token="owner", body={"action": "owner_scam_remove", "id": "2"})[1]["removed"] is False
    assert call("POST", token="owner", body={"action": "owner_scam_remove", "id": "x"})[0] == 422


def test_safety_writes_fail_closed_when_audit_down(sf):
    sf.fail_audit = True
    assert call("POST", token="owner", body={"action": "owner_scam_add", "text": "free nitro"})[0] == 503
    assert sf.calls == []
