"""Server branding (modules.guild_brand + its hooks), expired-interaction handling and the DB keepalive."""
import asyncio
import json
from types import SimpleNamespace

import discord
import pytest

import database
from modules import guild_brand as gb


class FakeDB:
    def __init__(self, fail=False):
        self.rows, self.reads, self.fail = {}, 0, fail

    async def get_global_setting(self, key):
        self.reads += 1
        if self.fail:
            raise RuntimeError("db down")
        return self.rows.get(key)

    async def set_global_setting(self, key, value):
        if self.fail:
            raise RuntimeError("db down")
        self.rows[key] = value


@pytest.fixture(autouse=True)
def _clean():
    gb._cache.clear()
    yield
    gb._cache.clear()


def run(coro):
    return asyncio.run(coro)


# ── cleaning ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_levelup_message_is_cleaned_and_needs_the_user_placeholder():
    assert gb.clean_message("") is None and gb.clean_message("   ") is None
    assert gb.clean_message("🔥 {user} hit {level}!") == "🔥 {user} hit {level}!"
    with pytest.raises(ValueError):
        gb.clean_message("no placeholder")
    with pytest.raises(ValueError):
        gb.clean_message("{user} " + "x" * 300)
    out = gb.clean_message("{user} @everyone @HERE <@123> <@&9> <#5> hi")
    assert "<@" not in out and "<#" not in out and "@everyone" not in out and "@HERE" not in out and "{user}" in out


def test_names_and_accent_validation():
    assert gb.clean_name("") is None and gb.clean_name("Rank") == "Rank" and gb.clean_name("  star  points ") == "star points"
    for bad in ("x" * 17, "<@1>", "💥", "a/b", "https://x.y"):
        with pytest.raises(ValueError):
            gb.clean_name(bad)
    assert gb.clean_accent("") is None and gb.clean_accent("#5865F2") == 0x5865F2 and gb.clean_accent("00ff00") == 0x00FF00
    for bad in ("red", "#12345", "#GGGGGG", "0x5865F2"):
        with pytest.raises(ValueError):
            gb.clean_accent(bad)


def test_render_fills_known_placeholders_only_and_cannot_reach_attributes():
    out = gb.render("{user} reached {level_name} {level} in {server} ({xp_name}) {x} {0} {user.__class__}",
                    user="<@1>", level=5, server="S", level_name="rank", xp_name="pts")
    assert out == "<@1> reached rank 5 in S (pts) {x} {0} {user.__class__}"


# ── storage ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_save_read_clear_and_clone_isolation():
    db = FakeDB()
    assert run(gb.set_fields(1, None, {"levelup_message": "{user} up", "accent": 255, "level_name": "rank"}, db=db))
    assert run(gb.get(1, None, db=db)) == {"levelup_message": "{user} up", "accent": 255, "level_name": "rank"}
    assert run(gb.get(1, 7, db=db)) == {} and run(gb.get(2, None, db=db)) == {}          # other clone / other server: untouched
    assert run(gb.set_fields(1, None, {"levelup_message": None, "xp_name": "pts"}, db=db))
    assert run(gb.get(1, None, db=db)) == {"accent": 255, "level_name": "rank", "xp_name": "pts"}
    assert set(db.rows) == {"guild_brand:main:1"}                                          # one small value, no schema


def test_reads_are_cached_and_fail_open():
    db = FakeDB()
    run(gb.set_fields(1, None, {"level_name": "rank"}, db=db))
    gb._cache.clear()
    db.reads = 0
    for _ in range(5):
        run(gb.get(1, None, db=db))
    assert db.reads == 1
    bad = FakeDB(fail=True)
    assert run(gb.get(9, None, db=bad)) == {}
    assert run(gb.set_fields(9, None, {"level_name": "x"}, db=bad)) is False
    db.rows["guild_brand:main:3"] = "not json"
    assert run(gb.get(3, None, db=db)) == {}


def test_accent_color_falls_back_to_the_default():
    db = FakeDB()
    run(gb.set_fields(1, None, {"accent": 0x112233}, db=db))
    gb._cache[(1, None)] = ({"accent": 0x112233}, 1e18)
    assert run(gb.accent_int(1, None)) == 0x112233
    assert run(gb.accent_color(1, None, "dflt")).value == 0x112233
    gb._cache[(2, None)] = ({}, 1e18)
    assert run(gb.accent_color(2, None, "dflt")) == "dflt" and run(gb.accent_int(None)) is None


# ── level-up text in the leveling cog ─────────────────────────────────────────────────────────────────────────────
def _cog():
    from discord_bot.cogs import leveling
    c = leveling.LevelingCog.__new__(leveling.LevelingCog)
    m = SimpleNamespace(mention="<@42>", guild=SimpleNamespace(id=1, name="Wolves"))
    return c, m


def test_levelup_text_default_custom_names_and_failure():
    c, m = _cog()
    gb._cache[(1, None)] = ({}, 1e18)
    assert run(c._levelup_text(m, 5)) == "🎉 <@42> leveled up to **level 5**!"
    assert run(c._levelup_text(m, 5, card=True)) == "🎉 <@42> leveled up!"
    gb._cache[(1, None)] = ({"level_name": "rank"}, 1e18)
    assert run(c._levelup_text(m, 5)) == "🎉 <@42> leveled up to **rank 5**!"
    gb._cache[(1, None)] = ({"levelup_message": "🔥 {user} is {level_name} {level} in {server}", "level_name": "tier"}, 1e18)
    assert run(c._levelup_text(m, 7)) == "🔥 <@42> is tier 7 in Wolves"
    assert run(c._levelup_text(m, 7, card=True)) == "🔥 <@42> is tier 7 in Wolves"
    broken = SimpleNamespace(mention="<@42>", guild=None)                                  # anything odd: standard text, no raise
    assert run(c._levelup_text(broken, 3)) == "🎉 <@42> leveled up to **level 3**!"


# ── the Branding modal (validation and save) ────────────────────────────────────────────────────────────────────────
def test_brand_modal_rejects_bad_input_and_saves_good_input(monkeypatch):
    from discord_bot.cogs import _views_bot_profile as vp
    db = FakeDB()
    monkeypatch.setattr(gb, "set_fields", lambda g, c, ch, db=db: _save(db, g, c, ch))
    sent = []

    async def fake_followup(text, **kw):
        sent.append(text)

    async def noop(*a, **k):
        return None

    async def fake_refresh(inter, g, c, note):
        sent.append("REFRESH " + note)
    monkeypatch.setattr(vp, "_refresh", fake_refresh)
    inter = SimpleNamespace(response=SimpleNamespace(defer=noop), followup=SimpleNamespace(send=fake_followup))

    def modal(msg, accent, lname, xname):
        m = vp._BrandModal(5, None, {})
        for item, v in zip((m.msg, m.accent, m.lname, m.xname), (msg, accent, lname, xname)):
            item._value = v
        return m
    run(modal("no placeholder", "", "", "").on_submit(inter))
    assert sent[-1].startswith("⚠️ Nothing saved")
    run(modal("{user} up", "nope", "", "").on_submit(inter))
    assert "colour" in sent[-1]
    run(modal("{user} up", "#0A0B0C", "rank", "pts").on_submit(inter))
    assert sent[-1].startswith("REFRESH ✅ Branding saved")
    assert json.loads(db.rows["guild_brand:main:5"]) == {"levelup_message": "{user} up", "accent": 0x0A0B0C, "level_name": "rank", "xp_name": "pts"}


async def _save(db, g, c, ch):
    import json as _j
    db.rows[gb._key(g, c)] = _j.dumps({k: v for k, v in ch.items() if v not in (None, "")})
    return True


def test_branding_button_is_registered_free_and_fits_the_wizard_rows():
    from discord_bot.cogs import _views_bot_profile as vp
    assert vp._BrandButton in vp.DYNAMIC_ITEMS and vp._BrandButton.gated is False
    for owner in (True, False):
        view = vp.build_profile_view(None, 1, None, owner, draft={"bio": "x"}, premium=True)
        for row in (c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)):
            assert len(row.children) <= 5


# ── expired interactions are not errors ─────────────────────────────────────────────────────────────────────────────
def _resp(status, code):
    return SimpleNamespace(status=status, reason="Not Found")


def test_unknown_interaction_is_logged_as_a_warning_not_an_error(caplog):
    import logging
    from discord import app_commands
    from discord_bot import bot as botmod
    b = botmod.AnimeBotDiscord.__new__(botmod.AnimeBotDiscord)
    nf = discord.NotFound(_resp(404, 10062), {"code": 10062, "message": "Unknown interaction"})
    err = app_commands.CommandInvokeError(SimpleNamespace(name="setup", qualified_name="ticket setup"), nf)
    inter = SimpleNamespace(command=SimpleNamespace(qualified_name="ticket setup"))
    with caplog.at_level(logging.WARNING):
        run(b._on_app_command_error(inter, err))
    assert any(r.levelno == logging.WARNING and "Expired interaction" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ── keepalive ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_keepalive_pings_every_interval_and_survives_errors(monkeypatch):
    monkeypatch.setenv("DB_KEEPALIVE_SECONDS", "10")
    monkeypatch.setenv("DB_KEEPALIVE_CONNS", "2")
    calls = {"exec": 0, "boom": 1}

    class Conn:
        async def execute(self, q):
            assert q == "SELECT 1"
            calls["exec"] += 1

    class Acq:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *a):
            return False

    class Pool:
        def acquire(self):
            return Acq()

        def get_max_size(self):
            return 3

    async def fake_pool():
        if calls["boom"]:
            calls["boom"] = 0
            raise RuntimeError("pool down once")
        return Pool()

    sleeps = []
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(database, "get_pool", fake_pool)
    monkeypatch.setattr(database.asyncio, "sleep", fast_sleep)

    async def go():
        t = asyncio.create_task(database.pool_keepalive())
        for _ in range(60):
            await real_sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    run(go())
    assert calls["exec"] >= 2 and 10 in sleeps                                              # pinged 2 connections, interval honoured


def test_keepalive_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("DB_KEEPALIVE", "0")
    assert run(asyncio.wait_for(database.pool_keepalive(), 1)) is None
