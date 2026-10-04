"""Small cached placeholder-card renderer for catch assets (P1-14)."""

from __future__ import annotations

import asyncio
import io
from functools import lru_cache


def _render_placeholder(asset_id: str, version: int, size: tuple[int, int]) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is required for catch card rendering") from exc
    image = Image.new("RGB", size, "#172033")
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, size[0] - 8, size[1] - 8), outline="#64748b", width=3)
    draw.text((size[0] // 2, size[1] // 2), asset_id[:18], fill="#e2e8f0", anchor="mm")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


@lru_cache(maxsize=256)
def _cached(asset_id: str, version: int, width: int, height: int) -> bytes:
    return _render_placeholder(asset_id, version, (width, height))


async def render_placeholder(asset_id: str, version: int = 1, *, width: int = 640, height: int = 360) -> bytes:
    if not asset_id or version < 1 or width < 1 or height < 1:
        raise ValueError("asset id, version, and dimensions must be positive")
    return await asyncio.to_thread(_cached, asset_id, version, width, height)


def clear_render_cache() -> None:
    _cached.cache_clear()


__all__ = ["clear_render_cache", "render_placeholder"]
