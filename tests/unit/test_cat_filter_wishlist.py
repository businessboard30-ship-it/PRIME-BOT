"""My cats search/filter/sort + wishlist rules. No real database or Discord."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_adopt as ca
from modules import cat_filter as cf
from modules import cat_wishlist as cw


def cat(i, name="Mochi", rarity="common", trait="Sleepy", rank=0, day=1):
    return ca.Cat(id=i, owner_id=10, clone_id=None, pool_id=100 + i, name=name, rarity=rarity, trait=trait,
                  adopted_at=datetime(2026, 1, day, tzinfo=timezone.utc), cat_rank=rank)


CATS = [cat(1, "Mochi", "common", "Sleepy", 0, 1), cat(2, "Luna", "legendary", "Royal", 2, 3),
        cat(3, "Biscuit", "rare", "Sleepy", 1, 2), cat(4, "Luna Jr", "rare", "Brave", 0, 4)]


def test_filter_by_rarity_trait_and_query():
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(rarity="rare"))] == [3, 4]
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(trait="Sleepy"))] == [3, 1]       # rarest first
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(query="luna"))] == [2, 4]
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(query="3"))] == [3]                # cat #ID search
    assert cf.apply(CATS, cf.CatFilter(rarity="legendary", trait="Sleepy")) == []


def test_sorts_and_input_untouched():
    before = list(CATS)
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(sort="newest"))] == [4, 2, 3, 1]
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(sort="oldest"))] == [1, 3, 2, 4]
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(sort="name"))] == [3, 2, 4, 1]
    assert [c.id for c in cf.apply(CATS, cf.CatFilter(sort="rank"))][:2] == [2, 3]
    assert CATS == before                                   # never mutates or drops cats


def test_active_count_ignores_sort_and_query_is_clean():
    assert cf.CatFilter(sort="name").active == 0
    assert cf.CatFilter(query="x", rarity="rare").active == 2
    assert cf.clean_query("  Hello   WORLD  ") == "hello world"
    assert len(cf.clean_query("a" * 100)) == cf.QUERY_MAX


def run(coro):
    return asyncio.run(coro)


class I:
    def __init__(self):
        self.user = SimpleNamespace(id=10)
        self.edits = []
        self.response = SimpleNamespace(edit_message=self._edit)

    async def _edit(self, **kw):
        self.edits.append(kw)


def _view():
    return v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), list(CATS))


def test_my_cats_filter_flow_only_commits_on_done(monkeypatch):
    async def noop(*a, **k):
        return SimpleNamespace(title="t", add_field=lambda **k: None, set_footer=lambda **k: None)
    view = _view()
    fv = v.FilterView(view)
    fv.f = fv.f.with_(rarity="rare")
    assert view.filter == cf.CatFilter() and len(view.cats) == 4        # draft only, nothing changed yet
    captured = {}

    async def fake_embed():
        captured["n"] = len(view.cats)
        return None
    monkeypatch.setattr(view, "embed", fake_embed)
    run(fv._done(I()))
    assert captured["n"] == 2 and {c.id for c in view.cats} == {3, 4}
    assert len(view.all_cats) == 4                                      # the collection itself is untouched


def test_filter_clears_itself_when_the_last_match_disappears():
    view = _view()
    view.filter = cf.CatFilter(rarity="legendary")
    view._reapply()
    assert [c.id for c in view.cats] == [2]
    view.all_cats = [c for c in CATS if c.id != 2]                       # the legendary was sold
    view._reapply()
    assert view.filter == cf.CatFilter() and len(view.cats) == 3         # never an empty screen


def test_untouched_filter_keeps_original_order():
    view = _view()
    assert [c.id for c in view.cats] == [1, 2, 3, 4]


# ── wishlist ────────────────────────────────────────────────────────────────

def test_note_cleaning():
    assert cw.clean_note("  will swap   a rare ") == "will swap a rare"
    assert cw.clean_note("@everyone hi <@123>") == "everyone hi 123"
    assert cw.clean_note("join discord.gg/abc") is None
    assert cw.clean_note("see https://x.y") is None
    assert len(cw.clean_note("a" * 200)) == cw.NOTE_MAX
    assert cw.clean_note(None) == ""


def test_valid_want():
    assert cw.valid("legendary", None) and cw.valid("rare", "Royal")
    assert not cw.valid("mythic", None) and not cw.valid("rare", "NotATrait")


class Conn:
    def __init__(self, open_n=0, insert=1):
        self.open_n, self.insert, self.executed = open_n, insert, []

    def transaction(self):
        @asynccontextmanager
        async def cm():
            yield
        return cm()

    async def execute(self, sql, *a):
        self.executed.append(sql)

    async def fetchval(self, sql, *a):
        return self.open_n if "COUNT" in sql else (self.insert if "INSERT" in sql else None)


class Pool:
    def __init__(self, c):
        self.c = c

    def acquire(self):
        @asynccontextmanager
        async def cm():
            yield self.c
        return cm()


def _add(conn, mp, *args):
    async def pool():
        return Pool(conn)
    mp.setattr(cw, "_pool", pool)
    return run(cw.add(*args))


def test_add_outcomes(monkeypatch):
    assert _add(Conn(), monkeypatch, 1, None, "legendary", None, "") == "ok"
    assert _add(Conn(open_n=cw.MAX_OPEN), monkeypatch, 1, None, "legendary", None) == "limit"
    assert _add(Conn(insert=None), monkeypatch, 1, None, "legendary", None) == "duplicate"
    assert _add(Conn(), monkeypatch, 1, None, "mythic", None) == "bad"
    bad = Conn()
    assert _add(bad, monkeypatch, 1, None, "rare", None, "discord.gg/x") == "bad" and bad.executed == []


def test_limit_check_does_not_insert(monkeypatch):
    c = Conn(open_n=cw.MAX_OPEN)
    _add(c, monkeypatch, 1, None, "rare", "Royal")
    assert not any("INSERT" in s for s in c.executed)


def test_migration_has_expiry_and_dedupe():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[2] / "database/migrations/045_cat_wishlist.sql").read_text()
    assert "expires_at" in sql and "CREATE UNIQUE INDEX" in sql and "status = 'open'" in sql
