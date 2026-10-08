#!/usr/bin/env python3
"""Generate the dashboard's level-tier gallery: WebP thumbnails + manifest.json.

Source of truth is modules/level_card._TIER_IMAGE_LEVELS (the table the bot itself uses),
so the gallery can't drift from what members actually see. Re-run after adding a tier:

    python3 scripts/build_tier_gallery.py

Output: dashboard/assets/tiers/<name>.webp and dashboard/assets/tiers/manifest.json
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost:5432/x")  # config import guard only

from PIL import Image  # noqa: E402

from modules import level_card  # noqa: E402

OUT = os.path.join(ROOT, "dashboard", "assets", "tiers")
THUMB_W = 480


def build_entries():
    """[{file, label, min_level, max_level|None, variant_group|None}] from the bot's table."""
    table, entries = level_card._TIER_IMAGE_LEVELS, []
    for i, (min_level, spec, *rest) in enumerate(table):
        nxt = table[i + 1][0] if i + 1 < len(table) else None
        max_level = (nxt - 1) if nxt else None
        if spec is None:                      # explicit gap: the procedural card is used there
            continue
        variants = spec if isinstance(spec, list) else [(spec, rest[0] if rest else "")]
        for fn, label in variants:
            entries.append({"file": fn, "label": label, "min_level": min_level, "max_level": max_level,
                            "random_pool": len(variants) > 1})
    return entries


def main():
    os.makedirs(OUT, exist_ok=True)
    entries = build_entries()
    for e in entries:
        src = os.path.join(ROOT, e["file"])
        thumb = os.path.splitext(e["file"])[0] + ".webp"
        with Image.open(src) as im:
            im = im.convert("RGBA")
            h = max(1, round(im.height * THUMB_W / im.width))
            im.resize((THUMB_W, h), Image.LANCZOS).save(os.path.join(OUT, thumb), "WEBP", quality=72, method=6)
        e["thumb"] = thumb
    with open(os.path.join(OUT, "manifest.json"), "w") as f:
        json.dump({"tiers": entries}, f, indent=1)
        f.write("\n")
    print(f"{len(entries)} tiers -> {OUT}")


if __name__ == "__main__":
    main()
