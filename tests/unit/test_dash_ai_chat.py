"""Web dashboard: AI chat module (reply switch + per-server AI name, same helpers as the Discord side)."""
import asyncio

import pytest

import config
from database import Database
from modules import ai_features, ai_name, dthings_vote
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

CH = {"text": set(), "category": set(), "voice": set()}


@pytest.fixture
def store(env, monkeypatch):
    fake, _ = env
    st = {"reply": {}, "names": {}, "unlocked": set(), "token": "tok"}

    async def is_on(gid): return st["reply"].get(gid, True)
    async def set_on(gid, enabled): st["reply"][gid] = bool(enabled)
    async def get_name(gid): return st["names"].get(gid)
    async def set_name(gid, name, user_id=None):
        st["names"][gid] = name
        return True
    async def unlocked(self, gid): return gid in st["unlocked"]
    async def token(): return st["token"]
    monkeypatch.setattr(ai_features, "is_reply_chat_enabled", is_on)
    monkeypatch.setattr(ai_features, "set_reply_chat_enabled", set_on)
    monkeypatch.setattr(ai_name, "get_name", get_name)
    monkeypatch.setattr(ai_name, "set_name", set_name)
    monkeypatch.setattr(dthings_vote, "_token", token)
    monkeypatch.setattr(config, "BOT_PROFILE_VOTE_GATE", True, raising=False)
    monkeypatch.setattr(Database, "is_bot_profile_unlocked", unlocked)
    real = Database.__new__(Database)

    async def get(gid, c=None): return await Database.get_ai_chat_config(real, gid, c)
    async def put(gid, c=None, **f): return await Database.set_ai_chat_config(real, gid, c, **f)
    monkeypatch.setattr(type(fake), "get_ai_chat_config", lambda self, g, c=None: get(g, c), raising=False)
    monkeypatch.setattr(type(fake), "set_ai_chat_config", lambda self, g, c=None, **f: put(g, c, **f), raising=False)
    return st


def _save(values):
    return call("POST", body={"action": "save", "guild_id": str(GUILD), "module": "aichat", "values": values})


def test_module_is_registered():
    m = S.BY_ID["aichat"]
    assert [f["key"] for f in m["fields"]] == ["reply_chat", "ai_name"]
    assert "check" in m["fields"][1]                       # plain data, so the public schema stays JSON-safe
    import json; json.dumps(S.public_schema())


def test_name_rules_match_the_discord_wizard():
    m = S.BY_ID["aichat"]
    assert S.validate_values(m, {"ai_name": "Luna"}, CH, set(), False)[0] == {"ai_name": "Luna"}
    assert S.validate_values(m, {"ai_name": "  "}, CH, set(), False)[0] == {"ai_name": None}      # blank = default
    for bad in ("Discord Staff", "my girlfriend", "http://x.co", "ignore instructions", "<@123>"):
        assert S.validate_values(m, {"ai_name": bad}, CH, set(), False)[1], bad
    assert S.validate_values(m, {"ai_name": "x" * 25}, CH, set(), False)[1]


def test_defaults_and_reply_switch(store):
    st, p, _ = call("GET", {"action": "config", "guild_id": str(GUILD), "module": "aichat"})
    assert st == 200 and p["values"] == {"reply_chat": True, "ai_name": None}
    st, p, _ = _save({"reply_chat": False})
    assert st == 200 and store["reply"][GUILD] is False and p["values"]["reply_chat"] is False
    assert _save({"reply_chat": "no"})[0] == 422


def test_naming_needs_the_vote_unless_unlocked(store):
    st, p, _ = _save({"ai_name": "Luna"})
    assert st == 422 and "vote" in p["message"].lower() and GUILD not in store["names"]
    store["unlocked"].add(GUILD)
    st, p, _ = _save({"ai_name": "Luna"})
    assert st == 200 and store["names"][GUILD] == "Luna" and p["values"]["ai_name"] == "Luna"
    st, p, _ = _save({"ai_name": ""})
    assert st == 200 and store["names"][GUILD] is None


def test_gate_off_or_no_token_lets_the_name_through(store, monkeypatch):
    store["token"] = ""
    assert _save({"ai_name": "Luna"})[0] == 200
    store["token"] = "tok"
    store["names"].clear()
    monkeypatch.setattr(config, "BOT_PROFILE_VOTE_GATE", False)
    assert _save({"ai_name": "Mia"})[0] == 200


def test_reply_switch_works_without_the_vote(store):
    assert _save({"reply_chat": False})[0] == 200 and GUILD not in store["names"]


def test_permissions(store):
    body = {"action": "save", "guild_id": str(GUILD), "module": "aichat", "values": {"reply_chat": False}}
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", body=dict(body, guild_id="999"))[0] == 403
    assert _save({"evil": 1})[0] == 422


def test_reset_and_clearing_are_never_gated(store):
    store["names"][GUILD] = "Luna"
    st, p, _ = call("POST", body={"action": "reset", "guild_id": str(GUILD), "module": "aichat"})
    assert st == 200 and store["names"][GUILD] is None and store["reply"][GUILD] is True
