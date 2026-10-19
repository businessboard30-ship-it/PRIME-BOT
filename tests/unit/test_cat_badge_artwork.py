"""The ten achievement badge images ship as separate square files (artwork only, not mapped to any badge yet)."""
from pathlib import Path

from PIL import Image

DIR = Path(__file__).resolve().parents[2] / "assets" / "images" / "cat_badges"
NAMES = ["first-adopt", "collection-master", "rare-finder", "adventure-seeker", "market-mogul",
         "home-designer", "event-champion", "community-hero", "quest-master", "legendary"]


def test_all_ten_badges_exist_and_are_square():
    for n in NAMES:
        with Image.open(DIR / f"{n}.png") as im:
            assert im.size[0] == im.size[1] >= 256, n
    assert len(list(DIR.glob("*.png"))) == 10
