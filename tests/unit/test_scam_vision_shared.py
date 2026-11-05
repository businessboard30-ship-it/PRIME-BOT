"""Shared AI-scan budget across the main bot and all clones: one quota, one pause, backoff after repeated 429s."""
import asyncio
from datetime import datetime, timedelta, timezone

from modules import scam_vision as sv
from modules import scam_vision_shared as sh

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


def fresh(**kw):
    return {"blocked_until": None, "day": "", "day_count": 0, "win_start": None, "win_count": 0, **kw}


def test_per_minute_budget_is_shared_then_resets():
    st = fresh()
    for _ in range(8):
        ok, st, why = sh.decide(st, NOW, 8, 200)
        assert ok and why == "ok"
    ok, st, why = sh.decide(st, NOW + timedelta(seconds=30), 8, 200)
    assert (ok, why) == (False, "rpm")
    ok, st, why = sh.decide(st, NOW + timedelta(seconds=61), 8, 200)
    assert ok and st["win_count"] == 1


def test_daily_budget_is_shared_and_resets_at_utc_midnight():
    st = fresh(day="2026-11-05", day_count=200)
    ok, _, why = sh.decide(st, NOW, 8, 200)
    assert (ok, why) == (False, "daily")
    ok, st2, _ = sh.decide(st, NOW + timedelta(days=1), 8, 200)
    assert ok and st2["day"] == "2026-11-06" and st2["day_count"] == 1


def test_pause_blocks_everyone_until_it_ends():
    st = fresh(blocked_until=NOW + timedelta(seconds=90))
    assert sh.decide(st, NOW, 8, 200)[2] == "paused"
    assert sh.decide(st, NOW + timedelta(seconds=91), 8, 200)[0] is True


def test_backoff_grows_is_capped_and_honours_retry_after():
    assert [sh.backoff_seconds(n) for n in (1, 2, 3, 4, 5, 9)] == [60, 120, 300, 900, 3600, 3600]
    assert sh.backoff_seconds(1, 600) == 600 and sh.backoff_seconds(3, 10) == 300 and sh.backoff_seconds(1, 99999) == 3600
    assert sh.parse_retry_after("30") == 30.0 and sh.parse_retry_after(None) is None and sh.parse_retry_after("soon") is None


class Conn:
    def __init__(self): self.row = fresh() | {"fail_count": 0}
    def transaction(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, sql, *a):
        if sql.startswith("UPDATE scam_vision_shared SET day"):
            self.row.update(day=a[0], day_count=a[1], win_start=a[2], win_count=a[3])
        elif sql.startswith("UPDATE scam_vision_shared SET blocked_until = $1"):
            self.row.update(blocked_until=a[0], fail_count=a[1])
        elif sql.startswith("UPDATE scam_vision_shared SET fail_count = 0 WHERE"):
            self.row["fail_count"] = 0
        elif sql.startswith("UPDATE scam_vision_shared SET blocked_until = NULL"):
            self.row.update(blocked_until=None, fail_count=0)
    async def fetchrow(self, sql, *a): return dict(self.row)


class Pool:
    def __init__(self, c): self.c = c
    def acquire(self): return self
    async def __aenter__(self): return self.c
    async def __aexit__(self, *a): return False


def db(monkeypatch):
    conn = Conn()

    async def pool(): return Pool(conn)
    monkeypatch.setattr(sh, "_pool", pool)
    monkeypatch.setattr(sh, "_BLOCK_CACHE", (0.0, 0.0))
    return conn


def test_two_bots_share_one_counter(monkeypatch):
    conn = db(monkeypatch)
    results = [run(sh.acquire(3, 100, NOW))[0] for _ in range(5)]      # five "bots", budget of 3 a minute
    assert results == [True, True, True, False, False] and conn.row["day_count"] == 3


def test_repeated_429s_back_off_but_simultaneous_ones_count_once(monkeypatch):
    conn = db(monkeypatch)
    assert run(sh.block(None, NOW)) == 60 and conn.row["fail_count"] == 1
    assert run(sh.block(None, NOW + timedelta(seconds=1))) == 60 and conn.row["fail_count"] == 1     # 11 bots 429 together = one failure
    assert run(sh.block(None, NOW + timedelta(seconds=70))) == 120 and conn.row["fail_count"] == 2   # failed again after the pause
    run(sh.success())
    assert conn.row["fail_count"] == 0
    run(sh.clear_block())
    assert conn.row["blocked_until"] is None


def test_database_trouble_falls_back_to_local_limits(monkeypatch):
    async def boom(): raise RuntimeError("db down")
    monkeypatch.setattr(sh, "_pool", boom)
    monkeypatch.setattr(sh, "_BLOCK_CACHE", (0.0, 0.0))
    assert run(sh.acquire(3, 100, NOW)) == (True, "local")
    assert run(sh.block(None, NOW)) == 60 and run(sh.blocked_for()) == 0.0
    run(sh.success()); run(sh.clear_block())                           # none of these raise


def test_scan_skips_gemini_when_another_bot_paused_the_fleet(monkeypatch):
    calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SCAM_VISION_OPENAI_KEY", raising=False)
    monkeypatch.setattr(sv.ss, "vision_enabled", lambda: True)

    async def paused(*a, **k): return 45.0

    async def ask(jpeg): calls.append(1); return True
    monkeypatch.setattr(sv.shared, "blocked_for", paused)
    monkeypatch.setattr(sv, "_ask", ask)
    sv._s.blocked_until = 0.0
    sv._s.cache.clear()
    assert run(sv.is_scam_image(b"not-cached-image-1", 1, 2)) is None and calls == []


def test_defaults_are_lower_and_shared_in_the_docs():
    assert sv.DEFAULT_RPM == 8 and sv.DEFAULT_DAILY == 200
    assert "SHARED" in open("modules/scam_vision.py", encoding="utf-8").read()
