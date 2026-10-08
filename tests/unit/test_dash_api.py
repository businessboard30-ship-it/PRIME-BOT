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
