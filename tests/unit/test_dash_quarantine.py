import pytest
from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

G = 1
EVERYONE = {"id": "1", "position": 0, "managed": False, "permissions": "0"}


def role(i, pos, perms=0, managed=False):
    return {"id": str(i), "position": pos, "managed": managed, "permissions": str(perms)}


def prob(rid, roles, bot):
    return S.quarantine_role_problem(G, rid, [EVERYONE] + roles, bot)


def test_ok_when_bot_role_above_with_manage_roles():
    assert prob(20, [role(10, 5, S.MANAGE_ROLES), role(20, 2)], [10]) is None


def test_admin_counts_as_manage_roles():
    assert prob(20, [role(10, 5, S.ADMINISTRATOR), role(20, 2)], [10]) is None


def test_problems():
    assert "deleted" in prob(99, [role(10, 5, S.MANAGE_ROLES)], [10])
    assert "Manage Roles" in prob(20, [role(10, 5, 0), role(20, 2)], [10])
    assert "can't be handed out" in prob(20, [role(10, 5, S.MANAGE_ROLES), role(20, 2, managed=True)], [10])
    assert "can't be handed out" in S.quarantine_role_problem(G, 1, [dict(EVERYONE, permissions=str(S.MANAGE_ROLES))], [])
    assert "above" in prob(20, [role(10, 2, S.MANAGE_ROLES), role(20, 5)], [10])      # role higher than bot
    assert "above" in prob(5, [role(10, 5, S.MANAGE_ROLES), role(5, 5)], [10])        # same position: lower id is the higher role
    assert "Manage Roles" in prob(20, [role(10, 5, S.MANAGE_ROLES), role(20, 2)], [])  # bot holds no role: nothing grants it


def test_same_position_higher_id_is_below_like_discord_py():
    assert prob(50, [role(40, 5, S.MANAGE_ROLES), role(50, 5)], [40]) is None


class QDB:
    def __init__(self, base):
        self.base, self.role, self.audit = base, None, []
    def __getattr__(self, n):
        return getattr(self.base, n)
    async def get_quarantine_role(self, gid, clone_id=None):
        return self.role
    async def set_quarantine_role(self, gid, clone_id, rid):
        self.role = rid
    async def dash_audit_add(self, *a, **k):
        self.audit.append(a)


@pytest.fixture
def qdb(env, monkeypatch):
    fake, _ = env
    d = QDB(fake)
    monkeypatch.setattr(dash, "db", d)
    state = {"problem": None}
    async def fake_problem(gid, rid):
        return state["problem"]
    monkeypatch.setattr(dash, "_quarantine_problem", fake_problem)
    d.state = state
    return d


def test_set_rejects_foreign_role_and_hierarchy_problem(qdb):
    body = {"action": "quarantine_role_set", "guild_id": str(GUILD)}
    assert call("POST", body={**body, "role_id": "123456"})[0] == 422        # not in this server
    assert call("POST", body={**body, "role_id": ""})[0] == 422
    assert qdb.role is None
    role_id = next(iter(dash._id_sets(__import__("asyncio").run(dash._meta(GUILD)))[1]))
    qdb.state["problem"] = "Move my role above the quarantine role in Server Settings > Roles."
    st, p, _ = call("POST", body={**body, "role_id": role_id})
    assert st == 422 and "Move my role" in p["message"] and qdb.role is None
    qdb.state["problem"] = None
    assert call("POST", body={**body, "role_id": role_id})[0] == 200 and qdb.role == int(role_id) and qdb.audit


def test_auth_enforced(qdb):
    assert call("POST", body={"action": "quarantine_role_set", "guild_id": "999", "role_id": "1"})[0] == 403
    assert call("GET", {"action": "quarantine", "guild_id": "999"})[0] == 403
    assert call("GET", {"action": "quarantine", "guild_id": str(GUILD)}, token=None)[0] == 401
