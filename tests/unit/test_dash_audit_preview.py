import base64

import pytest

from api import dash
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401


@pytest.fixture
def fx(env, monkeypatch):
    fake, members = env
    state = {"cfg": {"enabled": False, "channel_id": None, "message_template": "hi", "accent_color": "#5865F2",
                     "background_color": "#2b2d31", "use_template": True, "card_theme": "wolf",
                     "avatar_shape": "circle", "card_pack_unlocked": False}}
    fake.audit = []

    async def get(gid, clone_id=None):
        return dict(state["cfg"])
    async def setc(gid, clone_id=None, **f):
        state["cfg"].update(f)
    async def add(gid, uid, name, module, changes, retention_days=180):
        fake.audit.append(dict(id=str(len(fake.audit) + 1), user_id=uid, user_name=name, module=module, changes=changes, created_at="2026-01-01T00:00:00+00:00"))
    async def lst(gid, before_id=None, module=None, limit=30):
        rows = [r for r in reversed(fake.audit) if (not module or r["module"] == module)]
        return rows[:limit]
    fake.get_welcome_config, fake.set_welcome_config = get, setc
    fake.dash_audit_add, fake.dash_audit_list = add, lst

    async def no_avatar(url):
        from io import BytesIO
        from PIL import Image
        b = BytesIO(); Image.new("RGB", (64, 64), (200, 50, 50)).save(b, "PNG"); return b.getvalue()
    monkeypatch.setattr(dash, "_avatar_bytes", no_avatar)
    monkeypatch.setattr(dash, "PREVIEW_MIN_INTERVAL", 0)
    dash._last_preview.clear()
    return fake, state


def save(values, token="sid"):
    return call("POST", token=token, body={"action": "save", "guild_id": str(GUILD), "module": "welcome", "values": values})


def test_diff_values_only_declared_and_truncated():
    m = S.BY_ID["welcome"]
    d = S.diff_values(m, {"enabled": False, "message_template": "a", "guild_id": 1}, {"enabled": True, "message_template": "a" * 500, "guild_id": 2})
    assert set(d) == {"enabled", "message_template"} and d["enabled"] == {"from": False, "to": True}
    assert len(d["message_template"]["to"]) == S.AUDIT_VALUE_MAX + 1
    assert S.diff_values(m, {"enabled": True}, {"enabled": True}) == {}


def test_save_writes_audit_entry_and_skips_noop(fx):
    fake, _ = fx
    assert save({"enabled": True})[0] == 200
    assert len(fake.audit) == 1 and fake.audit[0]["changes"]["enabled"] == {"from": False, "to": True}
    assert fake.audit[0]["user_id"] == "6" and fake.audit[0]["module"] == "welcome"
    assert save({"enabled": True})[0] == 200
    assert len(fake.audit) == 1                      # nothing changed, nothing logged


def test_save_survives_audit_failure(fx):
    fake, state = fx
    async def boom(*a, **k):
        raise RuntimeError("db down")
    fake.dash_audit_add = boom
    assert save({"enabled": True})[0] == 200 and state["cfg"]["enabled"] is True


def test_audit_endpoint_authorisation_and_paging(fx):
    fake, _ = fx
    save({"enabled": True})
    st, p, _ = call("GET", {"action": "audit", "guild_id": str(GUILD)})
    assert st == 200 and len(p["entries"]) == 1 and p["more"] is False
    assert call("GET", {"action": "audit", "guild_id": "999"})[0] == 403
    assert call("GET", {"action": "audit", "guild_id": str(GUILD), "module": "nope"})[0] == 404
    assert call("GET", {"action": "audit", "guild_id": str(GUILD), "before": "x"})[0] == 400
    assert call("GET", {"action": "audit", "guild_id": str(GUILD)}, token=None)[0] == 401


def test_card_pack_buyer_can_use_premium_theme_without_premium(fx):
    fake, state = fx
    assert save({"card_theme": "reaper"})[0] == 422          # free server, no pack
    state["cfg"]["card_pack_unlocked"] = True
    assert save({"card_theme": "reaper"})[0] == 200 and state["cfg"]["card_theme"] == "reaper"
    st, p, _ = call("GET", {"action": "config", "guild_id": str(GUILD), "module": "welcome"})
    assert p["premium"] is True


def png(p):
    assert p["image"].startswith("data:image/png;base64,")
    return base64.b64decode(p["image"].split(",", 1)[1])


def test_preview_renders_free_theme_and_flat_card(fx):
    q = {"action": "welcome_preview", "guild_id": str(GUILD), "theme": "wolf", "shape": "hexagon"}
    st, p, _ = call("GET", q)
    assert st == 200 and png(p)[:8] == b"\x89PNG\r\n\x1a\n"
    st, p, _ = call("GET", dict(q, use_template="0", bg="#112233", accent="#ff00aa"))
    assert st == 200 and png(p)[:4] == b"\x89PNG"


def test_preview_gates_and_validates(fx):
    fake, state = fx
    base = {"action": "welcome_preview", "guild_id": str(GUILD)}
    assert call("GET", dict(base, theme="reaper"))[0] == 403
    assert call("GET", dict(base, theme="reaper", use_template="0"))[0] == 200   # flat card ignores theme
    state["cfg"]["card_pack_unlocked"] = True
    assert call("GET", dict(base, theme="reaper"))[0] == 200
    assert call("GET", dict(base, theme="../../etc"))[0] == 422
    assert call("GET", dict(base, shape="blob"))[0] == 422
    assert call("GET", dict(base, bg="red"))[0] == 422
    assert call("GET", dict(base, theme="wolf"), token=None)[0] == 401
    assert call("GET", {"action": "welcome_preview", "guild_id": "999"})[0] == 403


def test_preview_is_rate_limited(fx, monkeypatch):
    monkeypatch.setattr(dash, "PREVIEW_MIN_INTERVAL", 60)
    q = {"action": "welcome_preview", "guild_id": str(GUILD)}
    assert call("GET", q)[0] == 200
    assert call("GET", q)[0] == 429
