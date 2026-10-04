import asyncio

import pytest

from modules.catch_renderer import clear_render_cache, render_placeholder


def test_placeholder_renders_png_and_cache_reuses_bytes(monkeypatch):
    clear_render_cache()
    calls = 0

    from modules import catch_renderer

    original = catch_renderer._render_placeholder

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(catch_renderer, "_render_placeholder", counted)

    async def go():
        first = await render_placeholder("creature:fox", 2, width=120, height=80)
        second = await render_placeholder("creature:fox", 2, width=120, height=80)
        return first, second

    first, second = asyncio.run(go())
    assert first == second
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert calls == 1


def test_placeholder_rejects_invalid_dimensions():
    with pytest.raises(ValueError):
        asyncio.run(render_placeholder("creature:fox", width=0))

    with pytest.raises(ValueError):
        asyncio.run(render_placeholder("", version=1))


def test_placeholder_cache_can_be_cleared(monkeypatch):
    clear_render_cache()
    from modules import catch_renderer

    calls = 0
    original = catch_renderer._render_placeholder

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(catch_renderer, "_render_placeholder", counted)

    async def go():
        await render_placeholder("creature:owl", 1, width=96, height=96)
        clear_render_cache()
        await render_placeholder("creature:owl", 1, width=96, height=96)

    asyncio.run(go())
    assert calls == 2
