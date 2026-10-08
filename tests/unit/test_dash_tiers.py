import json
import os
import pytest

from modules import level_card

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TIERS = os.path.join(ROOT, "dashboard", "assets", "tiers")


@pytest.fixture(scope="module")
def manifest():
    with open(os.path.join(TIERS, "manifest.json")) as f:
        return json.load(f)["tiers"]


def _table_files():
    out = set()
    for _lvl, spec, *rest in level_card._TIER_IMAGE_LEVELS:
        if spec is None:
            continue
        out.update(fn for fn, _ in spec) if isinstance(spec, list) else out.add(spec)
    return out


def test_manifest_matches_the_bots_tier_table(manifest):
    """If this fails, someone changed level_card._TIER_IMAGE_LEVELS: run scripts/build_tier_gallery.py."""
    assert {t["file"] for t in manifest} == _table_files()


def test_every_thumbnail_exists_and_is_small(manifest):
    for t in manifest:
        path = os.path.join(TIERS, t["thumb"])
        assert os.path.isfile(path), t["thumb"]
        assert os.path.getsize(path) < 150_000, f"{t['thumb']} is too big for a thumbnail"


def test_ranges_agree_with_the_real_lookup(manifest):
    """Spot-check the gallery's level ranges against get_tier_image_for_level itself."""
    by_label = {}
    for t in manifest:
        by_label.setdefault(t["label"], []).append(t)
    for level in (1, 3, 7, 21, 40, 66, 100, 150, 211, 400):
        picked = level_card.get_tier_image_for_level(level)
        assert picked is not None, level
        fn, label = picked
        hits = [t for t in manifest if t["min_level"] <= level and (t["max_level"] is None or level <= t["max_level"])]
        assert any(t["file"] == fn and t["label"] == label for t in hits), (level, fn)


def test_levels_without_artwork_are_not_in_any_range(manifest):
    for level in range(1, 300):
        if level_card.get_tier_image_for_level(level) is None:
            assert not any(t["min_level"] <= level and (t["max_level"] is None or level <= t["max_level"]) for t in manifest), level


def test_committed_manifest_is_current(manifest):
    """The committed manifest equals what the generator would produce (without re-rendering images)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_tier_gallery", os.path.join(ROOT, "scripts", "build_tier_gallery.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = [dict(e, thumb=os.path.splitext(e["file"])[0] + ".webp") for e in mod.build_entries()]
    assert fresh == manifest
