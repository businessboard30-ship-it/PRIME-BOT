"""Bot tokens are accepted from the bot owner(s) only. No user, and no reviewer build, has any way to hand one over."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import DISCORD_CLONE_ADMIN_IDS
from discord_bot.cogs import _views_join_dm as join_dm
from discord_bot.cogs import clone_admin

ROOT = Path(__file__).resolve().parents[2]
OWNER = next(iter(DISCORD_CLONE_ADMIN_IDS)) if DISCORD_CLONE_ADMIN_IDS else 999999
USER = 424242


def run(c):
    return asyncio.run(c)


def _interaction():
    return SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))


# ── no public entry point ────────────────────────────────────────────────
def test_no_registerclone_or_relinkclone_slash_command():
    names = {c.name for c in clone_admin.CloneAdminCog.__cog_app_commands__}
    assert "registerclone" not in names and "relinkclone" not in names
    src = (ROOT / "discord_bot/cogs/clone_admin.py").read_text(encoding="utf-8")
    assert 'name="registerclone"' not in src and 'name="relinkclone"' not in src


def test_join_dm_has_no_build_bot_button_and_no_token_form():
    assert "build_bot" not in join_dm.FEATURE_TOGGLES
    src = (ROOT / "discord_bot/cogs/_views_join_dm.py").read_text(encoding="utf-8")
    assert "_BuildBotTokenModal" not in src and "_start_build_bot_wizard" not in src
    assert "send_modal" not in src[src.index("class _BuildBotPasteButton"):src.index("class _BuildBotPasteButton") + 2500]


def test_no_text_input_anywhere_asks_for_a_bot_token():
    for path in list((ROOT / "discord_bot").rglob("*.py")) + list((ROOT / "modules").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"TextInput\((.*?)\)", text, re.S):
            assert "token" not in m.group(1).lower(), f"{path} asks for a token"


def test_no_owner_switch_can_reopen_it():
    from modules import admin_controls as ac
    assert "build_bot_public" not in ac.OPT_IN and "clone_registration" not in ac.FEATURES
    assert not hasattr(ac, "build_bot_open") and not hasattr(ac, "build_bot_open_cached")


def test_help_manual_and_website_no_longer_advertise_it():
    for rel in ("discord_bot/cogs/help.py", "modules/command_reference.py", "app/manual/data.ts",
                "website/commands/index.html", "website/features/index.html", "website/pricing/index.html"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "registerclone" not in text and "Discord Clone activation" not in text, rel


# ── even if something called the functions, only the owner gets through ──
@pytest.mark.parametrize("fn,args", [("register_clone_token", ()), ("relink_clone_token", ())])
def test_functions_refuse_everyone_but_the_owner_before_touching_the_token(monkeypatch, fn, args):
    validate = AsyncMock()
    monkeypatch.setattr(clone_admin, "validate_bot_token", validate)
    i = _interaction()
    if fn == "register_clone_token":
        run(clone_admin.register_clone_token(i, "SECRET.TOKEN.VALUE", owner_id=USER))
    else:
        run(clone_admin.relink_clone_token(i, 1, "SECRET.TOKEN.VALUE", owner_id=USER))
    validate.assert_not_awaited()                                   # never validated, so never sent anywhere
    sent = i.followup.send.await_args
    assert sent.args[0] == clone_admin.NOT_AVAILABLE and sent.kwargs["ephemeral"] is True
    assert "SECRET" not in repr(i.followup.send.await_args_list)    # and never echoed back


def test_the_owner_is_still_let_through(monkeypatch):
    if not DISCORD_CLONE_ADMIN_IDS:
        pytest.skip("no DISCORD_CLONE_ADMIN_IDS configured in this environment")
    validate = AsyncMock(return_value={"ok": False, "error": "bad token"})
    monkeypatch.setattr(clone_admin, "validate_bot_token", validate)
    monkeypatch.setattr(clone_admin.db, "get_discord_clones_by_owner", AsyncMock(return_value=[]))
    i = _interaction()
    run(clone_admin.register_clone_token(i, "x", owner_id=OWNER))
    validate.assert_awaited_once()


# ── buttons left in OLD DMs answer politely and never open a form ────────
def test_a_stale_paste_button_does_not_open_a_modal():
    item = join_dm._BuildBotPasteButton(1, None)
    inter = SimpleNamespace(user=SimpleNamespace(id=USER),
                            response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock()))
    run(item.callback(inter))
    inter.response.send_modal.assert_not_awaited()
    assert "isn't available" in inter.response.send_message.await_args.args[0]


def test_a_stale_build_bot_toggle_is_disabled_and_answers_politely():
    btn = join_dm._FeatureToggleButton("build_bot", 1, None)
    assert btn.item.disabled is True
    inter = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    run(btn.callback(inter))
    assert "isn't available" in inter.followup.send.await_args.args[0]
