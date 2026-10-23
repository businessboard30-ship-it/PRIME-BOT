# path: modules/cat_quality.py

"""
Quality filter for cat pictures: only sharp, high-resolution, normal-shaped stills get posted.

Pure Pillow, no network. Used before a picture is saved to the shared pool (modules/cat_pool.py), so a blurry
or tiny picture is never posted by any bot.

Checks (all must pass):
  * a real still JPEG / PNG / WEBP (no GIFs or animations), 30 KB to 8 MB;
  * short side >= 600 px and long side >= 800 px, not a thin strip (aspect <= 2.2), not absurdly large;
  * sharp: a Laplacian edge measure on a 4x4 grid; the score is the mean of the 4 sharpest tiles, so a cat in
    focus in front of a nicely blurred background still passes, while a picture that is soft everywhere fails.

The sharpness limit can be tuned without a code change: set CAT_MIN_SHARPNESS on Railway.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageStat
import logging

logger = logging.getLogger(__name__)

MIN_SHORT_SIDE = 600
MIN_LONG_SIDE = 800
MAX_ASPECT = 2.2
MAX_PIXELS = 40_000_000
MIN_BYTES = 30_000
MAX_BYTES = 8_000_000
ANALYSIS_LONG_SIDE = 900            # bigger pictures are scaled down to this before measuring (fair across sizes)
GRID = 4
TOP_TILES = 4
DEFAULT_MIN_SHARPNESS = 120.0
ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")

# 8-neighbour Laplacian; offset keeps negative responses (plain clipping would throw half the edges away)
_LAPLACE = ImageFilter.Kernel((3, 3), (-1, -1, -1, -1, 8, -1, -1, -1, -1), scale=1, offset=128)


def min_sharpness() -> float:
    try:
        return float(os.getenv("CAT_MIN_SHARPNESS", "") or DEFAULT_MIN_SHARPNESS)
    except ValueError:
        return DEFAULT_MIN_SHARPNESS


@dataclass(frozen=True)
class Quality:
    ok: bool
    reason: str
    width: int = 0
    height: int = 0
    sharpness: float = 0.0
    fmt: str = ""
    size: int = 0


def sharpness(img: Image.Image) -> float:
    """Mean edge energy of the 4 sharpest tiles (higher = sharper)."""
    g = img.convert("L")
    long_side = max(g.size)
    if long_side > ANALYSIS_LONG_SIDE:
        k = ANALYSIS_LONG_SIDE / long_side
        g = g.resize((max(1, round(g.width * k)), max(1, round(g.height * k))), Image.LANCZOS)
    edges = g.filter(_LAPLACE)
    w, h = edges.size
    tiles = []
    for r in range(GRID):
        for c in range(GRID):
            box = (c * w // GRID, r * h // GRID, (c + 1) * w // GRID, (r + 1) * h // GRID)
            tiles.append(ImageStat.Stat(edges.crop(box)).var[0])
    tiles.sort(reverse=True)
    top = tiles[:TOP_TILES]
    return sum(top) / len(top)


def assess(data: bytes) -> Quality:
    """Judge one downloaded picture. Never raises."""
    size = len(data or b"")
    if size < MIN_BYTES:
        return Quality(False, "file is too small", size=size)
    if size > MAX_BYTES:
        return Quality(False, "file is too big", size=size)
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            w, h = img.size
            if fmt not in ALLOWED_FORMATS:
                return Quality(False, f"format {fmt or 'unknown'} not allowed", w, h, fmt=fmt, size=size)
            if getattr(img, "n_frames", 1) > 1:
                return Quality(False, "animated picture", w, h, fmt=fmt, size=size)
            if w * h > MAX_PIXELS:
                return Quality(False, "picture is absurdly large", w, h, fmt=fmt, size=size)
            short, long_ = min(w, h), max(w, h)
            if short < MIN_SHORT_SIDE or long_ < MIN_LONG_SIDE:
                return Quality(False, f"resolution too low ({w}x{h})", w, h, fmt=fmt, size=size)
            if long_ / short > MAX_ASPECT:
                return Quality(False, "odd shape (too wide or tall)", w, h, fmt=fmt, size=size)
            img.load()
            score = sharpness(img)
    except Exception:
        logger.debug("swallowed exception in assess", exc_info=True)
        return Quality(False, "couldn't read the picture", size=size)
    if score < min_sharpness():
        return Quality(False, f"blurry (sharpness {score:.0f})", w, h, score, fmt, size)
    return Quality(True, "ok", w, h, score, fmt, size)
