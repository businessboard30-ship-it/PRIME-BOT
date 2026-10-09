"""Phase 8: usage meters, renewal reminders, delete-my-data, privacy/pricing text."""
import asyncio
import importlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from api import dash
from modules import renewal_reminders as rr
from tests.unit.test_dash_api import env, call  # noqa: F401

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
D = timedelta(days=1)


def row(product="card_plan", status="active", exp=NOW + 2 * D, cancel=False, uid="6"):
    return {"user_id": uid, "product": product, "status": status, "expires_at": exp, "cancel_at_period_end": cancel}


# ---------- renewal reminders ----------
def test_kind_and_wording():
    assert rr.kind_for(row()) == "renews"
    assert rr.kind_for(row(status="cancelled")) == "ends"
    assert rr.kind_for(row(cancel=True)) == "ends"
    assert rr.kind_for(row(status="past_due")) == "failed"
    assert "renews automatically" in rr.message(row())
    assert "ends on" in rr.message(row(status="cancelled"))
    assert "didn't go through" in rr.message(row(status="past_due"))


def test_message_has_no_payment_link():
    txt = rr.message(row(), "https://prime-bot-dash.pages.dev")
    assert "/#/me/plans" in txt and "paystack" not in txt.lower() and "gumroad" not in txt.lower()


class FakeRemDB:
    def __init__(self, rows):
        self.rows, self.claimed = rows, set()

    async def renewal_reminder_candidates(self, days, limit):
        return [r for r in self.rows if (r["user_id"], r["product"], r["expires_at"]) not in self.claimed]

    async def renewal_reminder_claim(self, uid, product, end, kind):
        k = (uid, product, end)
        if k in self.claimed:
            return False
        self.claimed.add(k)
        return True

    async def renewal_reminder_release(self, uid, product, end):
        self.claimed.discard((uid, product, end))


def test_each_period_is_reminded_once():
    db, sent = FakeRemDB([row()]), []

    async def send(uid, text):
        sent.append(uid)
    assert asyncio.run(rr.run(db, send))["sent"] == 1
    assert asyncio.run(rr.run(db, send))["sent"] == 0 and sent == [6]


def test_transient_failure_is_released_and_closed_dm_is_final():
    db = FakeRemDB([row(), row(uid="7")])

    async def send(uid, text):
        return "dm_failed:429" if uid == 6 else "dm_failed:403"
    out = asyncio.run(rr.run(db, send))
    assert out == {"sent": 0, "skipped": 1, "retry": 1}
    assert ("6", "card_plan", row()["expires_at"]) not in db.claimed      # retried next tick
    assert ("7", "card_plan", row()["expires_at"]) in db.claimed          # never retried


# ---------- member routes ----------
@pytest.fixture
def m8(env, monkeypatch):
    fake, _ = env
    fake.sessions["sid"]["iat"] = 1
    dash._owner_hits.clear()
    state = {"deleted": [], "bot": []}
    rows = [{"product": "card_plan", "status": "active", "expires_at": NOW + 30 * D, "cancel_at_period_end": False},
            {"product": "dev_monthly", "status": "active", "expires_at": NOW + 30 * D, "cancel_at_period_end": False}]

    async def entitlements_list(uid):
        return list(rows)

    async def ai_usage_get(uid, week, source):
        return {"card_plan": 3, "dev": 12}[source]

    async def dev_export_list(uid, limit=50):
        return [{"id": "a", "message_id": "111"}]

    async def member_data_delete(uid):
        state["deleted"].append(uid)
        return {"card_design": 1, "ai_keys": 2}

    async def bot_request(method, path, **kw):
        state["bot"].append((method, path))
        return state.get("status", 204)
    for n, f in (("entitlements_list", entitlements_list), ("ai_usage_get", ai_usage_get),
                 ("dev_export_list", dev_export_list), ("member_data_delete", member_data_delete)):
        monkeypatch.setattr(fake, n, f, raising=False)
    monkeypatch.setattr(dash, "_bot_request", bot_request)
    import config
    monkeypatch.setattr(config, "DEV_STORAGE_CHANNEL_ID", 999, raising=False)
    return fake, state


def test_usage_meters_show_only_owned_plans_and_use_server_counters(m8):
    st, p, _ = call("GET", {"action": "member_usage"})
    assert st == 200
    by = {m["id"]: m for m in p["meters"]}
    assert by["card_plan"]["used"] == 3 and by["card_plan"]["limit"] == 10
    assert by["dev"]["used"] == 12 and by["dev"]["limit"] == 50


def test_usage_requires_session():
    dash._owner_hits.clear()
    assert call("GET", {"action": "member_usage"}, token=None)[0] == 401


def test_delete_needs_step_up_then_confirm(m8):
    fake, state = m8
    st, p, _ = call("POST", None, body={"action": "member_data_delete", "confirm": "DELETE MY DATA"})
    assert st == 403 and p["code"] == "stepup_required" and state["deleted"] == []
    fake.sessions["sid"]["fresh_until"] = int(time.time()) + 300
    st, p, _ = call("POST", None, body={"action": "member_data_delete", "confirm": "delete my data"})
    assert st == 422 and state["deleted"] == []
    st, p, _ = call("POST", None, body={"action": "member_data_delete", "confirm": "DELETE MY DATA", "user_id": "7777"})
    assert st == 200 and p["deleted"] is True and state["deleted"] == ["6"]      # session user, not the body's
    assert ("DELETE", "/channels/999/messages/111") in state["bot"]
    assert any("payments" in k for k in p["kept"])


def test_delete_changes_nothing_when_a_stored_file_cant_be_removed(m8):
    fake, state = m8
    fake.sessions["sid"]["fresh_until"] = int(time.time()) + 300
    state["status"] = 500
    st, p, _ = call("POST", None, body={"action": "member_data_delete", "confirm": "DELETE MY DATA"})
    assert st == 502 and state["deleted"] == []


def test_member_stepup_returns_discord_url_for_any_member(m8, monkeypatch):
    fake, _ = m8
    seen = []

    async def create_login_oauth_state(state, return_to=None):
        seen.append(return_to)
    monkeypatch.setattr(fake, "create_login_oauth_state", create_login_oauth_state, raising=False)
    monkeypatch.setattr(dash, "_check_oauth_configured", lambda: None)
    monkeypatch.setattr(dash, "_authorize_url", lambda s, p: "https://discord.com/oauth2/authorize?state=" + s)
    st, p, _ = call("POST", None, body={"action": "member_stepup"})
    assert st == 200 and p["url"].startswith("https://discord.com/") and seen[0].startswith("dash_stepup_me:")


def test_delete_handler_is_not_a_dev_route_and_wipes_only_dashboard_tables():
    src = Path("database.py").read_text()
    seg = src[src.index("async def member_data_delete"):src.index("async def card_asset_blocked")]
    seg = "\n".join(l for l in seg.splitlines() if "DELETE FROM" in l)       # the statements only, not the docstring
    for kept in ("user_entitlements", "user_billing_events", "user_ai_usage", "payments", "warns", "user_levels", "economy"):
        assert kept not in seg, kept
    for gone in ("user_level_cards", "dev_connections", "dev_exports", "user_card_assets", "dash_web_users"):
        assert gone in seg


# ---------- front end + policy text ----------
def test_front_end_has_meters_privacy_page_and_no_innerhtml():
    js = Path("dashboard/assets/dash.js").read_text()
    seg = js[js.index("function mePrivacy"):js.index("function renderMe")]
    assert "innerHTML" not in seg and "innerHTML" not in js[js.index("function meOverview"):js.index("function mePlans")]
    assert "member_usage" in js and "mePrivacy" in js and "member_data_delete" in js and "DELETE MY DATA" in js
    assert 'frag.get("me") === "stepup_ok"' in js


def test_policy_pages_describe_dashboard_data_and_personal_plans(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h/d")
    legal = importlib.import_module("api.legal_pages")
    assert "Web dashboard" in legal.PRIVACY_HTML and "Privacy &amp; data" in legal.PRIVACY_HTML
    assert "Personal plans" in legal.PRICING_HTML and "Developer mode" in legal.PRICING_HTML


def test_delete_covers_messaging_without_undoing_bans_blocks_or_reports():
    src = Path("database.py").read_text()
    seg = src[src.index("async def member_data_delete"):src.index("async def card_asset_blocked")]
    assert "DELETE FROM dash_member_messages" in seg and "DELETE FROM dash_friends" in seg
    assert "msg_banned = FALSE" in seg                                   # a banned user keeps the ban
    assert "status <> 'blocked' OR blocked_by = $1" in seg               # blocks others placed against them stay
    assert "dash_member_reports" not in seg                              # safety records stay
