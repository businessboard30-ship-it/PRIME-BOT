"""Owner-panel kill switch for clone registration."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import DISCORD_CLONE_ADMIN_IDS
from discord_bot.cogs import clone_admin

OWNER = next(iter(DISCORD_CLONE_ADMIN_IDS)) if DISCORD_CLONE_ADMIN_IDS else 999999
USER = 424242


def run(c):
    return asyncio.run(c)


class _Lazy:
    """Resolve modules.admin_controls the way the code under test does (a fresh
    `from modules import admin_controls` per use). Other test files re-import that
    module, so a reference captured at import time here could be a stale copy."""

    def __getattr__(self, name):
        from modules import admin_controls
        return getattr(admin_controls, name)


ac = _Lazy()


def _patch_ac(monkeypatch, name, value):
    from modules import admin_controls
    monkeypatch.setattr(admin_controls, name, value)


def engage(monkeypatch, *switches):
    async def fake_switches():
        return set(switches)
    _patch_ac(monkeypatch, "current_switches", fake_switches)


def test_switch_is_registered_and_covers_registerclone():
    label, roots = ac.FEATURES["clone_registration"]
    assert label == "Clone registration" and "registerclone" in roots


def test_slash_gate_refuses_registerclone_for_non_owners(monkeypatch):
    _patch_ac(monkeypatch, "_snapshot", {"ts": time.monotonic(), "ok": True,
                                         "switches": {"clone_registration"}, "users": set(), "guilds": set()})
    cmd = lambda name: SimpleNamespace(user=SimpleNamespace(id=USER), guild_id=None,
                                       command=SimpleNamespace(qualified_name=name))
    assert "Clone registration" in run(ac.block_reason(cmd("registerclone")))
    assert run(ac.block_reason(cmd("rank"))) is None                       # nothing else is switched off
    owner = SimpleNamespace(user=SimpleNamespace(id=OWNER), guild_id=None,
                            command=SimpleNamespace(qualified_name="registerclone"))
    assert run(ac.block_reason(owner)) is None


def _interaction():
    i = MagicMock()
    i.followup.send = AsyncMock()
    return i


def _spy_db(monkeypatch):
    db = MagicMock()
    db.get_discord_clones_by_owner = AsyncMock(return_value=[])
    monkeypatch.setattr(clone_admin, "db", db)
    return db


def test_build_bot_wizard_path_is_blocked_too(monkeypatch):
    """register_clone_token is what BOTH /registerclone and the Build Bot button call."""
    engage(monkeypatch, "clone_registration")
    db = _spy_db(monkeypatch)
    validate = AsyncMock()
    monkeypatch.setattr(clone_admin, "validate_bot_token", validate)
    i = _interaction()
    run(clone_admin.register_clone_token(i, "tok", owner_id=USER))
    assert "temporarily turned off" in i.followup.send.await_args.args[0]
    assert i.followup.send.await_args.kwargs["ephemeral"] is True
    db.get_discord_clones_by_owner.assert_not_called()                      # nothing happened past the guard
    validate.assert_not_called()                                           # the token was never even checked


def test_returning_owners_cannot_relink_through_registration_either(monkeypatch):
    engage(monkeypatch, "clone_registration")
    db = _spy_db(monkeypatch)
    db.get_discord_clones_by_owner = AsyncMock(return_value=[{"clone_id": 1}])
    relink = AsyncMock()
    monkeypatch.setattr(clone_admin, "relink_clone_token", relink)
    run(clone_admin.register_clone_token(_interaction(), "tok", owner_id=USER))
    relink.assert_not_called()


def test_owners_can_still_register_while_it_is_off(monkeypatch):
    engage(monkeypatch, "clone_registration")
    _spy_db(monkeypatch)
    monkeypatch.setattr(clone_admin, "validate_bot_token", AsyncMock(return_value={"ok": False, "error": "bad token"}))
    i = _interaction()
    run(clone_admin.register_clone_token(i, "tok", owner_id=OWNER))
    assert "bad token" in i.followup.send.await_args.args[0]               # got past the guard to validation


def test_registration_proceeds_normally_when_the_switch_is_off(monkeypatch):
    engage(monkeypatch)                                                     # nothing engaged
    _spy_db(monkeypatch)
    monkeypatch.setattr(clone_admin, "validate_bot_token", AsyncMock(return_value={"ok": False, "error": "bad token"}))
    i = _interaction()
    run(clone_admin.register_clone_token(i, "tok", owner_id=USER))
    assert "bad token" in i.followup.send.await_args.args[0]


def test_other_switches_do_not_block_registration(monkeypatch):
    engage(monkeypatch, "ai", "economy")
    _spy_db(monkeypatch)
    monkeypatch.setattr(clone_admin, "validate_bot_token", AsyncMock(return_value={"ok": False, "error": "bad token"}))
    i = _interaction()
    run(clone_admin.register_clone_token(i, "tok", owner_id=USER))
    assert "bad token" in i.followup.send.await_args.args[0]


def test_admin_panel_shows_a_toggle_for_it():
    from discord_bot.cogs._views_admin_panel_controls import ControlsView
    view = ControlsView(MagicMock(), OWNER)
    view.engaged = {"clone_registration"}
    body = "\n".join(view.body())
    assert "⛔ OFF — Clone registration" in body and "Build Bot" in body
    labels = [getattr(b, "label", "") for b in view.controls()]
    assert any(l.startswith("Clone registration: OFF") for l in labels)
    view.engaged = set()
    assert any(l.startswith("Clone registration: on") for l in
               [getattr(b, "label", "") for b in view.controls()])
