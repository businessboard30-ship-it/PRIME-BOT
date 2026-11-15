import asyncio
import types

import discord

from discord_bot.cogs.bump_setup import fetch_channels_safe


def _guild(fetch):
    return types.SimpleNamespace(fetch_channels=fetch, channels=["cached-1", "cached-2"])


def test_fetch_channels_safe_returns_fetched_list():
    async def ok():
        return ["fresh"]
    assert asyncio.run(fetch_channels_safe(_guild(ok))) == ["fresh"]


def test_fetch_channels_safe_unknown_channel_type_falls_back_to_cache():
    async def boom():
        raise discord.InvalidData("Unknown channel type 20 for channel ID 1.")
    assert asyncio.run(fetch_channels_safe(_guild(boom))) == ["cached-1", "cached-2"]


def test_fetch_channels_safe_http_error_falls_back_to_cache():
    async def boom():
        resp = types.SimpleNamespace(status=500, reason="x")
        raise discord.HTTPException(resp, "x")
    assert asyncio.run(fetch_channels_safe(_guild(boom))) == ["cached-1", "cached-2"]


def test_welcome_dm_text_modal_label_fits_discord_limit():
    from discord_bot.cogs._views_welcome import WelcomeDmTextModal

    async def build():
        return WelcomeDmTextModal(1, None, 2, "hi")
    modal = asyncio.run(build())
    assert 1 <= len(modal.template.label) <= 45
    assert len(modal.template.placeholder or "") <= 100


def test_no_literal_textinput_label_exceeds_discord_limit():
    """Discord rejects a modal whose text-input label is over 45 characters (400, error 50035), so the whole form
    fails to open. Checks every plain-string label in the bot; f-string labels are built at runtime and skipped."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    bad = []
    for base in ("discord_bot", "modules"):
        for path in (root / base).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "TextInput":
                    for kw in node.keywords:
                        if kw.arg == "label" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                            if len(kw.value.value) > 45:
                                bad.append(f"{path.relative_to(root)}:{node.lineno} ({len(kw.value.value)}) {kw.value.value!r}")
    assert not bad, "TextInput label over 45 chars:\n" + "\n".join(bad)
