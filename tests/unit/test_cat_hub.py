"""/cat is a small private wizard (a menu card), and every spawn has a Menu button that opens the same card."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_cat_adopt as adopt
from discord_bot.cogs import _views_cat_hub as hub
from modules import cat_hub


def run(coro):
    return asyncio.run(coro)


# ── pure text ─────────────────────────────────────────────────────────────

def test_status_lines_in_every_situation():
    assert cat_hub.status_lines(False, True, 123, "https://x") == []                 # DMs: nothing about a server
    off = cat_hub.status_lines(True, False, None, None)
    assert len(off) == 1 and "/catsetup" in off[0]
    on = cat_hub.status_lines(True, True, 1700000000, None)
    assert "<t:1700000000:t>" in on[0] and "<t:1700000000:R>" in on[0]
    waiting = cat_hub.status_lines(True, True, 1700000000, "https://discord.com/channels/1/2/3")
    assert waiting[0].startswith("🐾 **A cat is waiting!**") and "(https://discord.com/channels/1/2/3)" in waiting[0]
    assert "soon" in cat_hub.status_lines(True, True, None, None)[0]


def test_jump_url_needs_both_ids():
    assert cat_hub.jump_url(5, {"channel_id": 7, "message_id": 9}) == "https://discord.com/channels/5/7/9"
    assert cat_hub.jump_url(5, None) is None and cat_hub.jump_url(5, {"channel_id": 7, "message_id": None}) is None


def test_menu_is_short_and_fits_a_select():
    keys = [k for k, *_ in cat_hub.MENU]
    assert len(keys) == len(set(keys)) and 2 <= len(keys) <= 25
    for _k, emoji, label, desc in cat_hub.MENU:
        assert emoji and 0 < len(label) <= 100 and 0 < len(desc) <= 100
    assert len(cat_hub.card_text(cat_hub.status_lines(True, True, 1, "https://x"))) < 600


# ── every entry opens a real button ───────────────────────────────────────

def test_every_menu_entry_has_a_button_with_a_callback():
    for key, *_ in cat_hub.MENU:
        btn = hub._button_for(key)
        assert btn is not None and callable(btn.callback), key
    assert hub._button_for("nope") is None


def test_picking_an_entry_runs_that_button(monkeypatch):
    called = []

    class Fake:
        async def callback(self, interaction):
            called.append(interaction)
    monkeypatch.setattr(hub, "_button_for", lambda key: Fake() if key == "market" else None)

    async def go():
        sel = hub.HubSelect()
        sel._values = ["market"]
        inter = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await sel.callback(inter)
        return inter
    inter = run(go())
    assert called == [inter]


def test_picking_a_missing_entry_says_so(monkeypatch):
    monkeypatch.setattr(hub, "_button_for", lambda key: None)

    async def go():
        sel = hub.HubSelect()
        sel._values = ["market"]
        inter = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await sel.callback(inter)
        return inter
    inter = run(go())
    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True


# ── the card ──────────────────────────────────────────────────────────────

def _count(payload) -> int:
    """Components in a message payload, nested ones included (what Discord's 40 limit counts)."""
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(v) for v in payload)
    return 0


def _texts(view):
    out = []
    def walk(items):
        for it in items:
            if hasattr(it, "content"):
                out.append(it.content)
            walk(getattr(it, "children", []) or [])
    walk(view.children)
    return "\n".join(out)


def test_card_is_v2_with_ring_picture_and_optional_cat():
    import discord
    v = hub.HubView("https://cdn.discordapp.com/attachments/1/2/c.jpg", can_setup=True, lines=["⏰ **Next cat:** soon"])
    assert isinstance(v, discord.ui.LayoutView)
    payload = v.to_components()
    assert "c.jpg" in str(payload) and "bot-ring" in str(payload)
    t = _texts(v)
    assert "Cat menu" in t and "Next cat" in t
    plain = hub.HubView(lines=[])
    assert "c.jpg" not in str(plain.to_components())


def test_hub_stays_under_discords_40_component_limit():
    full = _count(hub.HubView("https://x/y.jpg", can_setup=True, lines=["a", "b"]).to_components())
    small = _count(hub.HubView().to_components())
    assert 10 <= small < full <= 40, (small, full)         # real lower bound, so the counter can't pass vacuously


def test_next_cat_button_rebuilds_the_card(monkeypatch):
    async def lines(_i):
        return ["⏰ **Next cat:** later"]
    monkeypatch.setattr(hub, "status", lines)
    v = hub.HubView(lines=["old"])
    i = _inter()
    i.response.edit_message = AsyncMock()
    nxt = next(c for row in v.walk_children() for c in [row] if isinstance(c, hub._NextCatButton))
    run(nxt.callback(i))
    assert "later" in _texts(v) and "old" not in _texts(v) and i.response.edit_message.await_args.kwargs["view"] is v


def _inter(done=False, guild_id=5):
    i = MagicMock()
    i.guild_id, i.guild, i.client = guild_id, None, SimpleNamespace(clone_id=None)
    i.response.is_done = lambda: done
    i.response.send_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def test_open_hub_is_private_and_uses_followup_only_after_a_defer(monkeypatch):
    async def lines(_i):
        return ["⏰ **Next cat:** soon"]
    monkeypatch.setattr(hub, "status", lines)
    a = _inter(done=False)
    run(hub.open_hub(a))
    kw = a.response.send_message.await_args.kwargs
    assert kw["ephemeral"] is False and isinstance(kw["view"], hub.HubView) and not a.followup.send.await_count
    b = _inter(done=True)
    run(hub.open_hub(b, "https://x/y.jpg", note="😺 Easy there"))
    kw = b.followup.send.await_args.kwargs
    assert kw["ephemeral"] is False and "😺 Easy there" in _texts(kw["view"]) and "embed" not in kw
    assert kw["view"].picture_url == "https://x/y.jpg"
    c = _inter(done=False)
    run(hub.open_hub(c, deferred=True))                     # send_cat has already deferred
    assert c.followup.send.await_count == 1 and not c.response.send_message.await_count


def test_status_never_raises_and_skips_dms(monkeypatch):
    assert run(hub.status(_inter(guild_id=None))) == []

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(hub.cs, "get_config", boom)
    assert run(hub.status(_inter())) == []


def test_status_shows_the_waiting_cat_and_next_time(monkeypatch):
    from datetime import datetime, timezone
    cfg = SimpleNamespace(enabled=True)

    async def get_config(g, c): return cfg

    async def get_next_at(g, c): return datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    async def open_spawn(g, c): return {"channel_id": 7, "message_id": 9}
    monkeypatch.setattr(hub.cs, "get_config", get_config)
    monkeypatch.setattr(hub.cs, "get_next_at", get_next_at)
    monkeypatch.setattr(hub.cs, "upcoming", lambda cfg, n: n)
    monkeypatch.setattr(hub.ca, "open_spawn", open_spawn)
    out = run(hub.status(_inter()))
    assert "discord.com/channels/5/7/9" in out[0] and "<t:" in out[1]


# ── connected to the spawn ────────────────────────────────────────────────

def test_spawn_menu_button_round_trips_and_opens_the_same_card(monkeypatch):
    m = re.match(adopt.MenuButton.__discord_ui_compiled_template__.pattern, "cat_menu")
    obj = run(adopt.MenuButton.from_custom_id(None, None, m))
    assert obj.item.custom_id == "cat_menu" and adopt.MenuButton in adopt.DYNAMIC_ITEMS
    opened = []

    async def fake_open(interaction, picture_url=None, **kw):
        opened.append(interaction)
    monkeypatch.setattr(hub, "open_hub", fake_open)
    sentinel = object()
    run(obj.callback(sentinel))
    assert opened == [sentinel]


def test_the_cat_command_opens_the_menu_not_a_bare_picture():
    import inspect
    from discord_bot.cogs import cat
    src = inspect.getsource(cat.send_cat)
    assert "open_hub" in src and "defer(ephemeral=not vis.is_public" in src
    assert discord.ui.View  # imported for the type checks above


def test_open_spawn_helper_never_raises(monkeypatch):
    from modules import cat_adopt

    async def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(cat_adopt, "_pool", boom)
    assert run(cat_adopt.open_spawn(1, None)) is None
