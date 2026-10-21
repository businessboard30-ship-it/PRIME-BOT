"""Welcome Card Studio Phase 6a: "if their DMs are closed, post in the channel instead" (off by default, free)."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from discord_bot.cogs import _views_welcome as vw
from discord_bot.cogs import welcome as w

ROOT = Path(__file__).resolve().parents[2]


def run(coro):
    return asyncio.run(coro)


def _resp(status):
    return SimpleNamespace(status=status, reason="x")


def forbidden():
    return discord.Forbidden(_resp(403), "no")


def closed_400():
    return discord.HTTPException(_resp(400), {"code": 50007, "message": "Cannot send messages to this user"})


def server_error():
    return discord.HTTPException(_resp(500), {"code": 0, "message": "oops"})


# ---- _dm_is_closed ---------------------------------------------------------------------------------------------

def test_closed_means_forbidden_or_50007_only():
    assert w._dm_is_closed(forbidden())
    assert w._dm_is_closed(closed_400())
    assert not w._dm_is_closed(server_error())
    assert not w._dm_is_closed(discord.HTTPException(_resp(429), {"code": 0, "message": "slow down"}))
    assert not w._dm_is_closed(ValueError("x"))


# ---- _post_dm_fallback -----------------------------------------------------------------------------------------

class FakeChannel:
    def __init__(self, boom=None):
        self.sent, self.boom = [], boom

    async def send(self, content=None, file=None):
        if self.boom:
            raise self.boom
        self.sent.append((content, file))


def member_with(channel, ch_id=55):
    guild = SimpleNamespace(id=1, get_channel=lambda cid: channel if cid == ch_id else None)
    return SimpleNamespace(id=2, guild=guild)


def test_fallback_posts_same_text_and_card_in_the_channel():
    ch = FakeChannel()
    ok = run(w._post_dm_fallback(member_with(ch), {"channel_id": 55}, b"PNGDATA", "png", "Welcome!"))
    assert ok is True
    (content, file), = ch.sent
    assert content == "Welcome!" and file.filename == "welcome.png" and file.fp.read() == b"PNGDATA"


def test_fallback_without_a_usable_channel_does_nothing_and_never_raises():
    ch = FakeChannel()
    assert run(w._post_dm_fallback(member_with(ch), {"channel_id": None}, b"x", "png", "t")) is False
    assert run(w._post_dm_fallback(member_with(ch), {}, b"x", "png", "t")) is False
    assert run(w._post_dm_fallback(member_with(ch), {"channel_id": 999}, b"x", "png", "t")) is False   # channel gone
    assert run(w._post_dm_fallback(member_with(ch), {"channel_id": "abc"}, b"x", "png", "t")) is False  # junk id
    assert ch.sent == []


def test_fallback_post_failure_is_swallowed():
    for err in (forbidden(), server_error()):
        ch = FakeChannel(boom=err)
        assert run(w._post_dm_fallback(member_with(ch), {"channel_id": 55}, b"x", "png", "t")) is False


# ---- the real join, DM mode ------------------------------------------------------------------------------------

class FakeDM:
    def __init__(self, boom=None):
        self.boom, self.sent = boom, []

    async def send(self, content=None, file=None):
        if self.boom:
            raise self.boom
        self.sent.append(content)


class FakeSession:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    def get(self, *a, **k):
        class R:
            async def __aenter__(s): return s
            async def __aexit__(s, *x): return False
            async def read(s): return b"avatar"
        return R()


@pytest.fixture
def join(monkeypatch):
    st = SimpleNamespace(dm_boom=None, channel=FakeChannel(), cfg=None, dms=[], log=[])

    async def get_cfg(guild_id, clone_id=None):
        return st.cfg
    monkeypatch.setattr(w.db, "get_welcome_config", get_cfg)
    monkeypatch.setattr(w.aiohttp, "ClientSession", FakeSession)

    async def sticker(session, url): return None
    async def gallery(session, cfg, bot, n): return None
    async def gate(cfg): return cfg.get("ultra_card_json")
    monkeypatch.setattr(w, "_fetch_sticker_bytes", sticker)
    monkeypatch.setattr(w, "_gallery_bg_bytes_for_render", gallery)
    monkeypatch.setattr("modules.welcome_studio.options_for_render", gate)
    monkeypatch.setattr(w, "render_welcome_card", lambda *a, **k: (b"CARD", "PNG"))
    monkeypatch.setattr(w, "_apply_template", lambda t, m: "hello")

    def make(**over):
        st.cfg = {"enabled": True, "delivery_mode": "dm", "channel_id": 55, "background_color": "#000",
                  "accent_color": "#fff", "message_template": "hi", "card_style": "png", "dm_fallback_to_channel": False,
                  **over}
        dm = FakeDM(st.dm_boom)
        st.dms = dm

        async def create_dm():
            return dm
        guild = SimpleNamespace(id=1, name="G", member_count=3, me=None,
                                get_channel=lambda cid: st.channel if cid == 55 else None)
        member = SimpleNamespace(id=7, guild=guild, display_name="Sam", create_dm=create_dm,
                                 display_avatar=SimpleNamespace(replace=lambda size: SimpleNamespace(url="http://a")))
        cog = w.WelcomeCog(SimpleNamespace(clone_id=None))
        run(cog.on_member_join(member))
        return st
    st.go = make
    return st


def test_dm_works_nothing_goes_to_the_channel_even_with_fallback_on(join):
    st = join.go(dm_fallback_to_channel=True)
    assert st.dms.sent == ["hello"] and st.channel.sent == []


def test_dm_closed_with_fallback_on_posts_once_in_the_channel(join):
    join.dm_boom = closed_400()
    st = join.go(dm_fallback_to_channel=True)
    assert len(st.channel.sent) == 1 and st.channel.sent[0][0] == "hello"


def test_dm_forbidden_with_fallback_on_also_falls_back(join):
    join.dm_boom = forbidden()
    st = join.go(dm_fallback_to_channel=True)
    assert len(st.channel.sent) == 1


def test_dm_closed_with_fallback_off_is_unchanged_nothing_posted(join):
    join.dm_boom = closed_400()
    st = join.go(dm_fallback_to_channel=False)
    assert st.channel.sent == []


def test_missing_column_value_behaves_as_off(join):
    join.dm_boom = closed_400()
    st = join.go()
    st.cfg.pop("dm_fallback_to_channel", None)
    assert st.channel.sent == []


def test_a_server_error_is_not_closed_so_no_surprise_channel_post(join):
    join.dm_boom = server_error()
    st = join.go(dm_fallback_to_channel=True)
    assert st.channel.sent == []


def test_fallback_on_but_no_channel_set_does_nothing_and_does_not_crash(join):
    join.dm_boom = closed_400()
    st = join.go(dm_fallback_to_channel=True, channel_id=None)
    assert st.channel.sent == []


def test_fallback_channel_send_failure_does_not_crash_or_double_post(join):
    join.dm_boom = closed_400()
    join.channel = FakeChannel(boom=forbidden())
    st = join.go(dm_fallback_to_channel=True)
    assert st.channel.sent == []


def test_channel_mode_is_untouched_by_the_flag(join):
    st = join.go(delivery_mode="channel", dm_fallback_to_channel=True)
    assert len(st.channel.sent) == 1 and st.dms.sent == []


# ---- the wizard ------------------------------------------------------------------------------------------------

def _buttons(config):
    view = vw.build_wizard_view(1, None, 9, config)
    found = []

    def walk(item):
        for ch in getattr(item, "children", []) or []:
            found.append(ch)
            walk(ch)
    walk(view)
    return found


def _ids(config):
    return [getattr(c, "custom_id", None) for c in _buttons(config)]


BASE = {"enabled": True, "channel_id": 55, "message_template": "hi", "use_template": True, "delivery_mode": "dm",
        "card_theme": "wolf", "avatar_shape": "circle"}


def test_button_only_appears_in_dm_mode():
    assert any((i or "").startswith("welcome_wz_dmfb:") for i in _ids(BASE))
    assert not any((i or "").startswith("welcome_wz_dmfb:") for i in _ids({**BASE, "delivery_mode": "channel"}))


def test_button_label_and_color_follow_the_setting():
    off = [c for c in _buttons(BASE) if (getattr(c, "custom_id", "") or "").startswith("welcome_wz_dmfb:")][0]
    on = [c for c in _buttons({**BASE, "dm_fallback_to_channel": True})
          if (getattr(c, "custom_id", "") or "").startswith("welcome_wz_dmfb:")][0]
    assert "(off)" in off.item.label and off.item.style == discord.ButtonStyle.secondary
    assert "(on)" in on.item.label and on.item.style == discord.ButtonStyle.success


def test_registered_and_id_matches_the_pattern():
    assert vw.WelcomeDmFallbackButton in vw.DYNAMIC_ITEMS
    assert re.match(vw.WelcomeDmFallbackButton.__discord_ui_compiled_template__.pattern, "welcome_wz_dmfb:1:-:9")


def test_status_line_mentions_the_fallback_only_when_on():
    off = "\n".join(vw.render_status_lines(BASE))
    on = "\n".join(vw.render_status_lines({**BASE, "dm_fallback_to_channel": True}))
    assert "if DMs are closed" not in off and "if DMs are closed" in on
    assert "if DMs are closed" not in "\n".join(
        vw.render_status_lines({**BASE, "delivery_mode": "channel", "dm_fallback_to_channel": True}))


class FakeInteraction:
    def __init__(self):
        self.followups, self.deferred = [], False
        self.response = SimpleNamespace(defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self, *a, **k): self.deferred = True
    async def _send(self, text, ephemeral=False): self.followups.append(text)


@pytest.fixture
def click(monkeypatch):
    st = SimpleNamespace(cfg=dict(BASE), saved=[], rerenders=0, access=True)

    async def access(i, inv, gid=None): return st.access
    async def get_cfg(g, clone_id=None): return st.cfg
    async def save(g, c, on): st.saved.append(on); return True
    async def rerender(*a, **k): st.rerenders += 1
    monkeypatch.setattr(vw, "_check_access", access)
    monkeypatch.setattr(vw.db, "get_welcome_config", get_cfg)
    monkeypatch.setattr(vw.db, "set_welcome_dm_fallback", save, raising=False)
    monkeypatch.setattr(vw, "_rerender", rerender)

    def press():
        btn = vw.WelcomeDmFallbackButton(1, None, 9, st.cfg)
        i = FakeInteraction()
        run(btn.callback(i))
        st.i = i
    st.press = press
    return st


def test_tap_turns_it_on_then_off(click):
    click.press()
    assert click.saved == [True] and click.rerenders == 1
    click.cfg["dm_fallback_to_channel"] = True
    click.press()
    assert click.saved == [True, False]


def test_cannot_turn_on_without_a_channel(click):
    click.cfg["channel_id"] = None
    click.press()
    assert click.saved == [] and click.rerenders == 0 and "Step 1" in click.i.followups[0]


def test_turning_off_works_even_without_a_channel(click):
    click.cfg.update(channel_id=None, dm_fallback_to_channel=True)
    click.press()
    assert click.saved == [False]


def test_no_access_changes_nothing(click):
    click.access = False
    click.press()
    assert click.saved == [] and click.rerenders == 0


# ---- database / migration --------------------------------------------------------------------------------------

def test_migration_is_additive_registered_and_off_by_default():
    sql = (ROOT / "database/migrations/081_welcome_dm_fallback.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS dm_fallback_to_channel BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE)\b", sql, re.I)
    assert "081_welcome_dm_fallback.sql" in (ROOT / "database.py").read_text()


def test_fresh_server_default_is_off():
    import database
    src = (ROOT / "database.py").read_text()
    assert '"dm_fallback_to_channel": False' in src
    assert database.db is not None


def test_setter_updates_only_its_own_column_and_reports_the_row_change(monkeypatch):
    import database
    calls = []

    class Conn:
        async def execute(self, sql, *args):
            calls.append((sql, args))
            return "UPDATE 1" if args[0] == 1 else "UPDATE 0"

    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def get_pool(): return Pool()
    monkeypatch.setattr(database, "get_pool", get_pool)
    assert run(database.db.set_welcome_dm_fallback(1, None, True)) is True
    assert run(database.db.set_welcome_dm_fallback(2, 5, False)) is False
    sql, args = calls[0]
    assert "dm_fallback_to_channel = $3" in sql and "clone_id IS NOT DISTINCT FROM $2" in sql
    assert args == (1, None, True) and calls[1][1] == (2, 5, False)
    assert "INSERT" not in sql and "delivery_mode" not in sql


def test_the_studio_gate_and_renderer_are_not_involved():
    assert "dm_fallback" not in (ROOT / "modules/welcome_card.py").read_text()
    assert "dm_fallback" not in (ROOT / "modules/welcome_studio.py").read_text()
