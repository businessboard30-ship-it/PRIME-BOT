"""Compact quick-start message + panel parallel-read tests."""
import asyncio
import importlib
import re
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.unit.test_server_panel_phase1 import (
    GUILD_ID, OWNER, interaction, make_db, run,
)

QP = "discord_bot.cogs._views_quickstart_pointer"
SP = "modules.server_panel"


def walk(view):
    out = []
    for c in view.walk_children():
        out.append(c)
    return out


def labels(view):
    return [getattr(c, "label", None) or getattr(getattr(c, "item", None), "label", None)
            for c in walk(view) if isinstance(c, (discord.ui.Button, discord.ui.DynamicItem))]


def texts(view):
    return "\n".join(c.content for c in walk(view) if isinstance(c, discord.ui.TextDisplay))


def test_guild_copy_has_panel_and_guide_buttons():
    qp = importlib.import_module(QP)
    v = qp.QuickstartPointerView(GUILD_ID, 7, guild_name="Test Guild", dm=False)
    assert "/serversetup" in texts(v)
    assert "Test Guild" in texts(v)
    ls = labels(v)
    assert "Open server panel" in ls and "Full setup guide" in ls


def test_dm_copy_has_link_not_panel_button():
    qp = importlib.import_module(QP)
    v = qp.QuickstartPointerView(GUILD_ID, 7, guild_name="G", jump_url="https://discord.com/channels/1/2", dm=True)
    ls = labels(v)
    assert "Go to server" in ls and "Open server panel" not in ls
    assert "Full setup guide" in ls
    link = next(c for c in walk(v) if isinstance(c, discord.ui.Button) and c.style == discord.ButtonStyle.link)
    assert link.url == "https://discord.com/channels/1/2"


def test_message_is_short():
    qp = importlib.import_module(QP)
    v = qp.QuickstartPointerView(GUILD_ID, None, guild_name="G", dm=False)
    assert len(texts(v)) < 200
    assert sum(isinstance(c, discord.ui.TextDisplay) for c in walk(v)) == 1


def test_invite_offer_still_shown_when_requested():
    qp = importlib.import_module(QP)
    jd = types.ModuleType("discord_bot.cogs._views_join_dm")
    class _Btn(discord.ui.Button):
        def __init__(self, action, g, c):
            super().__init__(label=action, custom_id=f"join_dm_offer:invite:{action}:{g}:-")
    jd._JoinOfferInviteButton = _Btn
    import sys
    sys.modules["discord_bot.cogs._views_join_dm"] = jd
    try:
        v = qp.QuickstartPointerView(GUILD_ID, None, guild_name="G", dm=True, join_offer={"show_invite": True})
        ls = labels(v)
        assert "allow" in ls and "decline" in ls
    finally:
        sys.modules.pop("discord_bot.cogs._views_join_dm", None)


def test_custom_ids_match_templates():
    qp = importlib.import_module(QP)
    for cls, prefix in ((qp.OpenPanelButton, "join_dm_panel"), (qp.FullGuideButton, "join_dm_full")):
        for clone in (None, 12):
            b = cls(GUILD_ID, clone)
            m = re.match(cls.__discord_ui_compiled_template__.pattern, b.item.custom_id)
            assert m and b.item.custom_id.startswith(prefix)
            assert int(m.group(1)) == GUILD_ID


def test_panel_button_in_dm_does_not_open_panel():
    qp = importlib.import_module(QP)
    b = qp.OpenPanelButton(GUILD_ID, None)
    it = interaction(user_id=OWNER) if "user_id" in interaction.__code__.co_varnames else interaction()
    it.guild = None
    it.response.send_message = AsyncMock()
    run(b.callback(it))
    it.response.send_message.assert_awaited_once()
    assert "/serversetup" in it.response.send_message.await_args.args[0]


def test_panel_button_wrong_server_refused():
    qp = importlib.import_module(QP)
    b = qp.OpenPanelButton(GUILD_ID + 1, None)
    it = interaction()
    it.response.send_message = AsyncMock()
    run(b.callback(it))
    assert "different server" in it.response.send_message.await_args.args[0]


def test_followup_nudge_is_one_short_embed():
    import sys
    cfg = types.ModuleType("config"); cfg.PREMIUM_FEE_USD = 5
    sys.modules.setdefault("config", cfg)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    sc.scan_missing_channels = sc.build_suggestions_embed = sc.SetupSuggestView = MagicMock()
    sys.modules["discord_bot.cogs.setup_channels"] = sc
    dbm = types.ModuleType("database"); dbm.db = MagicMock()
    sys.modules["database"] = dbm
    sys.modules.pop("discord_bot.cogs.quickstart", None)
    qs = importlib.import_module("discord_bot.cogs.quickstart")
    cog = qs.QuickstartCog.__new__(qs.QuickstartCog)
    e = cog._build_embed(SimpleNamespace(name="G"), intro="Quick nudge")
    assert not e.fields and "/serversetup" in e.description
    sys.modules.pop("discord_bot.cogs.quickstart", None)


def test_setup_items_reads_run_in_parallel(monkeypatch):
    """8 reads of 0.1s each must finish in well under the 0.8s a sequential run needs."""
    sp = importlib.import_module(SP)
    dbm = types.ModuleType("database")
    db = make_db()
    async def slow(*a, **k):
        await asyncio.sleep(0.1)
        return {}
    for name in ("get_welcome_config", "get_verification_config", "get_honeypot_config",
                 "get_automod_config", "get_leveling_config", "get_ticket_config"):
        setattr(db, name, slow)
    async def slow_bool(*a, **k):
        await asyncio.sleep(0.1)
        return False
    db.is_guild_premium_active = slow_bool
    dbm.db = db
    monkeypatch.setitem(__import__("sys").modules, "database", dbm)
    sc = types.ModuleType("discord_bot.cogs.setup_channels")
    async def scan(guild, cid):
        await asyncio.sleep(0.1)
        return []
    sc.scan_missing_channels = scan
    monkeypatch.setitem(__import__("sys").modules, "discord_bot.cogs.setup_channels", sc)
    t = time.perf_counter()
    items = run(sp.setup_items(SimpleNamespace(id=GUILD_ID), 7))
    took = time.perf_counter() - t
    assert len(items) == 8
    assert took < 0.4, f"setup_items took {took:.2f}s (sequential would be ~0.7s)"
