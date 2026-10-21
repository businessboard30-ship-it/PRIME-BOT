"""Welcome Card Studio Phase 6b: separate DM text + the DMs-closed switch on the join-DM sub-screen."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import _views_welcome as vw
from discord_bot.cogs import welcome as w

ROOT = Path(__file__).resolve().parents[2]


def run(coro):
    return asyncio.run(coro)


def closed():
    return discord.HTTPException(SimpleNamespace(status=400, reason="x"), {"code": 50007, "message": "closed"})


# ---- the real join ---------------------------------------------------------------------------------------------

class Chan:
    def __init__(self): self.sent = []
    async def send(self, content=None, file=None): self.sent.append(content)


class DM:
    def __init__(self, boom=None): self.boom, self.sent = boom, []

    async def send(self, content=None, file=None):
        if self.boom:
            raise self.boom
        self.sent.append(content)


class Sess:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    def get(self, *a, **k):
        class R:
            async def __aenter__(s): return s
            async def __aexit__(s, *x): return False
            async def read(s): return b"a"
        return R()


@pytest.fixture
def join(monkeypatch):
    st = SimpleNamespace(chan=Chan(), boom=None)

    async def get_cfg(g, clone_id=None): return st.cfg
    monkeypatch.setattr(w.db, "get_welcome_config", get_cfg)
    monkeypatch.setattr(w.aiohttp, "ClientSession", Sess)

    async def none(*a, **k): return None
    monkeypatch.setattr(w, "_fetch_sticker_bytes", none)
    monkeypatch.setattr(w, "_gallery_bg_bytes_for_render", none)
    monkeypatch.setattr("modules.welcome_studio.options_for_render", none)
    monkeypatch.setattr(w, "render_welcome_card", lambda *a, **k: (b"C", "PNG"))
    monkeypatch.setattr(w, "_apply_template", lambda t, m: t)          # text out == template in

    def go(**over):
        st.cfg = {"enabled": True, "delivery_mode": "dm", "channel_id": 55, "background_color": "#000",
                  "accent_color": "#fff", "message_template": "NORMAL", "card_style": "png", **over}
        st.dm = DM(st.boom)

        async def create_dm(): return st.dm
        guild = SimpleNamespace(id=1, name="G", member_count=3, me=None,
                                get_channel=lambda c: st.chan if c == 55 else None)
        member = SimpleNamespace(id=7, guild=guild, display_name="S", create_dm=create_dm,
                                 display_avatar=SimpleNamespace(replace=lambda size: SimpleNamespace(url="http://a")))
        run(w.WelcomeCog(SimpleNamespace(clone_id=None)).on_member_join(member))
        return st
    st.go = go
    return st


def test_dm_uses_its_own_text_when_set(join):
    assert join.go(dm_message_template="DMTEXT").dm.sent == ["DMTEXT"]


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_dm_text_means_the_normal_message(join, blank):
    assert join.go(dm_message_template=blank).dm.sent == ["NORMAL"]


def test_missing_key_means_the_normal_message(join):
    assert join.go().dm.sent == ["NORMAL"]


def test_channel_fallback_uses_the_normal_text_not_the_dm_text(join):
    join.boom = closed()
    st = join.go(dm_message_template="DMTEXT", dm_fallback_to_channel=True)
    assert st.chan.sent == ["NORMAL"]


def test_channel_mode_ignores_the_dm_text(join):
    st = join.go(delivery_mode="channel", dm_message_template="DMTEXT")
    assert st.chan.sent == ["NORMAL"] and st.dm.sent == []


# ---- database --------------------------------------------------------------------------------------------------

def _fake_pool(monkeypatch):
    import database
    calls = []

    class Conn:
        async def execute(self, sql, *args):
            calls.append((sql, args))
            return "UPDATE 1"

    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def get_pool(): return Pool()
    monkeypatch.setattr(database, "get_pool", get_pool)
    return database, calls


def test_setter_trims_caps_and_clears(monkeypatch):
    database, calls = _fake_pool(monkeypatch)
    assert run(database.db.set_welcome_dm_text(1, None, "  hi {member}  ")) is True
    run(database.db.set_welcome_dm_text(1, None, "x" * 500))
    run(database.db.set_welcome_dm_text(1, None, "   "))
    run(database.db.set_welcome_dm_text(1, None, None))
    assert calls[0][1] == (1, None, "hi {member}")
    assert len(calls[1][1][2]) == 300
    assert calls[2][1][2] is None and calls[3][1][2] is None
    sql = calls[0][0]
    assert "dm_message_template = $3" in sql and "INSERT" not in sql and "message_template =" not in sql.replace("dm_message_template =", "")


def test_migration_additive_registered_default_null():
    sql = (ROOT / "database/migrations/082_welcome_dm_text.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS dm_message_template TEXT;" in sql
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|NOT NULL)\b", sql, re.I)
    src = (ROOT / "database.py").read_text()
    assert "082_welcome_dm_text.sql" in src and '"dm_message_template": None' in src


# ---- /welcome wizard -------------------------------------------------------------------------------------------

BASE = {"enabled": True, "channel_id": 55, "message_template": "hi", "use_template": True, "delivery_mode": "dm",
        "card_theme": "wolf", "avatar_shape": "circle"}


def _items(config):
    out = []

    def walk(n):
        for c in getattr(n, "children", []) or []:
            out.append(c)
            walk(c)
    walk(vw.build_wizard_view(1, None, 9, config))
    return [c for c in out if (getattr(c, "custom_id", "") or "").startswith("welcome_wz_dmtext:")]


def test_dm_text_button_only_in_dm_mode_and_shows_custom():
    assert len(_items(BASE)) == 1
    assert _items({**BASE, "delivery_mode": "channel"}) == []
    assert "(custom)" not in _items(BASE)[0].item.label
    assert "(custom)" in _items({**BASE, "dm_message_template": "x"})[0].item.label
    assert "(custom)" not in _items({**BASE, "dm_message_template": "  "})[0].item.label


def test_registered_and_status_line():
    assert vw.WelcomeDmTextButton in vw.DYNAMIC_ITEMS
    on = "\n".join(vw.render_status_lines({**BASE, "dm_message_template": "x"}))
    assert "own DM text" in on and "own DM text" not in "\n".join(vw.render_status_lines(BASE))
    assert "own DM text" not in "\n".join(
        vw.render_status_lines({**BASE, "delivery_mode": "channel", "dm_message_template": "x"}))


def test_wizard_dm_row_stays_inside_discord_limits():
    view = vw.build_wizard_view(1, None, 9, {**BASE, "dm_fallback_to_channel": True, "dm_message_template": "x"})
    rows = [c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)]
    assert all(len(r.children) <= 5 for r in rows)
    assert sum(1 for _ in view.walk_children()) < 40


def test_button_opens_the_modal_prefilled(monkeypatch):
    seen = {}

    async def access(i, inv, gid=None): return True
    async def cfg(g, c): return {"dm_message_template": "CURRENT"}
    monkeypatch.setattr(vw, "_check_access", access)
    monkeypatch.setattr(vw, "_get_config_for_modal", cfg)

    async def send_modal(m): seen["modal"] = m
    i = SimpleNamespace(response=SimpleNamespace(send_modal=send_modal))
    run(vw.WelcomeDmTextButton(1, None, 9, BASE).callback(i))
    assert seen["modal"].template.default == "CURRENT" and seen["modal"].template.required is False


def test_button_no_access_opens_nothing(monkeypatch):
    async def access(i, inv, gid=None): return False
    monkeypatch.setattr(vw, "_check_access", access)
    i = SimpleNamespace(response=SimpleNamespace(send_modal=lambda m: (_ for _ in ()).throw(AssertionError("opened"))))
    run(vw.WelcomeDmTextButton(1, None, 9, BASE).callback(i))


def test_modal_submit_saves_and_redraws_and_empty_clears(monkeypatch):
    saved, redraws = [], []

    async def save(g, c, text): saved.append(text); return True
    async def rerender(*a, **k): redraws.append(1)
    monkeypatch.setattr(vw.db, "set_welcome_dm_text", save, raising=False)
    monkeypatch.setattr(vw, "_rerender", rerender)

    async def defer(*a, **k): pass
    for text in ("hello {member}", ""):
        m = vw.WelcomeDmTextModal(1, None, 9, "old")
        m.template._value = text
        run(m.on_submit(SimpleNamespace(response=SimpleNamespace(defer=defer))))
    assert saved == ["hello {member}", ""] and len(redraws) == 2


# ---- join-DM welcome sub-screen --------------------------------------------------------------------------------

def _sub_ids(mode, on=False):
    view = jd.build_welcome_sub_view(1, None, discord.ui.Container(), delivery_mode=mode, dm_fallback=on)
    return [c for c in view.walk_children() if (getattr(c, "custom_id", "") or "").startswith("join_dm_wsub_dmfb:")]


def test_sub_screen_switch_only_in_dm_mode_with_state():
    assert _sub_ids("channel") == []
    off, on = _sub_ids("dm")[0], _sub_ids("dm", True)[0]
    assert "(off)" in off.item.label and off.item.style == discord.ButtonStyle.secondary
    assert "(on)" in on.item.label and on.item.style == discord.ButtonStyle.success


def test_sub_screen_registered_and_every_rebuild_passes_the_flag():
    assert jd._WelcomeDmFallbackButton in jd.DYNAMIC_ITEMS
    src = (ROOT / "discord_bot/cogs/_views_join_dm.py").read_text()
    calls = re.findall(r"build_welcome_sub_view\(.*?\)\n", src)
    rebuilds = [c for c in calls if "self.guild_id" in c]
    assert len(rebuilds) >= 3 and all("dm_fallback" in c for c in rebuilds)


@pytest.fixture
def sub_click(monkeypatch):
    st = SimpleNamespace(cfg={"delivery_mode": "dm", "channel_id": 55}, saved=[], edits=[], notes=[])

    async def get_cfg(g, clone_id=None): return st.cfg
    async def save(g, c, on): st.saved.append(on); return True
    async def preview(guild, cfg, user): return discord.ui.Container(), object()
    monkeypatch.setattr(jd.db, "get_welcome_config", get_cfg)
    monkeypatch.setattr(jd.db, "set_welcome_dm_fallback", save, raising=False)
    monkeypatch.setattr(jd, "_welcome_preview_container", preview)

    async def defer(*a, **k): pass
    async def edit(**kw): st.edits.append(kw)
    async def note(text, ephemeral=False): st.notes.append(text)

    def press():
        i = SimpleNamespace(client=SimpleNamespace(get_guild=lambda g: None), user=None,
                            response=SimpleNamespace(defer=defer), edit_original_response=edit,
                            followup=SimpleNamespace(send=note))
        run(jd._WelcomeDmFallbackButton(1, None).callback(i))
    st.press = press
    return st


def test_sub_screen_tap_turns_on_then_off(sub_click):
    sub_click.press()
    assert sub_click.saved == [True] and len(sub_click.edits) == 1
    sub_click.cfg["dm_fallback_to_channel"] = True
    sub_click.press()
    assert sub_click.saved == [True, False]


def test_sub_screen_needs_a_channel_to_turn_on(sub_click):
    sub_click.cfg["channel_id"] = None
    sub_click.press()
    assert sub_click.saved == [] and sub_click.edits == [] and "channel" in sub_click.notes[0]
