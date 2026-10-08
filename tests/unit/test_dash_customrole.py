"""Web dashboard: Custom role module (the same kill switch /customrole disable_feature writes)."""
import asyncio

import pytest

from database import Database
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

CH = {"text": {"100"}, "category": set(), "voice": set()}


@pytest.fixture
def store(env, monkeypatch):
    """Fake the two real db methods the module maps to, backed by a dict, so the real
    get_/set_custom_role_config wrappers run on top of them."""
    fake, _ = env
    state = {}
    async def is_disabled(self, gid, clone_id=None): return state.get((gid, clone_id), False)
    async def set_disabled(self, gid, disabled, clone_id=None): state[(gid, clone_id)] = bool(disabled)
    monkeypatch.setattr(Database, "is_custom_role_feature_disabled", is_disabled)
    monkeypatch.setattr(Database, "set_custom_role_feature_disabled", set_disabled)
    real = Database.__new__(Database)
    async def get(gid, clone_id=None): return await Database.get_custom_role_config(real, gid, clone_id)
    async def put(gid, clone_id=None, **f): return await Database.set_custom_role_config(real, gid, clone_id, **f)
    monkeypatch.setattr(type(fake), "get_custom_role_config", lambda self, g, c=None: get(g, c), raising=False)
    monkeypatch.setattr(type(fake), "set_custom_role_config", lambda self, g, c=None, **f: put(g, c, **f), raising=False)
    return state


def test_module_is_registered_and_valid():
    m = S.BY_ID["customrole"]
    assert m["category"] == "Community" and [f["key"] for f in m["fields"]] == ["enabled"]
    ok, err = S.validate_values(m, {"enabled": False}, CH, set(), False)[:2]
    assert ok == {"enabled": False} and not err
    assert S.validate_values(m, {"enabled": True, "evil": 1}, CH, set(), False)[1]       # unknown key rejected


def test_db_wrappers_invert_the_disabled_flag(store):
    real = Database.__new__(Database)
    assert asyncio.run(Database.get_custom_role_config(real, 1, None)) == {"enabled": True}      # default: perk on
    asyncio.run(Database.set_custom_role_config(real, 1, None, enabled=False))
    assert store[(1, None)] is True                                                              # stored as disabled
    assert asyncio.run(Database.get_custom_role_config(real, 1, None)) == {"enabled": False}
    asyncio.run(Database.set_custom_role_config(real, 1, None))                                  # no field: unchanged
    assert asyncio.run(Database.get_custom_role_config(real, 1, None)) == {"enabled": False}
    asyncio.run(Database.set_custom_role_config(real, 1, 7, enabled=True))                       # clones are separate
    assert store[(1, 7)] is False and store[(1, None)] is True


def test_save_through_the_api_matches_the_discord_command(store):
    body = {"action": "save", "guild_id": str(GUILD), "module": "customrole", "values": {"enabled": False}}
    st, p, _ = call("POST", body=body)
    assert st == 200 and p["values"]["enabled"] is False
    assert store[(GUILD, None)] is True                      # exactly what /customrole disable_feature:True writes
    st, p, _ = call("GET", {"action": "config", "guild_id": str(GUILD), "module": "customrole"})
    assert st == 200 and p["values"] == {"enabled": False}


def test_reset_turns_the_perk_back_on(store):
    call("POST", body={"action": "save", "guild_id": str(GUILD), "module": "customrole", "values": {"enabled": False}})
    st, p, _ = call("POST", body={"action": "reset", "guild_id": str(GUILD), "module": "customrole"})
    assert st == 200 and p["values"]["enabled"] is True and store[(GUILD, None)] is False


def test_permissions(store):
    body = {"action": "save", "guild_id": str(GUILD), "module": "customrole", "values": {"enabled": False}}
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", token="plain", body=body)[0] == 401
    assert call("POST", body=dict(body, guild_id="999"))[0] == 403          # not a server they manage
    assert (GUILD, None) not in store
