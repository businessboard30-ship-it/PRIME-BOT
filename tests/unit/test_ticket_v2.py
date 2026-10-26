import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules import ticket_panel as tp
from discord_bot.cogs import _views_ticket_panel as pv
from discord_bot.cogs import ticket as tk
from discord_bot.cogs import _views_ticket_wizard as wz


def run(c):
    return asyncio.run(c)


def guild(icon=True):
    ic = SimpleNamespace(url="https://cdn.discordapp.com/icons/1/x.png") if icon else None
    return SimpleNamespace(id=1, name="BETA SCRIPTS", icon=ic)


def kinds(view):
    return [type(c).__name__ for c in view.walk_children()]


def test_categories_parse_with_stable_keys_and_limits():
    cats = tp.parse_categories(json.dumps(tp.PRESET_CATEGORIES))
    assert [c["key"] for c in cats][:3] == ["general-support", "member-report", "staff-appeal"]
    assert tp.parse_categories("not json") == [] and tp.parse_categories(None) == []
    many = json.dumps([{"label": f"Cat {i}"} for i in range(30)])
    assert len(tp.parse_categories(many)) == tp.MAX_CATEGORIES
    assert tp.find_category(json.dumps(tp.PRESET_CATEGORIES), "technical-help")["label"] == "Technical help"


def test_panel_look_is_custom_only_for_premium():
    cfg = {"panel_title": "Help desk", "panel_text": "Hi", "panel_color": "#FF0000"}
    assert tp.panel_look(cfg, True) == {"title": "Help desk", "text": "Hi", "color": 0xFF0000}
    assert tp.panel_look(cfg, False)["title"] == tp.DEFAULT_TITLE
    assert tp.parse_color("zzz") is None and tp.parse_color("5865f2") == 0x5865F2


def test_free_panel_shows_server_icon_and_one_open_button_no_dropdown():
    cfg = {"categories_json": json.dumps(tp.PRESET_CATEGORIES)}
    view = pv.build_panel_view(guild(), cfg, premium=False)
    k = kinds(view)
    assert "Thumbnail" in k and "Select" not in k
    ids = [c.custom_id for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert ids == ["ticket:open"]


def test_premium_panel_has_category_dropdown_with_descriptions_and_link_buttons():
    cfg = {"categories_json": json.dumps(tp.PRESET_CATEGORIES),
           "custom_buttons_json": json.dumps([{"label": "Rules", "url": "https://example.com/rules"}])}
    view = pv.build_panel_view(guild(), cfg, premium=True)
    sel = [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]
    assert len(sel) == 1 and sel[0].custom_id == "ticket:category" and sel[0].placeholder == "Choose how we can help"
    assert [o.label for o in sel[0].options][:2] == ["General support", "Member report"]
    assert sel[0].options[0].description == "Questions or help using the server"
    assert any(isinstance(c, discord.ui.Button) and c.url for c in view.walk_children())
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "BETA SCRIPTS" in text and "Support desk" in text


def test_panel_without_a_server_icon_still_builds():
    view = pv.build_panel_view(guild(icon=False), {}, premium=False)
    assert "Thumbnail" not in kinds(view)


def test_ticket_message_has_claim_close_topic_and_icon():
    opener = SimpleNamespace(display_name="Max", mention="<@5>")
    cat = tp.parse_categories(json.dumps(tp.PRESET_CATEGORIES))[3]
    view = pv.ticket_view(opener, guild(), "Hello <@5>", cat, "my bot is offline", "<@&9>")
    ids = [c.custom_id for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert ids == ["ticket:claim", "ticket:close"]
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "Technical help" in text and "my bot is offline" in text and "<@&9>" in text


def test_inactivity_rule_and_channel_names():
    assert tp.is_inactive(0, 3600 * 24, 24) and not tp.is_inactive(0, 3600 * 23, 24) and not tp.is_inactive(0, 10**9, None)
    cat = tp.parse_categories(json.dumps(tp.PRESET_CATEGORIES))[0]
    assert tp.ticket_channel_name("Max D", cat) == "general-support-max-d" and tp.ticket_channel_name("Max") == "ticket-max"


def test_persistent_layout_and_wizard_registration():
    layout = pv.TicketPanelLayout(SimpleNamespace())
    assert {c.custom_id for c in layout.walk_children() if hasattr(c, "custom_id")} >= {"ticket:category", "ticket:open"}
    assert wz.TicketPanelLookButton in wz.DYNAMIC_ITEMS and wz.TicketPresetButton in wz.DYNAMIC_ITEMS


def test_category_pick_without_premium_opens_a_plain_ticket(monkeypatch):
    cog = tk.TicketCog.__new__(tk.TicketCog)
    cog.bot = SimpleNamespace(clone_id=None)
    monkeypatch.setattr(tk.db, "get_ticket_config", AsyncMock(return_value={"categories_json": json.dumps(tp.PRESET_CATEGORIES)}))
    monkeypatch.setattr(tk.db, "is_guild_premium_active", AsyncMock(return_value=False))
    created = AsyncMock()
    monkeypatch.setattr(cog, "create_ticket", created)
    monkeypatch.setattr(cog, "_reset_panel", AsyncMock())
    it = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2))
    run(cog.handle_category(it, "technical-help"))
    created.assert_awaited_once_with(it, None, None)


def test_premium_category_pick_opens_the_question_modal(monkeypatch):
    cog = tk.TicketCog.__new__(tk.TicketCog)
    cog.bot = SimpleNamespace(clone_id=None)
    monkeypatch.setattr(tk.db, "get_ticket_config", AsyncMock(return_value={"categories_json": json.dumps(tp.PRESET_CATEGORIES)}))
    monkeypatch.setattr(tk.db, "is_guild_premium_active", AsyncMock(return_value=True))
    monkeypatch.setattr(tk.db, "get_open_ticket_for_opener", AsyncMock(return_value=None))
    monkeypatch.setattr(cog, "_reset_panel", AsyncMock())
    sent = {}
    async def send_modal(m): sent["m"] = m
    it = SimpleNamespace(guild_id=1, user=SimpleNamespace(id=2), response=SimpleNamespace(send_modal=send_modal))
    run(cog.handle_category(it, "technical-help"))
    assert isinstance(sent["m"], tk.IntakeModal) and sent["m"].category["label"] == "Technical help"


def test_set_ticket_config_now_saves_the_premium_fields(monkeypatch):
    import database
    captured = {}

    class Conn:
        async def fetchrow(self, sql, *args):
            if "INSERT INTO discord_ticket_config" in sql:
                captured["sql"], captured["args"] = sql, args
                return {"guild_id": 1}
            return None

    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def fake_pool(): return Pool()
    monkeypatch.setattr(database, "get_pool", fake_pool)
    d = database.Database.__new__(database.Database)
    run(d.set_ticket_config(1, None, categories_json="[]", auto_close_hours=24, transcript_channel_id=7,
                            custom_close_message="bye", panel_title="Help", panel_color="#fff000"))
    sql = captured["sql"]
    for col in ("categories_json", "auto_close_hours", "transcript_channel_id", "custom_close_message", "panel_title", "panel_color"):
        assert col in sql
    assert 24 in captured["args"] and 7 in captured["args"] and "bye" in captured["args"]


def test_autoclose_pass_closes_only_idle_tickets_of_premium_servers(monkeypatch):
    import time
    cog = tk.TicketCog.__new__(tk.TicketCog)
    now = time.time()

    def sf(ts):   # a Discord snowflake for a unix time
        return (int(ts * 1000) - 1420070400000) << 22

    sent = AsyncMock()
    idle = SimpleNamespace(id=10, last_message_id=sf(now - 5 * 3600), created_at=None, send=sent)
    fresh = SimpleNamespace(id=11, last_message_id=sf(now - 600), created_at=None, send=sent)
    g = SimpleNamespace(id=1, get_channel=lambda cid: {10: idle, 11: fresh}.get(cid))
    cog.bot = SimpleNamespace(clone_id=None, get_guild=lambda gid: g)
    monkeypatch.setattr(tk.db, "list_open_tickets", AsyncMock(return_value=[
        {"guild_id": 1, "channel_id": 10, "opener_id": 5}, {"guild_id": 1, "channel_id": 11, "opener_id": 6}]))
    monkeypatch.setattr(tk.db, "is_guild_premium_active", AsyncMock(return_value=True))
    monkeypatch.setattr(tk.db, "get_ticket_config", AsyncMock(return_value={"auto_close_hours": 2}))
    closed = AsyncMock()
    monkeypatch.setattr(cog, "close_ticket", closed)
    assert run(cog.autoclose_pass()) == 1
    assert closed.await_args.args[1] is idle
    # not premium: nothing closes
    closed.reset_mock()
    monkeypatch.setattr(tk.db, "is_guild_premium_active", AsyncMock(return_value=False))
    assert run(cog.autoclose_pass()) == 0
    closed.assert_not_awaited()
