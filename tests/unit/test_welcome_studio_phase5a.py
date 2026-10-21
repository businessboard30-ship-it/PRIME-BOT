"""Welcome Card Studio Phase 5a: saved designs, data layer only (no wizard change, no render change)."""
import asyncio
import json
import re
from pathlib import Path

import pytest

import database as dbmod
from modules import welcome_card as wc
from modules import welcome_designs as wd

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


# ---------- pure helpers ----------

def test_clean_name():
    assert wd.clean_name("  Halloween   look ") == "Halloween look"
    assert wd.clean_name("") is None and wd.clean_name(None) is None and wd.clean_name("\n\t ") is None
    assert len(wd.clean_name("x" * 100)) == wd.MAX_NAME_LEN
    assert "@\u200b" in wd.clean_name("@everyone") and wd.clean_name("@everyone") != "@everyone"
    assert "\n" not in wd.clean_name("a\nb")
    assert wd.clean_name(12345) == "12345"


def test_snapshot_is_validated_json_and_never_holds_a_picture():
    j, shape = wd.snapshot_look({"font": "marquee", "frame": "glow", "junk": 1, "zoom": 9999}, "hexagon-not-real")
    d = json.loads(j)
    assert d["font"] == "marquee" and d["frame"] == "glow" and "junk" not in d and d["zoom"] == 100
    assert shape == wc.DEFAULT_AVATAR_SHAPE
    assert not any(k.startswith(("custom_bg", "custom_background")) for k in d)
    assert wd.snapshot_look(None, None)[0] == json.dumps(wc.parse_ultra_options(None), sort_keys=True)
    assert wd.snapshot_look("not json{", "circle")[1] == "circle"


def test_every_real_shape_is_kept_and_restore_round_trips():
    for shape in wc.AVATAR_SHAPES:
        assert wd.snapshot_look({}, shape)[1] == shape
    j, s = wd.snapshot_look({"effect": "sparkle", "text_dx": 40}, "circle")
    assert wd.restore_look(j, s) == (j, s)
    assert wd.restore_look("garbage", "nope") == wd.snapshot_look(None, None)


def test_limit_is_five():
    assert wd.MAX_DESIGNS == 5


# ---------- migration + wiring ----------

def test_migration_is_additive_and_registered_and_version_bumped():
    sql = (ROOT / "database/migrations/080_welcome_designs.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS discord_welcome_designs" in sql
    assert "ADD COLUMN IF NOT EXISTS active_design_id" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    assert not re.search(r"\bDROP\b|\bTRUNCATE\b|\bDELETE\b", sql, re.I)
    src = (ROOT / "database.py").read_text()
    assert "080_welcome_designs.sql" in src
    assert re.search(r'^SCHEMA_VERSION = "129"', src, re.M)


def test_wizard_and_renderer_do_not_use_designs_yet():
    for f in ("modules/welcome_card.py", "discord_bot/cogs/_views_card_customize.py", "discord_bot/cogs/welcome.py"):
        assert "welcome_designs" not in (ROOT / f).read_text(), f


# ---------- database methods against a tiny in-memory fake ----------

class FakeConn:
    def __init__(self, store):
        self.s = store

    def transaction(self):
        class T:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *a):
                return False
        return T()

    async def execute(self, sql, *a):
        if "pg_advisory" in sql:
            return "SELECT 1"
        if sql.startswith("UPDATE discord_welcome_designs SET options_json"):
            r = next(x for x in self.s["rows"] if x["id"] == a[2])
            r["options_json"], r["avatar_shape"] = a[0], a[1]
            return "UPDATE 1"
        if sql.startswith("UPDATE discord_welcome_designs SET name"):
            name, did, g, k = a
            hit = [x for x in self.s["rows"] if x["id"] == did and x["guild_id"] == g and x["clone_key"] == k]
            if hit and any(x["name"].lower() == name.lower() and x is not hit[0] and x["guild_id"] == g and x["clone_key"] == k
                           for x in self.s["rows"]):
                raise RuntimeError("unique violation")
            if hit:
                hit[0]["name"] = name
            return "UPDATE %d" % len(hit)
        if sql.startswith("DELETE FROM discord_welcome_designs"):
            did, g, k = a
            before = len(self.s["rows"])
            self.s["rows"] = [x for x in self.s["rows"] if not (x["id"] == did and x["guild_id"] == g and x["clone_key"] == k)]
            return "DELETE %d" % (before - len(self.s["rows"]))
        if sql.startswith("UPDATE discord_welcome_config SET active_design_id = NULL"):
            self.s["cleared"].append(a)
            return "UPDATE 1"
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        if "lower(name) = lower" in sql:
            g, k, name = a
            return next((x for x in self.s["rows"] if x["guild_id"] == g and x["clone_key"] == k and x["name"].lower() == name.lower()), None)
        if "WHERE id = $1 AND guild_id" in sql:
            did, g, k = a
            return next((x for x in self.s["rows"] if x["id"] == did and x["guild_id"] == g and x["clone_key"] == k), None)
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        if sql.startswith("SELECT COUNT"):
            g, k = a
            return sum(1 for x in self.s["rows"] if x["guild_id"] == g and x["clone_key"] == k)
        if sql.startswith("INSERT"):
            g, k, name, oj, sh, by = a
            self.s["next"] += 1
            self.s["rows"].append({"id": self.s["next"], "guild_id": g, "clone_key": k, "name": name,
                                   "options_json": oj, "avatar_shape": sh})
            return self.s["next"]
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        g, k = a
        return [x for x in self.s["rows"] if x["guild_id"] == g and x["clone_key"] == k]


class FakePool:
    def __init__(self, store):
        self.store = store

    def acquire(self):
        conn = FakeConn(self.store)

        class A:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *a):
                return False
        return A()


@pytest.fixture
def d(monkeypatch):
    store = {"rows": [], "next": 0, "cleared": []}

    async def gp():
        return FakePool(store)
    monkeypatch.setattr(dbmod, "get_pool", gp)
    inst = dbmod.Database.__new__(dbmod.Database) if hasattr(dbmod, "Database") else None
    assert inst is not None
    return inst, store


def test_save_create_update_and_limit(d):
    db, store = d
    assert run(db.design_save(1, None, "A", "{}", "circle", 9)) == ("created", 1)
    assert run(db.design_save(1, None, "a", '{"x":1}', "square", 9)) == ("updated", 1)      # same name, any case
    assert len(store["rows"]) == 1 and store["rows"][0]["options_json"] == '{"x":1}'
    for n in "BCDE":
        assert run(db.design_save(1, None, n, "{}", "circle", 9))[0] == "created"
    assert run(db.design_save(1, None, "F", "{}", "circle", 9)) == ("full", None)
    assert len(store["rows"]) == 5
    assert run(db.design_save(1, None, "B", "{}", "circle", 9))[0] == "updated"             # full servers can still overwrite
    assert run(db.design_save(1, 55, "A", "{}", "circle", 9))[0] == "created"               # a clone has its own five
    assert run(db.design_save(2, None, "A", "{}", "circle", 9))[0] == "created"             # so does another server


def test_list_get_rename_delete_are_scoped(d):
    db, store = d
    run(db.design_save(1, None, "A", "{}", "circle", 9))
    run(db.design_save(1, None, "B", "{}", "circle", 9))
    run(db.design_save(2, None, "Other", "{}", "circle", 9))
    assert [x["name"] for x in run(db.design_list(1, None))] == ["A", "B"]
    assert run(db.design_get(1, None, 1))["name"] == "A" and run(db.design_get(2, None, 1)) is None
    assert run(db.design_rename(1, None, 1, "Renamed")) is True
    assert run(db.design_rename(1, None, 99, "x")) is False
    assert run(db.design_rename(1, None, 1, "b")) is False                                   # taken by B
    assert run(db.design_delete(2, None, 1)) is False and len(store["rows"]) == 3            # wrong server: nothing deleted
    assert run(db.design_delete(1, None, 1)) is True and store["cleared"]
    assert [x["name"] for x in run(db.design_list(1, None))] == ["B"]


def test_methods_never_raise_on_database_errors(monkeypatch):
    async def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(dbmod, "get_pool", boom)
    db = dbmod.Database.__new__(dbmod.Database)
    assert run(db.design_list(1, None)) == [] and run(db.design_get(1, None, 1)) is None
    assert run(db.design_save(1, None, "A", "{}", "circle", 9)) == ("error", None)
    assert run(db.design_rename(1, None, 1, "x")) is False and run(db.design_delete(1, None, 1)) is False


def test_delete_never_touches_pictures_or_the_live_card():
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def design_delete"):src.index("async def gallery_count_recent")]
    assert "discord_welcome_backgrounds" not in body and "ultra_card_json" not in body
