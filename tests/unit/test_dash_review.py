import asyncio
from datetime import datetime, timezone

import pytest

from api import dash
from utils import dash_review as R
from tests.unit.test_dash_api import GUILD, INFO, call, env  # noqa: F401  (fixture reuse)


# ── pure helpers ───────────────────────────────────────────────────────────────

def test_snowflake_created_and_row_shape():
    uid = (1700000000000 - R.DISCORD_EPOCH_MS) << 22
    assert R.snowflake_created(uid).year == 2023
    row = {"user_id": uid, "reason": "[anti-raid] new account", "saved_role_ids": [1, 2],
           "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    out = R.review_row(row, {"user": {"username": "x", "avatar": None}}, now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert out["user_id"] == str(uid) and out["reason"] == "new account" and out["saved_roles"] == 2
    assert out["in_server"] and 700 < out["account_age_days"] < 800
    assert R.review_row(row, None)["in_server"] is False


def test_has_permission_bits():
    roles = {1: 0, 10: R.BAN_MEMBERS, 11: 0, 12: R.ADMINISTRATOR}
    assert R.has_permission(5, 5, [], roles, 1, R.BAN_MEMBERS)                 # owner
    assert R.has_permission(5, 6, [10], roles, 1, R.BAN_MEMBERS)
    assert R.has_permission(5, 6, [12], roles, 1, R.BAN_MEMBERS)               # admin
    assert not R.has_permission(5, 6, [11], roles, 1, R.BAN_MEMBERS)


def test_plan_release_restores_only_assignable_roles():
    guild_roles = {1: {"position": 0}, 20: {"position": 2}, 21: {"position": 9}, 22: {"position": 1, "managed": True},
                   30: {"position": 5}}
    new, restored, lost = R.plan_release([30, 99], [20, 21, 22, 77], 99, guild_roles, bot_top_position=5, guild_id=1)
    assert new == [30, 20] and restored == 1 and lost == 3        # 21 above bot, 22 managed, 77 deleted; quarantine 99 dropped


def test_bot_top_position():
    assert R.bot_top_position([5, 6], {5: {"position": 3}, 6: {"position": 8}}) == 8
    assert R.bot_top_position([], {}) == 0


# ── routes ─────────────────────────────────────────────────────────────────────

class ReviewDB:
    def __init__(self, base):
        self.base = base
        self.rows = {777: {"user_id": 777, "reason": "[anti-raid] filter", "saved_role_ids": [501],
                           "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
                     888: {"user_id": 888, "reason": "staff did this", "saved_role_ids": [],
                           "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc)}}
        self.audit = []
    def __getattr__(self, n): return getattr(self.base, n)
    async def list_quarantined(self, gid, cid, limit=25): return list(self.rows.values())
    async def get_quarantined(self, gid, cid, uid): return self.rows.get(int(uid))
    async def remove_quarantined(self, gid, cid, uid): self.rows.pop(int(uid), None)
    async def get_quarantine_role(self, gid, cid): return 600
    async def dash_audit_add(self, *a, **k): self.audit.append(a)


@pytest.fixture
def renv(env, monkeypatch):
    fake, members = env
    rdb = ReviewDB(fake)
    monkeypatch.setattr(dash, "db", rdb)
    members["777"] = {"roles": ["600"], "user": {"username": "raider", "avatar": None}}
    calls = []
    orig = dash._bot_get

    async def bot_get(path):
        if path == "/users/@me":
            return {"id": "42"}
        if path.endswith("/members/42"):
            return {"roles": ["500"]}
        return await orig(path)

    async def bot_request(method, path, body=None, reason=""):
        calls.append((method, path, body, reason))
        return {}
    monkeypatch.setattr(dash, "_bot_get", bot_get)
    monkeypatch.setattr(dash, "_bot_request", bot_request)
    dash._last_review.clear()
    return rdb, members, calls


def test_list_shows_only_antiraid_rows(renv):
    st, p, _ = call("GET", {"action": "antiraid_review", "guild_id": str(GUILD)})
    assert st == 200 and [e["user_id"] for e in p["entries"]] == ["777"]
    assert p["entries"][0]["in_server"] is True and p["entries"][0]["name"] == "raider"


def test_release_restores_roles_and_removes_row(renv):
    rdb, members, calls = renv
    st, p, _ = call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "777", "decision": "release"})
    assert st == 200 and 777 not in rdb.rows
    method, path, body, reason = calls[-1]
    assert method == "PATCH" and path.endswith("/members/777") and body == {"roles": ["501"]}
    assert rdb.audit and "Released" in str(rdb.audit[-1])


def test_ban_needs_ban_permission(renv):
    rdb, members, calls = renv
    st, p, _ = call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "777", "decision": "ban"})
    assert st == 403 and 777 in rdb.rows and not calls            # Mods role has Manage Server only


def test_ban_with_permission_and_hierarchy_error(renv, monkeypatch):
    rdb, members, calls = renv
    INFO["roles"][1]["permissions"] = str(0x20 | R.BAN_MEMBERS)
    try:
        st, _, _ = call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "777", "decision": "ban"})
        assert st == 200 and calls[-1][0] == "PUT" and calls[-1][2] == {"delete_message_seconds": 3600} and 777 not in rdb.rows

        rdb.rows[777] = {"user_id": 777, "reason": "[anti-raid] x", "saved_role_ids": [], "created_at": None}
        async def refuse(method, path, body=None, reason=""): raise dash.DiscordError(403)
        monkeypatch.setattr(dash, "_bot_request", refuse)
        dash._last_review.clear()
        st, p, _ = call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "777", "decision": "ban"})
        assert st == 422 and "role is above" in p["message"] and 777 in rdb.rows
    finally:
        INFO["roles"][1]["permissions"] = str(0x20)


def test_cannot_act_on_manual_quarantine_or_unknown_decision(renv):
    rdb, _, calls = renv
    assert call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "888", "decision": "release"})[0] == 404
    dash._last_review.clear()
    assert call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "777", "decision": "nuke"})[0] == 422
    assert call("POST", body={"action": "antiraid_review_act", "guild_id": str(GUILD), "user_id": "abc", "decision": "release"})[0] in (400, 429)
    assert not calls


def test_review_requires_manage_server(renv):
    _, members, _ = renv
    members["6"] = {"roles": ["501"]}
    dash._cache.clear()
    assert call("GET", {"action": "antiraid_review", "guild_id": str(GUILD)})[0] == 403
