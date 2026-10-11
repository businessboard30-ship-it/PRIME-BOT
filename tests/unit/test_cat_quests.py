"""Quest/badge rules and DB behaviour with a fake connection (no real database)."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date

from modules import cat_quests as cq, cat_adopt


def test_quest_keys_for_events():
    assert cq.quest_keys_for("adopt", "common") == ["adopt-2"]
    assert cq.quest_keys_for("adopt", "rare") == ["adopt-2", "rare-1"]
    assert cq.quest_keys_for("adopt", "legendary") == ["adopt-2", "rare-1"]
    assert cq.quest_keys_for("rank") == ["rank-1"]
    assert cq.quest_keys_for("bogus") == []


def test_badge_rules():
    traits = list(cat_adopt.TRAITS)
    assert cq.earned_badges([]) == set()
    assert cq.earned_badges([("common", t) for t in traits[:4]]) == set()
    assert cq.earned_badges([("common", t) for t in traits[:5]]) == {"coat-collector"}
    assert "rare-hunter" in cq.earned_badges([("rare", "Sleepy")])
    assert {"rare-hunter", "legend"} <= cq.earned_badges([("legendary", "Sleepy")])
    assert "full-set" in cq.earned_badges([("common", t) for t in traits])
    assert "full-house" in cq.earned_badges([("common", "Sleepy")] * 10)
    assert "full-house" not in cq.earned_badges([("common", "Sleepy")] * 9)


def test_every_quest_and_badge_has_a_positive_reward():
    assert all(t > 0 and r > 0 for _, t, r in cq.QUESTS.values())
    assert all(b[3] > 0 for b in cq.BADGES.values())


class Conn:
    def __init__(self, rows=None, claim_row=None, awarded=()):
        self.rows, self.claim_row, self.awarded = rows or [], claim_row, set(awarded)
        self.executed = []

    def transaction(self):
        @asynccontextmanager
        async def cm():
            yield
        return cm()

    async def fetch(self, sql, *a):
        return self.rows

    async def fetchrow(self, sql, *a):
        return self.claim_row

    async def fetchval(self, sql, *a):
        slug = a[2]
        if slug in self.awarded:
            return None
        self.awarded.add(slug)
        return slug

    async def execute(self, sql, *a):
        self.executed.append((sql, a))


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def cm():
            yield self.conn
        return cm()


def _run(coro, conn, mp):
    async def pool():
        return Pool(conn)
    mp.setattr(cq, "_pool", pool)
    return asyncio.run(coro)


def test_claim_pays_once(monkeypatch):
    conn = Conn(claim_row={"diamonds_reward": 1})
    assert _run(cq.claim(1, None, "adopt-2", date(2026, 1, 1)), conn, monkeypatch) == "ok"
    assert len(conn.executed) == 1 and "diamonds" in conn.executed[0][0]
    none = Conn(claim_row=None)
    assert _run(cq.claim(1, None, "adopt-2", date(2026, 1, 1)), none, monkeypatch) == "not_ready"
    assert none.executed == []
    assert _run(cq.claim(1, None, "nope"), Conn(), monkeypatch) == "missing"


def test_badges_awarded_once_and_paid(monkeypatch):
    rows = [{"rarity": "legendary", "trait": "Sleepy"}]
    conn = Conn(rows=rows)
    first = _run(cq.award_badges(1, None), conn, monkeypatch)
    assert set(first) == {"rare-hunter", "legend"}
    assert len(conn.executed) == 2                          # one payout per new badge
    second = _run(cq.award_badges(1, None), conn, monkeypatch)
    assert second == [] and len(conn.executed) == 2         # nothing paid again


def test_progress_ignores_unknown_quest(monkeypatch):
    conn = Conn()
    _run(cq.record_progress(1, None, ["rank-1", "bogus", "rank-1"]), conn, monkeypatch)
    assert len(conn.executed) == 1


def test_hooks_never_raise(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(cq, "record_progress", boom)
    assert asyncio.run(cq.on_adopt(1, None, "rare")) == []
    assert asyncio.run(cq.on_rank(1, None)) is None
