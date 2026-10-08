"""Server panel additions: Bump network (A7) and Custom roles (A6). Language (A5) is a per-user setting and lives in
the member 'My preferences' phase instead. Schema validation, the DB adapters, and the real route."""
import asyncio

import pytest

from api import dash
from database import Database
from utils import dash_schema as S
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

CH = {"text": {"100", "101"}, "category": {"200"}, "voice": {"300"}}


def v(mod, values, premium=False):
    return S.validate_values(S.BY_ID[mod], values, CH, set(), premium)


# ---------- schema ----------
def test_modules_are_registered_and_valid():
    for mid in ("bumpnet", "customrole"):
        m = S.BY_ID[mid]
        assert m["category"] in S.CATEGORIES and callable(getattr(Database, m["get"])) and callable(getattr(Database, m["set"]))


def test_bump_validation():
    clean, err = v("bumpnet", {"receives_bumps": True, "bump_channel_id": "101", "language": "fr", "nsfw_opt_in": False, "intensity_level": "4"})
    assert not err and clean["bump_channel_id"] == 101 and clean["intensity_level"] == "4"
    assert v("bumpnet", {"bump_channel_id": "999"})[1]                 # not a channel of this server
    assert v("bumpnet", {"language": "klingon"})[1]
    assert v("bumpnet", {"intensity_level": "9"})[1]
    assert v("bumpnet", {"receives_bumps": "yes"})[1]
    assert v("bumpnet", {"bogus": 1})[1]


def test_custom_role_validation():
    assert v("customrole", {"enabled": False})[0] == {"enabled": False}
    assert v("customrole", {"enabled": "no"})[1]


# ---------- DB adapters (no schema change: they only call existing functions) ----------
class FakeSelf:
    def __init__(self, cfg=None, disabled=False):
        self.cfg, self.disabled, self.calls = cfg, disabled, []

    async def bump_get_guild_config(self, g, c): return self.cfg

    async def bump_set_guild_config(self, g, c, by, **kw): self.calls.append(("bump", g, c, by, kw))

    async def is_custom_role_feature_disabled(self, g, c): return self.disabled

    async def set_custom_role_feature_disabled(self, g, disabled, clone_id=None): self.calls.append(("cr", g, disabled, clone_id))


def run(coro): return asyncio.run(coro)


def test_bump_get_defaults_and_stringifies_intensity():
    out = run(Database.get_bump_settings_config(FakeSelf(None), 5, None))
    assert out == {"receives_bumps": False, "bump_channel_id": None, "language": "any", "nsfw_opt_in": False, "intensity_level": "3"}
    out = run(Database.get_bump_settings_config(FakeSelf({"receives_bumps": True, "bump_channel_id": 9, "language": "es", "nsfw_opt_in": True, "intensity_level": 5}), 5, None))
    assert out["intensity_level"] == "5" and out["bump_channel_id"] == 9


def test_bump_set_keeps_configured_by_converts_intensity_and_ignores_cleared_channel():
    f = FakeSelf({"configured_by": 42})
    run(Database.set_bump_settings_config(f, 5, 7, intensity_level="4", bump_channel_id=None, receives_bumps=False))
    _, g, c, by, kw = f.calls[0]
    assert (g, c, by) == (5, 7, 42)
    assert kw == {"bump_channel_id": None, "language": None, "nsfw_opt_in": None, "intensity_level": 4, "receives_bumps": False}
    f2 = FakeSelf(None)
    run(Database.set_bump_settings_config(f2, 5, None, language="en"))
    assert f2.calls[0][3] == 0 and f2.calls[0][4]["intensity_level"] is None      # untouched fields stay None (COALESCE keeps them)


def test_custom_role_adapter_inverts_disabled():
    assert run(Database.get_custom_role_settings_config(FakeSelf(disabled=True), 5)) == {"enabled": False}
    assert run(Database.get_custom_role_settings_config(FakeSelf(disabled=False), 5)) == {"enabled": True}
    f = FakeSelf()
    run(Database.set_custom_role_settings_config(f, 5, 3, enabled=False))
    assert f.calls == [("cr", 5, True, 3)]
    run(Database.set_custom_role_settings_config(f, 5, 3, enabled=True))
    assert f.calls[-1] == ("cr", 5, False, 3)
    n = len(f.calls)
    run(Database.set_custom_role_settings_config(f, 5, 3))             # nothing to write
    assert len(f.calls) == n


# ---------- through the real route ----------
def test_save_bump_through_route(env, monkeypatch):
    fake, _ = env
    saved = {}

    async def get_cfg(g, c): return {"receives_bumps": False, "bump_channel_id": None, "language": "any", "nsfw_opt_in": False, "intensity_level": "3"}

    async def set_cfg(g, c, **kw): saved.update(kw)
    monkeypatch.setattr(fake, "get_bump_settings_config", get_cfg, raising=False)
    monkeypatch.setattr(fake, "set_bump_settings_config", set_cfg, raising=False)
    body = {"action": "save", "guild_id": str(GUILD), "module": "bumpnet", "values": {"receives_bumps": True, "intensity_level": "2"}}
    st, p, _ = call("POST", body=body)
    assert st == 200 and saved == {"receives_bumps": True, "intensity_level": "2"}
    bad = dict(body, values={"intensity_level": "77"})
    assert call("POST", body=bad)[0] in (400, 422)


def test_modules_listed_in_public_schema_without_db_method_names():
    mods = {m["id"]: m for m in S.public_schema()["modules"]}
    for mid in ("bumpnet", "customrole"):
        assert mid in mods and "get" not in mods[mid] and "set" not in mods[mid]
