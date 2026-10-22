from utils import dash_schema as S


def v(trigger, response, existing=()):
    return S.validate_autoresponder({"trigger": trigger, "response": response}, existing)


def test_ok_and_trigger_lowercased():
    assert v("  Rules ", " see #rules ") == ({"trigger": "rules", "response": "see #rules"}, None)


def test_rejects_empty_long_and_non_text():
    for bad in (("", "x"), ("x", ""), ("   ", "x"), ("a" * 101, "x"), ("x", "a" * 1501)):
        assert v(*bad)[1], bad
    assert S.validate_autoresponder({"trigger": 1, "response": "x"})[1]
    assert S.validate_autoresponder("nope")[1]


def test_mass_mentions_refused():
    for r in ("hi @everyone", "@HERE now", "ping <@&123>"):
        assert v("t", r)[1], r
    assert not v("t", "hi <@123> and @someone")[1]


def test_duplicate_trigger_case_insensitive():
    assert v("Rules", "x", ["rules"])[1]


def test_row_view_stringifies_ids():
    assert S.autoresponder_row_view({"id": 5, "trigger": "a", "response": "b", "created_by": 9}) == {"id": "5", "trigger": "a", "response": "b", "created_by": "9"}


# ---- routes
import pytest
from api import dash
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401


class ARDB:
    def __init__(self, base):
        self.base, self.rows, self.audit, self.next = base, [], [], 1
    def __getattr__(self, n):
        return getattr(self.base, n)
    async def get_autoresponders(self, gid, clone_id=None):
        return list(self.rows)
    async def add_autoresponder(self, gid, trigger, response, by, clone_id=None):
        self.rows.append({"id": self.next, "trigger": trigger, "response": response, "created_by": by}); self.next += 1
        return self.next - 1
    async def remove_autoresponder(self, gid, rid, clone_id=None):
        n = len(self.rows); self.rows = [r for r in self.rows if r["id"] != rid]; return len(self.rows) < n
    async def dash_audit_add(self, *a, **k):
        self.audit.append(a)


@pytest.fixture
def ardb(env, monkeypatch):
    fake, _ = env
    d = ARDB(fake)
    monkeypatch.setattr(dash, "db", d)
    return d


def ar_add(**kw):
    body = {"action": "autoresponder_add", "guild_id": str(GUILD), "trigger": "Hi", "response": "hello"}
    body.update(kw)
    return call("POST", body=body)


def test_add_list_delete_roundtrip_and_audit(ardb):
    assert ar_add()[0] == 200 and ardb.rows[0]["trigger"] == "hi" and ardb.audit
    st, p, _ = call("GET", {"action": "autoresponders", "guild_id": str(GUILD)})
    assert st == 200 and p["items"][0]["response"] == "hello"
    assert call("POST", body={"action": "autoresponder_delete", "guild_id": str(GUILD), "id": "1"})[0] == 200
    assert call("POST", body={"action": "autoresponder_delete", "guild_id": str(GUILD), "id": "1"})[0] == 404
    assert call("POST", body={"action": "autoresponder_delete", "guild_id": str(GUILD), "id": "q"})[0] == 400


def test_rejections_auth_and_cap(ardb):
    assert ar_add(response="@everyone")[0] == 422
    assert ar_add()[0] == 200 and ar_add(trigger="HI")[0] == 422          # duplicate
    assert call("POST", body={"action": "autoresponder_add", "guild_id": "999"})[0] == 403
    assert call("POST", body={"action": "autoresponder_add", "guild_id": str(GUILD)}, token=None)[0] == 401
    for i in range(S.AUTORESPONDER_MAX):
        ardb.rows.append({"id": 100 + i, "trigger": f"t{i}", "response": "x", "created_by": 1})
    assert ar_add(trigger="new")[0] == 422
