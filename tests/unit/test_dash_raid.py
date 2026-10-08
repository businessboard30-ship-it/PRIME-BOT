from datetime import datetime, timezone

import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD, INFO  # noqa: F401


def test_permission_helpers():
    roles = {GUILD: 0, 7: S.BAN_MEMBERS, 8: S.MANAGE_GUILD}
    assert S.has_permission(1, 2, [7], roles, GUILD, S.BAN_MEMBERS)
    assert not S.has_permission(1, 2, [8], roles, GUILD, S.BAN_MEMBERS)
    assert S.has_permission(2, 2, [], roles, GUILD, S.BAN_MEMBERS)               # owner
    assert S.has_permission(1, 2, [9], {**roles, 9: S.ADMINISTRATOR}, GUILD, S.KICK_MEMBERS)


def test_raid_op_rules():
    assert S.raid_op_allowed("approve", True, False, False) is None
    assert S.raid_op_allowed("ban", True, False, False)                              # manage-only can't ban
    assert S.raid_op_allowed("kick", False, True, False)
    assert S.raid_op_allowed("kick", False, False, True) is None
    assert S.raid_op_allowed("nuke", True, True, True)


def test_is_raid_row_and_view():
    row = {"user_id": 123456789012345678, "reason": "[anti-raid] account 1d old", "saved_role_ids": [1, 2],
           "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    assert S.is_raid_row(row) and not S.is_raid_row({"reason": "staff did this"})
    v = S.raid_row_view(row, {"user": {"username": "bob"}})
    assert v["user_id"] == "123456789012345678" and v["name"] == "bob" and v["reason"] == "account 1d old"
    assert v["in_server"] and v["roles_held"] == 2 and S.raid_row_view(row, None)["in_server"] is False


class RaidDB:
    def __init__(self, base):
        self.base, self.removed, self.rows = base, [], {}
    def __getattr__(self, n):
        return getattr(self.base, n)
    async def list_quarantined(self, gid, cid, limit=25):
        return list(self.rows.values())
    async def get_quarantined(self, gid, cid, uid):
        return self.rows.get(uid)
    async def remove_quarantined(self, gid, cid, uid):
        self.removed.append(uid); self.rows.pop(uid, None)
    async def get_quarantine_role(self, gid, cid):
        return 900
    async def dash_audit_add(self, *a, **k):
        pass


@pytest.fixture
def raid(env, monkeypatch):
    fake, members = env
    db = RaidDB(fake)
    monkeypatch.setattr(dash, "db", db)
    dash._last_raid_op.clear()
    db.rows[50] = {"user_id": 50, "reason": "[anti-raid] new", "saved_role_ids": [501, 777], "created_at": None}
    db.rows[51] = {"user_id": 51, "reason": "staff quarantine", "saved_role_ids": [], "created_at": None}
    members["50"] = {"user": {"id": "50", "username": "raider"}, "roles": ["900"]}
    calls = []
    orig = dash._bot_get

    async def bot_get(path):
        if path.startswith(f"/guilds/{GUILD}/members/6") and path.endswith("/6"):
            return {"roles": ["500"]}
        return await orig(path)

    async def request(method, path, reason=None, json_body=None):
        calls.append((method, path, json_body)); return 204
    monkeypatch.setattr(dash, "_bot_request", request)
    return db, members, calls


def act(op, uid="50"):
    dash._last_raid_op.clear()
    return call("POST", body={"action": "raid_action", "guild_id": str(GUILD), "user_id": uid, "op": op})


def test_review_lists_only_raid_rows(raid):
    st, p, _ = call("GET", {"action": "raid_review", "guild_id": str(GUILD)})
    assert st == 200 and [x["user_id"] for x in p["people"]] == ["50"] and p["total"] == 1


def test_approve_restores_valid_roles_and_drops_quarantine_role(raid):
    db, _, calls = raid
    st, p, _ = act("approve")
    assert st == 200 and 50 in db.removed
    method, path, body = calls[0]
    assert method == "PATCH" and "900" not in body["roles"] and "501" in body["roles"] and "777" not in body["roles"]


def test_manage_server_only_cannot_ban_or_kick(raid):
    db, _, calls = raid
    assert act("ban")[0] == 403 and act("kick")[0] == 403 and not calls and not db.removed


def test_cannot_act_on_non_raid_or_unknown_people(raid):
    assert act("approve", "51")[0] == 404          # staff-made quarantine is off limits
    assert act("approve", "999")[0] == 404
    assert act("approve", "abc")[0] == 400


def test_ban_with_permission_and_hierarchy(raid, monkeypatch):
    db, members, calls = raid
    INFO["roles"][1]["permissions"] = str(S.MANAGE_GUILD | S.BAN_MEMBERS)
    try:
        dash._cache.clear()
        members["50"]["roles"] = ["501"]               # position 1, below the mod's role (2)
        st, _, _ = act("ban")
        assert st == 200 and calls[-1][0] == "PUT" and 50 in db.removed
        db.rows[50] = {"user_id": 50, "reason": "[anti-raid] x", "saved_role_ids": [], "created_at": None}
        members["50"]["roles"] = ["500"]               # same rank as the mod
        assert act("ban")[0] == 403
    finally:
        INFO["roles"][1]["permissions"] = str(0x20)
