import asyncio

from modules.catch_renderer import clear_render_cache, render_placeholder


def test_placeholder_render_is_cached_and_png():
    clear_render_cache()
    first = asyncio.run(render_placeholder("fox", 2, width=32, height=24))
    second = asyncio.run(render_placeholder("fox", 2, width=32, height=24))
    assert first == second
    assert first.startswith(b"\x89PNG")


def test_placeholder_rejects_invalid_dimensions():
    try:
        asyncio.run(render_placeholder("fox", width=0))
    except ValueError:
        pass
    else:
        raise AssertionError("invalid dimensions should fail")
