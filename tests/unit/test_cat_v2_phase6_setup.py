"""Phase 6: the Cat setup wizard is a Components V2 card (visuals only; settings logic is unchanged)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_cat_setup as wiz
from modules import cat_setup as cs


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


def _count(payload) -> int:
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(v) for v in payload)
    return 0


def _view(enabled=True, premium=False, channels=(5,)):
    cfg = cs.CatConfig(guild_id=1, enabled=enabled, channel_ids=tuple(channels))
    return wiz.CatSetupView(MagicMock(), SimpleNamespace(id=1), None, 42, cfg, premium)


def _texts(view):
    return [c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay)]


def test_wizard_is_a_layout_view_within_budget():
    view = _view()
    assert isinstance(view, discord.ui.LayoutView)
    n = _count(view.to_components())
    assert 15 <= n <= 40
    assert sum(len(t) for t in _texts(view)) < 4000


def test_wizard_shows_status_text_and_footer():
    texts = " ".join(_texts(_view()))
    assert "Cat setup" in texts and "Status" in texts and "Only sharp, high-resolution cats" in texts


def test_premium_wizard_still_fits():
    view = _view(premium=True, channels=(1, 2, 3))
    assert _count(view.to_components()) <= 40


def test_change_edits_with_view_only():
    view = _view()
    i = MagicMock(); i.response.edit_message = AsyncMock(); i.user.id = 42
    old = cs.save_config
    cs.save_config = AsyncMock()
    try:
        run(view._apply(i, enabled=False))
    finally:
        cs.save_config = old
    kw = i.response.edit_message.call_args.kwargs
    assert set(kw) == {"view"} and "OFF" in " ".join(_texts(kw["view"]))
    assert view.toggle.label == "Turn on"


def test_close_leaves_text_without_controls():
    view = _view()
    i = MagicMock(); i.response.edit_message = AsyncMock()
    run(view._on_close(i))
    final = i.response.edit_message.call_args.kwargs["view"]
    assert isinstance(final, discord.ui.LayoutView)
    assert not any(isinstance(c, discord.ui.ActionRow) for c in final.walk_children())
    assert "Cat setup" in " ".join(_texts(final))


def test_open_setup_sends_view_only(monkeypatch):
    monkeypatch.setattr(cs, "get_config", AsyncMock(return_value=cs.CatConfig(guild_id=1)))
    monkeypatch.setattr(cs, "is_premium", AsyncMock(return_value=False))
    ch = MagicMock(); ch.send = AsyncMock(return_value=MagicMock())
    guild = SimpleNamespace(id=1)
    run(wiz.open_setup(MagicMock(), guild, None, 42, ch))
    kw = ch.send.call_args.kwargs
    assert "embed" not in kw and "embeds" not in kw and isinstance(kw["view"], wiz.CatSetupView)
