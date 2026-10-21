"""Welcome Card Studio Phase 2a: gallery data layer + rotation picker (no wizard, no render change)."""
import random
from pathlib import Path

from modules import welcome_gallery as g

ROOT = Path(__file__).resolve().parents[2]


def test_no_pictures_gives_none_and_one_picture_is_always_used():
    assert g.pick_slot("random", []) is None
    for mode in g.MODES:
        assert g.pick_slot(mode, [4], member_number=7, last_slot=4, counter=3) == 4


def test_default_mode_is_random_and_bad_mode_falls_back():
    assert g.DEFAULT_MODE == "random"
    assert g.clean_mode(None) == "random" and g.clean_mode("nonsense") == "random"


def test_random_never_repeats_back_to_back():
    rng = random.Random(1)
    last = None
    for _ in range(300):
        s = g.pick_slot("random", [1, 2, 3], last_slot=last, rng=rng)
        assert s != last
        last = s


def test_random_with_unknown_last_slot_still_picks():
    assert g.pick_slot("random", [1, 2], last_slot=9) in (1, 2)


def test_order_wraps_around_and_follows_the_counter():
    got = [g.pick_slot("order", [2, 5, 7], counter=c) for c in range(7)]
    assert got == [2, 5, 7, 2, 5, 7, 2]


def test_member_number_is_stable():
    a = g.pick_slot("member", [1, 2, 3], member_number=10)
    assert a == g.pick_slot("member", [1, 2, 3], member_number=10) == 2
    assert g.pick_slot("member", [1, 2, 3], member_number=None) == 1


def test_pin_uses_the_pinned_slot_or_falls_back_to_first():
    assert g.pick_slot("pin", [1, 2, 3], pinned=3) == 3
    assert g.pick_slot("pin", [1, 2, 3], pinned=9) == 1


def test_junk_input_never_raises():
    assert g.pick_slot("order", ["x"], counter="bad") is None
    assert g.pick_slot("member", [1, 2], member_number="abc") is None
    assert g.pick_slot("random", [0, 11, 99]) is None


def test_ten_picture_cap_constant():
    assert g.MAX_PICTURES == 10


def test_migration_is_additive_registered_and_leaves_old_columns_alone():
    sql = (ROOT / "database/migrations/078_welcome_gallery.sql").read_text()
    up = sql.upper()
    assert "CREATE TABLE IF NOT EXISTS DISCORD_WELCOME_BACKGROUNDS" in up
    assert "DROP" not in up and "DELETE" not in up and "RENAME" not in up
    assert "CUSTOM_BG" not in up.split("CREATE TABLE")[1].split(");")[1]       # old picture columns untouched
    assert "UNIQUE (GUILD_ID, CLONE_KEY, SLOT)" in up
    assert "078_welcome_gallery.sql" in (ROOT / "database.py").read_text()


def test_db_methods_never_raise_on_the_join_path():
    src = (ROOT / "database.py").read_text()
    for name in ("gallery_list", "gallery_flag_missing", "gallery_next_counter", "gallery_set_last_slot"):
        body = src.split(f"async def {name}")[1].split("\n    async def ")[0]
        assert "except Exception" in body, name


def test_old_render_and_wizard_untouched_by_this_slice():
    # Phase 2a ships data + picker only: the card renderer and the wizard do not import the gallery yet.
    for f in ("modules/welcome_card.py", "discord_bot/cogs/_views_card_customize.py"):
        assert "welcome_gallery" not in (ROOT / f).read_text(), f
