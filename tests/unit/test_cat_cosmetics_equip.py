"""Owned cosmetics can be worn, and they show on the drawn profile card and the plain profile."""
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from PIL import Image

from discord_bot.cogs import _views_cat_features as vf
from modules import cat_features as cf, cat_profile_card as pc, cat_share
from tests.unit.test_cat_adopt import FI

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Conn:
    def __init__(self, kinds, owned, worn=None):
        self.kinds, self.owned, self.worn = kinds, set(owned), dict(worn or {})     # worn: kind -> cosmetic id
    def transaction(self): return Tx()
    async def fetchrow(self, sql, *a):
        return {"id": a[0], "kind": self.kinds[a[0]]} if a[0] in self.kinds else None
    async def fetchval(self, sql, *a):
        if "FROM cat_cosmetic_ownership" in sql:
            return 1 if a[2] in self.owned else None
        return self.worn.get(a[2])
    async def execute(self, sql, *a):
        if sql.startswith("DELETE"):
            self.worn.pop(a[2], None)
        else:
            self.worn[a[2]] = a[3]


def use(monkeypatch, conn):
    class Acq:
        async def __aenter__(self): return conn
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(cf, "_pool", AsyncMock(return_value=SimpleNamespace(acquire=lambda: Acq())))
    return conn


KINDS = {1: "hat", 2: "hat", 3: "background"}


def test_wearing_replaces_the_same_kind_and_toggles_off(monkeypatch):
    conn = use(monkeypatch, Conn(KINDS, owned={1, 2, 3}))
    assert run(cf.toggle_equip(10, None, 1)) == "equipped" and conn.worn == {"hat": 1}
    assert run(cf.toggle_equip(10, None, 2)) == "equipped" and conn.worn == {"hat": 2}      # one hat at a time
    assert run(cf.toggle_equip(10, None, 3)) == "equipped" and conn.worn == {"hat": 2, "background": 3}
    assert run(cf.toggle_equip(10, None, 2)) == "removed" and conn.worn == {"background": 3}


def test_you_cannot_wear_what_you_do_not_own_or_that_does_not_exist(monkeypatch):
    conn = use(monkeypatch, Conn(KINDS, owned={1}))
    assert run(cf.toggle_equip(10, None, 2)) == "not_owned" and conn.worn == {}
    assert run(cf.toggle_equip(10, None, 99)) == "missing"


def test_reads_never_raise(monkeypatch):
    monkeypatch.setattr(cf, "_pool", AsyncMock(side_effect=RuntimeError("db")))
    assert run(cf.equipped(10, None)) == {}
    assert run(cf.wardrobe(10, None)) == ([], set())


def test_equip_select_wears_the_item_and_says_so(monkeypatch):
    monkeypatch.setattr(cf, "toggle_equip", AsyncMock(return_value="equipped"))
    item = cf.Cosmetic(1, "royal-crown", "hat", "Royal Crown", "👑", 0, 8)
    sel = vf.CosmeticEquipSelect([item], set())
    monkeypatch.setattr(type(sel), "values", property(lambda self: ["1"]))
    i = FI(10)
    run(sel.callback(i))
    assert "Royal Crown" in i.sent[0][0] and "on your profile card" in i.sent[0][0] and i.sent[0][1]["ephemeral"] is False
    assert sel.options[0].description.startswith("Pick to wear")


def test_worn_item_is_offered_for_removal():
    item = cf.Cosmetic(1, "royal-crown", "hat", "Royal Crown", "👑", 0, 8)
    assert vf.CosmeticEquipSelect([item], {1}).options[0].description.startswith("Worn now")


@pytest.mark.parametrize("equipped", [{}, {"hat": "royal-crown"}, {"hat": "strawberry-hat", "collar": "moon-collar"},
                                       {"background": "galaxy-card"}, {"background": "sunset-card", "hat": "royal-crown", "collar": "moon-collar"},
                                       {"hat": "unknown-hat", "background": "unknown-card"}])
def test_card_draws_every_combination_at_the_fixed_size(equipped):
    out = pc.draw_card("Ama", "bio", 3, ("Biscuit", "legendary"), 2, [("Mochi", "Sleepy", "rare")], None, [None], equipped)
    assert Image.open(io.BytesIO(out)).size == (pc.W, pc.H)


def test_cosmetics_change_the_picture():
    plain = pc.draw_card("Ama", "bio", 3, None, 0, [], None, [])
    dressed = pc.draw_card("Ama", "bio", 3, None, 0, [], None, [], {"hat": "royal-crown", "background": "galaxy-card"})
    assert plain != dressed


def test_render_passes_the_worn_slugs_to_the_card(monkeypatch):
    seen = {}
    monkeypatch.setattr(pc, "draw_card", lambda *a, **k: seen.update(eq=a[8]) or b"png")
    monkeypatch.setattr(pc, "_avatar_bytes", AsyncMock(return_value=None))
    profile = {"bio": "", "collection_size": 0, "rarest": None, "pinned": [],
               "equipped": {"hat": {"slug": "royal-crown", "name": "Royal Crown", "emoji": "👑"}}}
    assert run(pc.render(SimpleNamespace(), SimpleNamespace(), "Ama", profile, [])) == b"png"
    assert seen["eq"] == {"hat": "royal-crown"}


def test_plain_profile_names_what_is_worn():
    profile = {"bio": "", "collection_size": 1, "rarest": None, "pinned": [],
               "equipped": {"hat": {"slug": "royal-crown", "name": "Royal Crown", "emoji": "👑"}}}
    e = cat_share.profile_embed("Ama", profile, [])
    assert any(f.name == "Wearing" and "Royal Crown" in f.value for f in e.fields)
    assert not any(f.name == "Wearing" for f in cat_share.profile_embed("Ama", {**profile, "equipped": {}}, []).fields)


def test_migration_is_additive_and_wired():
    sql = (ROOT / "database/migrations/048_cat_equipped.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_equipped" in sql and "DROP " not in sql.upper() and "DELETE FROM" not in sql.upper()
    assert "048_cat_equipped.sql" in (ROOT / "database.py").read_text()
