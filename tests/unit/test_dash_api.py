import asyncio
import pytest

import config
from api import dash


class FakeDB:
    def __init__(self, sessions):
        self.sessions, self.saved, self.premium = sessions, [], False
    async def get_login_session(self, sid, ttl_minutes=30):
        return self.sessions.get(sid)
    async def delete_login_session(self, sid):
        return bool(self.sessions.pop(sid, None))
    async def is_guild_premium_active(self, gid, clone_id=None):
        return self.premium
    async def get_welcome_config(self, gid, clone_id=None):
        return {"enabled": False, "channel_id": None, "message_template": "hi", "accent_color": "#5865F2", "background_color": "#2b2d31"}
    async def set_welcome_config(self, gid, clone_id=None, **f):
        self.saved.append((gid, clone_id, f))
    def __getattr__(self, name):                       # every other module: harmless empty config
        async def _f(*a, **k):
            return {}
        return _f


GUILD = 111
INFO = {"id": str(GUILD), "name": "G", "icon": None, "owner_id": "5", "roles": [
    {"id": str(GUILD), "permissions": "0", "position": 0, "name": "@everyone"},
    {"id": "500", "permissions": str(0x20), "position": 2, "name": "Mods"},
    {"id": "501", "permissions": "0", "position": 1, "name": "Members"}]}
CHANNELS = [{"id": "100", "name": "general", "type": 0, "position": 0}]


@pytest.fixture
def env(monkeypatch):
    sess = {"user": {"id": "6", "username": "u", "avatar_url": ""}, "kind": "dash",
            "guilds": [{"id": str(GUILD), "name": "G", "icon": None, "owner": False}]}
    fake = FakeDB({"sid": sess, "plain": {"user": {"id": "6"}, "guilds": []}})
    monkeypatch.setattr(dash, "db", fake)
    monkeypatch.setattr(dash, "_initialized", True)
    dash._cache.clear()
    members = {"6": {"roles": ["500"]}}

    async def bot_get(path):
        if path.startswith("/users/@me/guilds"):
            return [{"id": str(GUILD)}]
        if path.startswith(f"/guilds/{GUILD}/members/"):
            uid = path.rsplit("/", 1)[1]
            if uid not in members:
                raise dash.DiscordError(404)
            return members[uid]
        if path.startswith(f"/guilds/{GUILD}/channels"):
            return CHANNELS
        if path.startswith(f"/guilds/{GUILD}"):
            return dict(INFO, approximate_member_count=42)
        raise dash.DiscordError(404)
    monkeypatch.setattr(dash, "_bot_get", bot_get)
    return fake, members


def call(method, query=None, token="sid", body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    q = {k: [v] for k, v in (query or {}).items()}
    try:
        asyncio.run(dash._route(method, q, headers, body or {}))
    except dash._Reply as r:
        return r.status, r.payload, r.location
    raise AssertionError("no reply")


def test_schema_is_public(env):
    st, p, _ = call("GET", {"action": "schema"}, token=None)
    assert st == 200 and p["modules"]


def test_requires_session(env):
    assert call("GET", {"action": "me"}, token=None)[0] == 401
    assert call("GET", {"action": "me"}, token="nope")[0] == 401
    assert call("GET", {"action": "me"}, token="plain")[0] == 401      # not a dashboard session


def test_me_lists_servers(env):
    st, p, _ = call("GET", {"action": "me"})
    assert st == 200 and p["servers"][0]["bot_present"] is True


def test_guild_overview_and_authorisation(env):
    fake, members = env
    st, p, _ = call("GET", {"action": "guild", "guild_id": str(GUILD)})
    assert st == 200 and p["guild"]["members"] == 42
    assert call("GET", {"action": "guild", "guild_id": "999"})[0] == 403        # not in their list
    members["6"] = {"roles": ["501"]}                                           # lost Manage Server
    dash._cache.clear()
    assert call("GET", {"action": "guild", "guild_id": str(GUILD)})[0] == 403
    members.pop("6")                                                            # left the server
    dash._cache.clear()
    assert call("GET", {"action": "guild", "guild_id": str(GUILD)})[0] == 403


def test_save_validates_and_writes(env):
    fake, _ = env
    body = {"action": "save", "guild_id": str(GUILD), "module": "welcome", "values": {"enabled": True, "channel_id": "100"}}
    st, p, _ = call("POST", body=body)
    assert st == 200 and fake.saved == [(GUILD, None, {"enabled": True, "channel_id": 100})]
    bad = dict(body, values={"channel_id": "424242"})
    assert call("POST", body=bad)[0] == 422 and len(fake.saved) == 1
    assert call("POST", body=dict(body, module="nope"))[0] == 404
    assert call("POST", body=dict(body, values={"evil": 1}))[0] == 422


def test_save_blocks_premium_for_free_servers(env):
    body = {"action": "save", "guild_id": str(GUILD), "module": "antiraid", "values": {"filter_age_days": 7}}
    assert call("POST", body=body)[0] == 422


def test_logout(env):
    fake, _ = env
    assert call("POST", body={"action": "logout"})[0] == 200
    assert call("GET", {"action": "me"})[0] == 401


def test_login_needs_config(env, monkeypatch):
    monkeypatch.setattr(config, "DASH_PAGES_URL", "")
    assert call("GET", {"action": "login"}, token=None)[0] == 503


# --- clone bots ----------------------------------------------------------------

def _clone_env(env, monkeypatch, status="active"):
    fake, members = env
    seen = {"tokens": []}

    async def get_clone(cid):
        return {"clone_id": cid, "status": status, "bot_username": "Cloney", "bot_token_encrypted": "enc"} if cid == 7 else None
    async def list_clones():
        return [{"clone_id": 7, "bot_username": "Cloney"}]
    fake.get_discord_clone, fake.list_active_discord_clones = get_clone, list_clones
    import types, sys
    mod = types.ModuleType("utils.crypto")
    mod.secret_manager = types.SimpleNamespace(decrypt=lambda c: "CLONE-TOKEN")
    monkeypatch.setitem(sys.modules, "utils.crypto", mod)
    dash._clone_rows.clear()
    orig = dash._bot_get

    async def spy(path):
        seen["tokens"].append(dash._token())
        return await orig(path)
    monkeypatch.setattr(dash, "_bot_get", spy)
    return seen


def test_clone_requests_use_the_clone_token_and_clone_id(env, monkeypatch):
    fake, _ = env
    seen = _clone_env(env, monkeypatch)
    st, p, _ = call("POST", body={"action": "save", "guild_id": str(GUILD), "module": "welcome", "clone_id": 7,
                                  "values": {"enabled": True}})
    assert st == 200
    assert fake.saved[-1][1] == 7
    assert set(seen["tokens"]) == {"CLONE-TOKEN"}


def test_main_bot_requests_still_use_main_token_and_none(env, monkeypatch):
    fake, _ = env
    seen = _clone_env(env, monkeypatch)
    st, _, _ = call("POST", body={"action": "save", "guild_id": str(GUILD), "module": "welcome", "values": {"enabled": True}})
    assert st == 200 and fake.saved[-1][1] is None
    assert config.DISCORD_BOT_TOKEN in seen["tokens"] or set(seen["tokens"]) == {dash._token()}
    assert "CLONE-TOKEN" not in seen["tokens"]


def test_unknown_or_inactive_clone_is_rejected(env, monkeypatch):
    _clone_env(env, monkeypatch)
    assert call("GET", {"action": "guild", "guild_id": str(GUILD), "clone_id": "99"})[0] == 404
    assert call("GET", {"action": "guild", "guild_id": str(GUILD), "clone_id": "abc"})[0] == 400
    dash._clone_rows.clear()
    _clone_env(env, monkeypatch, status="inactive")
    assert call("GET", {"action": "guild", "guild_id": str(GUILD), "clone_id": "7"})[0] == 404


def test_clone_still_requires_manage_server(env, monkeypatch):
    _, members = env
    _clone_env(env, monkeypatch)
    members["6"] = {"roles": ["501"]}                      # plain member
    assert call("GET", {"action": "guild", "guild_id": str(GUILD), "clone_id": "7"})[0] == 403


# Clone billing is covered by tests/unit/test_dash_clone_checkout.py (clones sell Premium through the dashboard now).


def test_me_lists_clones_present_in_server(env, monkeypatch):
    _clone_env(env, monkeypatch)
    st, p, _ = call("GET", {"action": "me"})
    assert st == 200 and p["servers"][0]["clones"] == [{"clone_id": 7, "name": "Cloney"}]
