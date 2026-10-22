"""Web dashboard: Cat game module (the same cat_guild_config row /catsetup writes)."""
import asyncio

import pytest

from database import Database
from modules import cat_setup as cs
from utils import dash_schema as S
from tests.unit import test_dash_api
from tests.unit.test_dash_api import env, call, GUILD  # noqa: F401

CH = {"text": {"100", "101", "102", "103"}, "category": set(), "voice": set()}


@pytest.fixture
def store(env, monkeypatch):
    """Back cat_setup's three DB functions with a dict, then run the REAL get/set_cat_config wrappers."""
    fake, _ = env
    rows = {}

    async def get_config(guild_id, clone_id=None):
        return rows.get((guild_id, clone_id)) or cs.CatConfig(guild_id=guild_id, clone_id=clone_id)

    async def save_config(cfg, updated_by=None):
        rows[(cfg.guild_id, cfg.clone_id)] = cfg

    async def is_premium(guild_id, clone_id=None):
        return await fake.is_guild_premium_active(guild_id, clone_id)

    monkeypatch.setattr(cs, "get_config", get_config)
    monkeypatch.setattr(cs, "save_config", save_config)
    monkeypatch.setattr(cs, "is_premium", is_premium)
    monkeypatch.setattr(test_dash_api, "CHANNELS", [{"id": c, "name": f"c{c}", "type": 0, "position": i}
                                                    for i, c in enumerate(("100", "101", "102", "103"))])
    real = Database.__new__(Database)

    async def get(gid, clone_id=None): return await Database.get_cat_config(real, gid, clone_id)
    async def put(gid, clone_id=None, **f): return await Database.set_cat_config(real, gid, clone_id, **f)
    monkeypatch.setattr(type(fake), "get_cat_config", lambda self, g, c=None: get(g, c), raising=False)
    monkeypatch.setattr(type(fake), "set_cat_config", lambda self, g, c=None, **f: put(g, c, **f), raising=False)
    return rows


def _save(values, module="cat"):
    return call("POST", body={"action": "save", "guild_id": str(GUILD), "module": module, "values": values})


def test_module_is_registered_and_valid():
    m = S.BY_ID["cat"]
    assert m["category"] == "Fun" and m["get"] == "get_cat_config" and m["set"] == "set_cat_config"
    keys = [f["key"] for f in m["fields"]]
    assert keys == ["enabled", "channel_1", "channel_2", "channel_3", "pace_percent", "active_start",
                    "active_end", "announce_mode", "fights_cross_server", "fights_show_server"]
    by = {f["key"]: f for f in m["fields"]}
    assert [v for v, _ in by["pace_percent"]["options"]] == [str(p) for p in cs.PACES]      # same choices as /catsetup
    assert [v for v, _ in by["announce_mode"]["options"]] == list(cs.ANNOUNCE_MODES)
    assert len(by["active_start"]["options"]) == 24 and len(by["active_end"]["options"]) == 24
    assert by["channel_2"]["premium"] and by["channel_3"]["premium"] and not by["channel_1"].get("premium")


def test_validation():
    m = S.BY_ID["cat"]
    ok, err = S.validate_values(m, {"pace_percent": "50", "active_end": 24, "announce_mode": "off"}, CH, set(), False)
    assert not err and ok == {"pace_percent": 50, "active_end": 24, "announce_mode": "off"}
    assert S.validate_values(m, {"pace_percent": 120}, CH, set(), False)[1]                   # not a listed pace
    assert S.validate_values(m, {"active_start": 24}, CH, set(), False)[1]                    # start is 0..23
    assert S.validate_values(m, {"channel_1": "999"}, CH, set(), False)[1]                    # channel not in this server
    assert S.validate_values(m, {"channel_2": "101"}, CH, set(), False)[1]                    # Premium only
    assert not S.validate_values(m, {"channel_2": "101"}, CH, set(), True)[1]


def test_wrappers_roundtrip_and_defaults(store):
    real = Database.__new__(Database)
    d = asyncio.run(Database.get_cat_config(real, 1, None))
    assert d == {"enabled": False, "channel_1": None, "channel_2": None, "channel_3": None, "pace_percent": 100,
                 "active_start": 0, "active_end": 24, "announce_mode": "all",
                 "fights_cross_server": False, "fights_show_server": False}
    asyncio.run(Database.set_cat_config(real, 1, None, channel_1=100, pace_percent=75))
    assert store[(1, None)].channel_ids == (100,) and store[(1, None)].pace_percent == 75
    asyncio.run(Database.set_cat_config(real, 1, None))                                       # no field: unchanged
    assert store[(1, None)].pace_percent == 75
    asyncio.run(Database.set_cat_config(real, 1, 7, enabled=True))                            # clones are separate
    assert (1, 7) in store and store[(1, None)].enabled is False


def test_turning_on_schedules_a_cat_like_the_wizard(store):
    st, p, _ = _save({"channel_1": "100"})                              # game still off: nothing scheduled
    assert st == 200 and store[(GUILD, None)].next_at is None
    st, p, _ = _save({"enabled": True})                                 # turning it on posts one on the next tick
    assert st == 200 and p["values"]["enabled"] is True and store[(GUILD, None)].next_at is not None


def test_changing_spawn_hours_resets_the_schedule(store):
    _save({"enabled": True, "channel_1": "100"})
    assert store[(GUILD, None)].next_at is not None
    st, _, _ = _save({"active_start": 8, "active_end": 20})
    assert st == 200 and store[(GUILD, None)].next_at is None and store[(GUILD, None)].active_start == 8


def test_free_server_gets_one_channel_premium_three(store, env):
    fake, _ = env
    st, p, _ = _save({"channel_1": "100", "channel_2": "101"})
    assert st in (400, 403, 422) and "Premium" in str(p) and (GUILD, None) not in store
    fake.premium = True
    st, p, _ = _save({"channel_1": "100", "channel_2": "101", "channel_3": "102"})
    assert st == 200 and store[(GUILD, None)].channel_ids == (100, 101, 102)
    assert p["values"]["channel_3"] == "102"
    st, p, _ = _save({"channel_2": ""})                                                       # clearing a slot drops it
    assert st == 200 and store[(GUILD, None)].channel_ids == (100, 102)


def test_lapsed_premium_keeps_channels_and_the_spawner_clamps(store, env):
    """An unrelated save never edits stored channels (re-subscribing restores them, same as /catsetup);
    the spawner clamps to the plan the server has right now via CatConfig.effective."""
    fake, _ = env
    fake.premium = True
    _save({"channel_1": "100", "channel_2": "101"})
    fake.premium = False
    st, _, _ = _save({"pace_percent": 50})
    cfg = store[(GUILD, None)]
    assert st == 200 and cfg.channel_ids == (100, 101)
    assert cfg.effective(premium=False)[0] == (100,)
    st, _, _ = _save({"channel_1": "100"})                    # touching a channel slot re-applies the free limit
    assert st == 200 and store[(GUILD, None)].channel_ids == (100,)


def test_duplicate_channels_collapse(store, env):
    fake, _ = env
    fake.premium = True
    _save({"channel_1": "100", "channel_2": "100"})
    assert store[(GUILD, None)].channel_ids == (100,)


def test_fight_toggles_and_announce(store):
    st, p, _ = _save({"fights_cross_server": True, "fights_show_server": True, "announce_mode": "milestones"})
    assert st == 200
    cfg = store[(GUILD, None)]
    assert cfg.fights_cross_server and cfg.fights_show_server and cfg.announce_mode == "milestones"
    st, p, _ = call("GET", {"action": "config", "guild_id": str(GUILD), "module": "cat"})
    assert st == 200 and p["values"]["announce_mode"] == "milestones"


def test_permissions_and_unknown_keys(store):
    body = {"action": "save", "guild_id": str(GUILD), "module": "cat", "values": {"enabled": True}}
    assert call("POST", token=None, body=body)[0] == 401
    assert call("POST", body=dict(body, guild_id="999"))[0] == 403
    assert _save({"channel_ids": [1]})[0] in (400, 422)                                       # raw column is not a setting
    assert (GUILD, None) not in store
