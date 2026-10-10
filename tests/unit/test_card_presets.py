"""Style presets: one tap sets the look; heading and background are never touched; every preset is valid."""
import asyncio
import json
import re
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_card_customize as cc
from discord_bot.cogs import _views_welcome as vw
from modules.welcome_card import ULTRA_AVATAR_SIDES, ULTRA_BANNERS, ULTRA_DIM_ALPHA, ULTRA_TEXT_COLORS, parse_ultra_options


def test_every_preset_is_valid_and_complete():
    assert 2 <= len(cc.STYLE_PRESETS) <= 25
    for key, p in cc.STYLE_PRESETS.items():
        o = p["opts"]
        assert set(o) == {"banner", "dim", "avatar_side", "text_color", "show_number"}, key
        assert o["banner"] in ULTRA_BANNERS and o["dim"] in ULTRA_DIM_ALPHA
        assert o["avatar_side"] in ULTRA_AVATAR_SIDES and o["text_color"] in ULTRA_TEXT_COLORS
        assert isinstance(o["show_number"], bool) and p["shape"] in vw.AVATAR_SHAPE_LABELS
        assert 0 < len(p["label"]) <= 100 and 0 < len(p["desc"]) <= 100
        # the card parser must accept it unchanged (it silently drops bad values, so compare)
        parsed = parse_ultra_options(json.dumps(o))
        assert all(parsed[k] == v for k, v in o.items()), key


def test_classic_matches_the_default_look():
    d = parse_ultra_options(None)
    c = cc.STYLE_PRESETS["classic"]
    assert all(d[k] == v for k, v in c["opts"].items()) and c["shape"] == "circle"


def test_presets_are_visibly_different_from_each_other():
    looks = {json.dumps([p["opts"], p["shape"]], sort_keys=True) for p in cc.STYLE_PRESETS.values()}
    assert len(looks) == len(cc.STYLE_PRESETS)


def test_the_dropdown_is_on_every_tab_with_nothing_preselected():
    for tab in cc._TABS:
        v = cc.build_customize_view(1, None, 5, {"ultra_pack_unlocked": False}, tab=tab)
        sel = [c for c in v.walk_children() if getattr(c, "custom_id", "").startswith("cardwz_preset:")]
        assert len(sel) == 1
        assert not any(o.default for o in sel[0].item.options) and "preset" in sel[0].item.placeholder.lower()
        assert [o.value for o in sel[0].item.options] == list(cc.STYLE_PRESETS)


def test_preset_id_round_trips_for_main_and_clone():
    for clone, inv in ((None, 5), (7, None)):
        b = cc.CardPresetSelect(123, clone, inv)
        m = re.match(cc.CardPresetSelect.__discord_ui_compiled_template__.pattern, b.item.custom_id)
        again = asyncio.run(cc.CardPresetSelect.from_custom_id(None, None, m))
        assert (again.guild_id, again.clone_id, again.invoker_id) == (123, clone, inv)


@pytest.fixture
def world(monkeypatch):
    st = {"cfg": {"ultra_pack_unlocked": False}, "writes": [], "rerenders": 0, "access": True}

    async def get(gid, clone_id=None):
        return dict(st["cfg"])

    async def setc(gid, clone_id=None, **kw):
        st["writes"].append(kw)
        st["cfg"].update(kw)

    async def rerender(*a, **k):
        st["rerenders"] += 1

    async def access(*a, **k):
        return st["access"]
    monkeypatch.setattr(cc.db, "get_welcome_config", get)
    monkeypatch.setattr(cc.db, "set_welcome_config", setc)
    monkeypatch.setattr(cc, "_rerender", rerender)
    monkeypatch.setattr(cc, "_check_access", access)
    cc._DRAFTS.clear()
    return st


class It:
    def __init__(self, uid=42):
        self.user = SimpleNamespace(id=uid)
        self.response = SimpleNamespace(defer=self._defer)
        self.deferred = 0

    async def _defer(self, **k):
        self.deferred += 1


def choose(value, uid=42):
    item = cc.CardPresetSelect(1, None, uid)
    item.item = SimpleNamespace(values=[value])      # what Discord delivers
    it = It(uid)
    asyncio.run(item.callback(it))
    return it


def test_unpaid_server_only_touches_its_in_memory_draft_and_keeps_heading_and_bg(world):
    d = cc._get_draft(1, None, 42, create=True)
    d["opts"]["heading"] = "Hi {member}"
    d["bg"] = b"IMG"
    choose("bold")
    assert world["writes"] == [] and world["rerenders"] == 1               # nothing written to the database
    d = cc._get_draft(1, None, 42)
    assert d["opts"]["banner"] == "top" and d["opts"]["text_color"] == "gold" and d["shape"] == "hexagon"
    assert d["opts"]["heading"] == "Hi {member}" and d["bg"] == b"IMG"


def test_unlocked_server_saves_in_one_write_and_keeps_heading_and_bg(world):
    world["cfg"] = {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png",
                    "ultra_card_json": json.dumps({"heading": "Welcome home", "dim": "light"}), "avatar_shape": "circle"}
    choose("neon")
    assert len(world["writes"]) == 1
    w = world["writes"][0]
    saved = json.loads(w["ultra_card_json"])
    assert saved["text_color"] == "cyan" and saved["dim"] == "heavy" and saved["heading"] == "Welcome home"
    assert w["avatar_shape"] == "diamond" and "custom_background_url" not in w and world["cfg"]["custom_background_url"] == "https://x/y.png"


def test_unknown_preset_value_is_ignored_but_the_tv_still_refreshes(world):
    world["cfg"] = {"ultra_pack_unlocked": True}
    choose("../../evil")
    assert world["writes"] == [] and world["rerenders"] == 1


def test_no_access_means_no_change_and_no_defer(world):
    world["access"] = False
    it = choose("bold")
    assert world["writes"] == [] and world["rerenders"] == 0 and it.deferred == 0 and cc._get_draft(1, None, 42) is None


def test_preset_select_is_registered_for_restarts():
    assert cc.CardPresetSelect in cc.DYNAMIC_ITEMS
