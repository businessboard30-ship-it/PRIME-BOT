"""Cat trade IDs are short now: 6 digits + a check letter (like 142574D). Old 7-word IDs keep working."""
import asyncio
import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_ids as ids
from modules import cat_setup as cs
from tests.unit.test_cat_free_id_trade import Box, _setup
from tests.unit.test_cat_trade_ids import FakeConn, FakePool, _interaction, _parent

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


@pytest.fixture
def pool(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(ids, "_pool", AsyncMock(return_value=FakePool(conn)))
    ids._fails.clear()
    return conn


# ---------- the format ----------
def test_generated_ids_look_like_142574D_and_never_use_confusing_letters():
    seen = set()
    for _ in range(2000):
        t = ids.generate_short()
        assert re.fullmatch(r"[1-9][0-9]{5}[A-HJKMNP-Z]", t) and ids.normalize_short(t) == t
        assert not set(t) & set("ILO")
        seen.add(t)
    assert len(seen) > 1990 and len(ids.SHORT_LETTERS) == 23


def test_the_worked_example():
    assert ids.check_letter("142574") == "D" and ids.normalize_short("142574D") == "142574D"
    assert ids.normalize_short("142574M") is None                    # right shape, wrong check letter: a typo


def test_every_single_wrong_digit_is_caught():
    base = "142574"
    letter = ids.check_letter(base)
    for pos in range(6):
        for d in "0123456789":
            if d == base[pos]:
                continue
            bad = base[:pos] + d + base[pos + 1:]
            if bad[0] == "0":
                continue
            assert ids.normalize_short(bad + letter) is None


def test_every_swap_of_two_neighbouring_different_digits_is_caught():
    for n in range(100000, 100000 + 4000, 7):
        s = str(n)
        letter = ids.check_letter(s)
        for pos in range(5):
            if s[pos] == s[pos + 1]:
                continue
            swapped = s[:pos] + s[pos + 1] + s[pos] + s[pos + 2:]
            if swapped[0] == "0":
                continue
            assert ids.normalize_short(swapped + letter) is None


def test_typing_is_forgiving_about_case_spaces_hash_and_dashes():
    t = ids.generate_short()
    for typed in (t.lower(), t[:3] + " " + t[3:], "#" + t, f" {t} ", t[:3] + "-" + t[3:], t[:3] + "." + t[3:], t[:3] + " " + t[3:6] + " " + t[6]):
        assert ids.normalize_short(typed) == t


@pytest.mark.parametrize("bad", [None, "", " ", "142574", "142574DD", "D142574", "042574D", "14257D", "1425744D", "142574I", "<b>142574D</b>",
                                 "142574D\n142574D", 142574, ["142574D"], {"a": 1}, "142 574 D x"])
def test_junk_is_never_an_id(bad):
    assert ids.normalize_short(bad) is None and ids.parse(bad) is None


def test_old_seven_word_ids_are_still_recognised():
    w = ids.generate()
    assert ids.parse(w) == ("words", w) and ids.parse(w.upper().replace("-", " ")) == ("words", w)
    s = ids.generate_short()
    assert ids.parse(s) == ("short", s)


# ---------- creating and finding ----------
def test_new_owner_gets_a_short_id_only_and_it_is_fixed(pool):
    a = run(ids.get_or_create(1, None))
    assert ids.normalize_short(a) == a and pool.rows[(1, -1)] == {"short": a, "words": None}
    assert run(ids.get_or_create(1, None)) == a


def test_an_owner_with_only_an_old_word_id_gets_a_short_one_on_the_same_row(pool):
    old = ids.generate()
    pool.rows[(5, -1)] = {"short": None, "words": old}
    new = run(ids.get_or_create(5, None))
    assert ids.normalize_short(new) == new and pool.rows[(5, -1)] == {"short": new, "words": old}


def test_old_ids_still_find_their_owner_and_new_ones_do_too(pool):
    old = ids.generate()
    pool.rows[(5, -1)] = {"short": None, "words": old}
    assert run(ids.find_owner(old, None)) == 5 and run(ids.find_identity(old)) == (5, None)
    new = run(ids.get_or_create(5, None))
    assert run(ids.find_owner(new, None)) == 5 and run(ids.find_identity(new.lower())) == (5, None)
    other = run(ids.get_or_create(9, 7))
    assert run(ids.find_identity(other)) == (9, 7) and run(ids.find_owner(other, None)) is None


def test_a_number_somebody_else_has_is_redrawn(pool, monkeypatch):
    pool.clash_next = 3
    a = run(ids.get_or_create(1, None))
    assert ids.normalize_short(a) == a
    taken = ids.generate_short()
    pool.rows[(2, -1)] = {"short": taken, "words": None}
    draws = iter([taken, taken, ids.generate_short()])
    monkeypatch.setattr(ids, "generate_short", lambda: next(draws))
    b = run(ids.get_or_create(3, None))
    assert b != taken and pool.rows[(2, -1)]["short"] == taken


def test_giving_up_after_many_clashes_raises_and_safe_get_returns_none(pool, monkeypatch):
    pool.clash_next = 10_000
    with pytest.raises(RuntimeError):
        run(ids.get_or_create(1, None))
    assert run(ids.safe_get(1, None)) is None


# ---------- the Trade-by-ID box ----------
def _typed(text):
    m = v.TradeIdModal(_parent())
    m.trade_id = Box(text)
    return m


def test_a_typo_is_caught_without_any_lookup_and_does_not_count_as_a_guess(monkeypatch):
    ids._fails.clear()
    find = AsyncMock()
    monkeypatch.setattr(ids, "find_identity", find)
    for typed in ("142574M", "hello", "12", "maple otter river cloud pepper lantern"):
        i = _interaction()
        run(_typed(typed).on_submit(i))
        assert "doesn't look like a trade ID" in i.response.send_message.call_args.args[0] and "142574D" in i.response.send_message.call_args.args[0]
    find.assert_not_called()
    assert ids.lookup_allowed(10)


def test_unknown_ids_are_rate_limited_so_nobody_can_scan_them(monkeypatch):
    ids._fails.clear()
    i0 = _setup(monkeypatch, premium=True, owner_id=None)
    for _ in range(ids.LOOKUP_FAILS_MAX):
        i = _interaction()
        run(_typed(ids.generate_short()).on_submit(i))
        assert "don't know that ID" in i.response.send_message.call_args.args[0]
    i = _interaction()
    run(_typed(ids.generate_short()).on_submit(i))
    assert "wait a little while" in i.response.send_message.call_args.args[0]
    other = _interaction(user_id=11)                                   # somebody else is not affected
    run(_typed(ids.generate_short()).on_submit(other))
    assert "don't know that ID" in other.response.send_message.call_args.args[0]


def test_outside_person_in_a_free_server_counts_like_an_unknown_id(monkeypatch):
    ids._fails.clear()
    i = _setup(monkeypatch, premium=False, owner_id=77, member=None, fetch_raises=True)
    run(_typed("142574D").on_submit(i))
    assert "in this server" in i.response.send_message.call_args.args[0]
    assert len(ids._fails[10]) == 1


def test_a_good_lookup_is_not_counted(monkeypatch):
    ids._fails.clear()
    from types import SimpleNamespace
    member = SimpleNamespace(id=77, bot=False, display_name="N", mention="<@77>")
    i = _setup(monkeypatch, premium=False, owner_id=77, member=member)
    run(_typed("142574D").on_submit(i))
    assert not ids._fails.get(10)


def test_lookup_window_forgets_old_misses(monkeypatch):
    ids._fails.clear()
    for _ in range(ids.LOOKUP_FAILS_MAX):
        ids.note_failed_lookup(1)
    assert not ids.lookup_allowed(1)
    ids._fails[1] = [t - ids.LOOKUP_WINDOW_SECONDS - 1 for t in ids._fails[1]]
    assert ids.lookup_allowed(1)


# ---------- wiring ----------
def test_migration_wiring_and_text():
    sql = (ROOT / "database/migrations/042_cat_short_ids.sql").read_text()
    assert "short_id" in sql and "DROP NOT NULL" in sql and "UNIQUE INDEX IF NOT EXISTS ux_cat_owner_ids_short" in sql
    db = (ROOT / "database.py").read_text()
    assert "042_cat_short_ids.sql" in db and db.index("041_cat_ranks.sql\"") < db.index("042_cat_short_ids.sql\"")
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', db, re.M).group(1)) >= 87


def test_no_word_ids_are_shown_to_people_any_more():
    src = inspect.getsource(v)
    assert "maple-otter" not in src and "7-word" not in src and "142574D" in src
    ai = (ROOT / "modules/ai_features.py").read_text()
    assert "7 random words" not in ai and "6 digits and a letter" in ai
    assert "142574M" not in src and "142574M" not in ai                                 # the old example fails its own check letter


def test_the_my_cats_screen_shows_the_short_id(monkeypatch):
    assert "cat_ids.safe_get" in inspect.getsource(v.CollectionView.prepare)
    assert "Your trade ID" in inspect.getsource(v.CollectionView._body)
