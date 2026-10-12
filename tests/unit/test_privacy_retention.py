import asyncio
import re
from pathlib import Path

import modules.moderation_extra as modx

ROOT = Path(__file__).resolve().parents[2]


def test_fingerprint_hides_text_and_matches_repeats():
    a = modx.fingerprint("Free   NITRO here")
    assert a.startswith("h1:") and "nitro" not in a.lower()
    assert a == modx.fingerprint("free nitro here")
    assert a != modx.fingerprint("something else")
    assert modx.fingerprint("") is None


def test_flood_event_stores_only_hash(monkeypatch):
    seen = {}

    class Conn:
        async def execute(self, sql, *args):
            seen["args"] = args

    class Acq:
        async def __aenter__(self): return Conn()
        async def __aexit__(self, *a): return False

    class Pool:
        def acquire(self): return Acq()

    async def gp(): return Pool()
    monkeypatch.setattr(modx, "get_pool", gp)
    asyncio.run(modx.record_flood_event(1, 2, "secret words"))
    assert "secret" not in str(seen["args"]) and seen["args"][2].startswith("h1:")


def test_scam_hit_and_ai_log_do_not_store_text():
    scam = (ROOT / "modules/scam_shield.py").read_text()
    assert "snippet[:300]" not in scam
    db = (ROOT / "database.py").read_text()
    assert "json.dumps(args or {}, default=str), allowed" not in db


def test_startup_no_longer_rewrites_big_tables():
    """The old 051 migration ran three table-wide UPDATEs/DELETEs during startup, timed out, and kept every bot from
    finishing its start. The scrub now runs in small batches from modules/data_retention.py instead."""
    db = (ROOT / "database.py").read_text()
    assert '"051_privacy_retention.sql"' not in db                         # (the version comments may still mention it)
    assert not (ROOT / "database/migrations/051_privacy_retention.sql").exists()
    import modules.data_retention as dr
    for sql in (dr.SCRUB_FLOOD_TEXT, dr.SCRUB_SCAM_SNIPPETS, dr.SCRUB_AI_ARGS, dr._flood_delete_sql()):
        assert "LIMIT $1" in sql and "SKIP LOCKED" in sql                  # bounded and never waits on another process


class _Conn:
    """Pretends each batch changed `sizes[i]` rows."""
    def __init__(self, sizes):
        self.sizes, self.sql, self.calls = list(sizes), [], 0

    def transaction(self):
        conn = self

        class T:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return T()

    async def execute(self, sql, *args):
        if sql.startswith("SET LOCAL"):
            self.sql.append(sql)
            return "SET"
        self.calls += 1
        return f"UPDATE {self.sizes.pop(0) if self.sizes else 0}"


def test_scrub_runs_in_batches_and_stops_when_done():
    import modules.data_retention as dr
    c = _Conn([2000, 2000, 700])
    assert asyncio.run(dr._batched(c, dr.SCRUB_AI_ARGS)) == 4700
    assert c.calls == 3 and c.sql and "statement_timeout" in c.sql[0]       # stopped at the short batch, with a per-batch timeout
    full = _Conn([2000] * 100)
    assert asyncio.run(dr._batched(full, dr.SCRUB_AI_ARGS)) == 2000 * dr.MAX_BATCHES and full.calls == dr.MAX_BATCHES   # one run stays short


def test_a_failing_batch_never_raises():
    import modules.data_retention as dr

    class Bad(_Conn):
        async def execute(self, sql, *args):
            raise RuntimeError("lock timeout")
    assert asyncio.run(dr._batched(Bad([5]), dr.SCRUB_AI_ARGS)) == 0


def test_my_data_cog_registered_last_and_cap_safe():
    bot = (ROOT / "discord_bot/bot.py").read_text()
    assert bot.index("cogs.my_data") > bot.index("cogs.lookup")
    assert "CommandLimitReached" in (ROOT / "discord_bot/cogs/my_data.py").read_text()
