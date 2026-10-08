import pytest

from api import dash
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

CHANNELS = [{"id": "100", "name": "general", "type": 0, "position": 0},
            {"id": "101", "name": "verify", "type": 0, "position": 1}]


@pytest.fixture
def act(env, monkeypatch):
    fake, members = env
    fake.posts, fake.saved_cfg, fake.audit = [], {}, []
    fake.cfgs = {
        "verification": {"channel_id": 101, "unverified_role_id": 501, "mode": "button"},
        "tickets": {"panel_channel_id": 100},
        "welcome": {"channel_id": 100, "use_template": True, "card_theme": "reaper", "avatar_shape": "circle",
                    "accent_color": "#5865F2", "background_color": "#2b2d31", "card_pack_unlocked": False},
    }
    fake.get_verification_config = lambda g, c=None: _co(fake.cfgs["verification"])
    fake.get_ticket_config = lambda g, c=None: _co(fake.cfgs["tickets"])
    fake.get_welcome_config = lambda g, c=None: _co(fake.cfgs["welcome"])

    async def setv(gid, clone_id=None, **f): fake.saved_cfg["verification"] = f
    async def sett(gid, clone_id=None, **f): fake.saved_cfg["tickets"] = f
    async def add(gid, uid, name, module, changes, retention_days=180): fake.audit.append((module, changes))
    fake.set_verification_config, fake.set_ticket_config, fake.dash_audit_add = setv, sett, add

    async def bot_post(path, json_body=None, form=None):
        if fake.fail:
            raise dash.DiscordError(fake.fail)
        fake.posts.append((path, json_body, form is not None))
        return {"id": "9001"}
    fake.fail = None
    monkeypatch.setattr(dash, "_bot_post", bot_post)

    async def get(path):
        if path.startswith(f"/guilds/{GUILD}/channels"):
            return CHANNELS
        return await orig(path)
    orig = dash._bot_get
    monkeypatch.setattr(dash, "_bot_get", get)
    async def av(url):
        from io import BytesIO
        from PIL import Image
        b = BytesIO(); Image.new("RGB", (64, 64), (1, 2, 3)).save(b, "PNG"); return b.getvalue()
    monkeypatch.setattr(dash, "_avatar_bytes", av)
    monkeypatch.setattr(dash, "ACTION_MIN_INTERVAL", 0)
    dash._last_action.clear(); dash._cache.clear()
    return fake


async def _co(v):
    return dict(v)


def run(action, guild=str(GUILD), token="sid"):
    return call("POST", token=token, body={"action": "bot_action", "guild_id": guild, "id": action})


def test_verify_panel_posts_persistent_button_and_records_message(act):
    st, p, _ = run("verify_panel")
    assert st == 200 and "#verify" in p["message"]
    path, body, _ = act.posts[0]
    assert path == "/channels/101/messages"
    assert body["components"][0]["components"][0]["custom_id"] == f"verify_btn:{GUILD}"
    assert body["allowed_mentions"] == {"parse": []}
    assert act.saved_cfg["verification"] == {"message_id": 9001}
    assert act.audit[0][0] == "verification"


def test_verify_panel_needs_saved_channel_and_role(act):
    act.cfgs["verification"]["unverified_role_id"] = None
    assert run("verify_panel")[0] == 422 and not act.posts
    act.cfgs["verification"].update(unverified_role_id=501, channel_id=424242)       # not in this guild
    assert run("verify_panel")[0] == 422 and not act.posts


def test_ticket_panel(act):
    st, p, _ = run("ticket_panel")
    assert st == 200
    assert act.posts[0][1]["components"][0]["components"][0]["custom_id"] == "ticket:open"
    assert act.saved_cfg["tickets"] == {"panel_channel_id": 100, "panel_message_id": 9001}
    act.cfgs["tickets"]["panel_channel_id"] = None
    assert run("ticket_panel")[0] == 422


def test_welcome_test_uploads_card_and_gates_premium_theme(act):
    st, p, _ = run("welcome_test")                      # reaper theme, no premium/pack -> falls back to wolf
    assert st == 200 and act.posts[0][0] == "/channels/100/messages" and act.posts[0][2] is True
    assert act.audit[0][1]["Test welcome sent"]["to"] == "#general"


def test_action_errors_are_explained(act):
    act.fail = 403
    st, p, _ = run("verify_panel")
    assert st == 422 and "can't post" in p["message"] and "Embed Links" in p["message"]
    act.fail = 404
    assert run("ticket_panel")[0] == 422
    act.fail = 429
    assert run("welcome_test")[0] == 429


def test_action_authorisation_and_whitelist(act):
    assert run("delete_everything")[0] == 422
    assert run("verify_panel", guild="999")[0] == 403
    assert run("verify_panel", token=None)[0] == 401
    assert not act.posts


def test_action_rate_limited(act, monkeypatch):
    monkeypatch.setattr(dash, "ACTION_MIN_INTERVAL", 60)
    assert run("ticket_panel")[0] == 200
    assert run("ticket_panel")[0] == 429 and len(act.posts) == 1
