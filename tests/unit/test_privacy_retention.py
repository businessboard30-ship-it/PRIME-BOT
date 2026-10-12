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


def test_migration_wired_and_leaves_managed_bots_alone():
    sql = (ROOT / "database/migrations/051_privacy_retention.sql").read_text()
    assert "managed_bots" not in sql
    assert "051_privacy_retention.sql" in (ROOT / "database.py").read_text()


def test_my_data_cog_registered_last_and_cap_safe():
    bot = (ROOT / "discord_bot/bot.py").read_text()
    assert bot.index("cogs.my_data") > bot.index("cogs.lookup")
    assert "CommandLimitReached" in (ROOT / "discord_bot/cogs/my_data.py").read_text()
