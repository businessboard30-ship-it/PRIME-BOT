"""Auto-log of OLD cats: adoptions and pictures from before the storage-channel log existed."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from modules import cat_log, cat_log_backfill as bf, image_host

run = asyncio.run
OLD = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
NEW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


# ---------- the line ----------
def _row(**kw):
    return {"id": 7, "owner_id": 111, "pool_id": 9, "name": "Mochi", "rarity": "rare", "trait": "Sleepy",
            "adopted_guild_id": 222, "adopted_at": OLD, "traded_count": 2, **kw}


def test_history_line_says_only_what_the_database_knows():
    bot = SimpleNamespace(get_user=lambda i: SimpleNamespace(display_name="Sam"), get_guild=lambda i: SimpleNamespace(name="Cat Club"))
    line = cat_log.format_event("history", bf.history_fields(_row(), bot))
    assert "Earlier record" in line and "**Mochi** (rare · Sleepy)" in line
    assert "owner now: Sam (111)" in line and "adopted in server: Cat Club (222) on 2026-10-01" in line
    assert "changed hands 2x" in line
    assert "Adopted" not in line and " by " not in line                   # the first adopter isn't recorded, so never claim it


def test_unknown_people_and_servers_fall_back_and_nothing_pings():
    bot = SimpleNamespace(get_user=lambda i: None, get_guild=lambda i: None)
    line = cat_log.format_event("history", bf.history_fields(_row(name="@everyone <@1> **x**", traded_count=0), bot))
    assert "someone (111)" in line and "a server (222)" in line and "changed hands" not in line
    assert "@" not in line.replace("@everyone", "") and "<@" not in line and "@everyone" not in line


# ---------- picture note ----------
def test_note_is_appended_never_replaces_and_is_idempotent():
    text = "🐱 cat pool · 640x480 · sharpness 120 · cataas"
    new = bf.with_server_note(text, 222, "Cat Club")
    assert new.startswith(text) and new.endswith("first spawned in server: Cat Club (222)")
    assert bf.with_server_note(new, 222, "Cat Club") is None
    assert bf.with_server_note(text + " · fetched for server: X (1)", 222, "Y") is None     # already named by #238


def test_note_skips_when_no_server_or_no_room_and_is_clean():
    assert bf.with_server_note("x", None, "n") is None and bf.with_server_note("x", 0, "n") is None
    assert bf.with_server_note("x" * 1995, 222, "n") is None
    assert "@everyone" not in bf.with_server_note("x", 222, "@everyone **hi** https://e.example")


# ---------- settings ----------
def test_cutoff_default_override_and_garbage(monkeypatch):
    monkeypatch.delenv("CAT_LOG_BACKFILL_BEFORE", raising=False)
    assert bf.cutoff() == bf.DEFAULT_CUTOFF and bf.DEFAULT_CUTOFF > datetime(2026, 10, 11, 12, 4, 30, tzinfo=timezone.utc)
    monkeypatch.setenv("CAT_LOG_BACKFILL_BEFORE", "2026-10-05T00:00:00Z")
    assert bf.cutoff() == datetime(2026, 10, 5, tzinfo=timezone.utc)
    monkeypatch.setenv("CAT_LOG_BACKFILL_BEFORE", "not a date")
    assert bf.cutoff() == bf.DEFAULT_CUTOFF


def test_switch_off(monkeypatch):
    monkeypatch.delenv("CAT_LOG_BACKFILL", raising=False)
    assert bf.enabled() is True
    monkeypatch.setenv("CAT_LOG_BACKFILL", "0")
    assert bf.enabled() is False


# ---------- one tick, with the ledger faked ----------
class Ledger:
    def __init__(self, adoptions=(), pictures=()):
        self.adoptions, self.pictures, self.marks = list(adoptions), list(pictures), []

    async def pending_adoptions(self, limit, before):
        done = {r for k, r, _ in self.marks if k == "adoption"}
        return [a for a in self.adoptions if a["id"] not in done][:limit]

    async def pending_pictures(self, limit, before):
        done = {r for k, r, _ in self.marks if k == "picture"}
        return [p for p in self.pictures if p["id"] not in done][:limit]

    async def mark(self, kind, ref, status):
        self.marks.append((kind, ref, status))
        return status


def _msg(content, author=999, edits=None):
    async def edit(**kw):
        edits.append(kw)
    return SimpleNamespace(content=content, author=SimpleNamespace(id=author), edit=edit)


def _setup(monkeypatch, ledger, host_ok=True, send_ok=True, msgs=None):
    sent, edits = [], []
    host = SimpleNamespace(id=555)

    async def fetch_message(mid):
        m = (msgs or {}).get(mid)
        if m is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone")
        return _msg(m, edits=edits)
    host.fetch_message = fetch_message

    async def resolve(bot):
        return host if host_ok else None

    async def fake_send(bot, kind, fields):
        sent.append((kind, fields))
        return send_ok
    monkeypatch.setattr(image_host, "resolve_cat_channel", resolve)
    monkeypatch.setattr(cat_log, "_send", fake_send)
    for n in ("pending_adoptions", "pending_pictures", "mark"):
        monkeypatch.setattr(bf, n, getattr(ledger, n))
    bot = SimpleNamespace(user=SimpleNamespace(id=999), get_user=lambda i: None, get_guild=lambda i: SimpleNamespace(name="Cat Club"))
    return bot, sent, edits


def test_no_storage_channel_marks_nothing_so_it_tries_again_later(monkeypatch):
    led = Ledger([_row()])
    bot, sent, _ = _setup(monkeypatch, led, host_ok=False)
    out = run(bf.run_once(bot, gap=0))
    assert out["error"] == "no_storage_channel" and led.marks == [] and sent == []


def test_old_adoptions_are_logged_once_and_marked(monkeypatch):
    led = Ledger([_row(id=1), _row(id=2, name="Bean")])
    bot, sent, _ = _setup(monkeypatch, led)
    assert run(bf.run_once(bot, gap=0))["logged"] == 2
    assert [k for k, _ in sent] == ["history", "history"] and [m[2] for m in led.marks] == ["done", "done"]
    assert run(bf.run_once(bot, gap=0))["logged"] == 0 and len(sent) == 2          # second tick writes nothing


def test_a_failed_line_is_marked_for_retry_not_done(monkeypatch):
    led = Ledger([_row()])
    bot, sent, _ = _setup(monkeypatch, led, send_ok=False)
    out = run(bf.run_once(bot, gap=0))
    assert out["retry"] == 1 and led.marks == [("adoption", 7, "retry")]


def test_pictures_get_the_note_and_only_if_it_is_ours_and_missing(monkeypatch):
    pics = [{"id": 1, "channel_id": 555, "message_id": 10, "first_guild": 222},          # old caption: edit
            {"id": 2, "channel_id": 555, "message_id": 11, "first_guild": 222},          # already named: nothing
            {"id": 3, "channel_id": 555, "message_id": 12, "first_guild": None},         # never spawned: skip
            {"id": 4, "channel_id": 555, "message_id": 13, "first_guild": 222},          # message deleted: skip
            {"id": 5, "channel_id": 777, "message_id": 14, "first_guild": 222}]          # other channel: skip
    msgs = {10: "🐱 cat pool · 1x1", 11: "🐱 cat pool · fetched for server: A (1)", 12: "x", 14: "x"}
    led = Ledger(pictures=pics)
    bot, sent, edits = _setup(monkeypatch, led, msgs=msgs)
    out = run(bf.run_once(bot, gap=0))
    assert len(edits) == 1 and edits[0]["content"].endswith("first spawned in server: Cat Club (222)")
    assert isinstance(edits[0]["allowed_mentions"], discord.AllowedMentions)
    assert dict((r, s) for _, r, s in led.marks) == {1: "done", 2: "skipped", 3: "skipped", 4: "skipped", 5: "skipped"}
    assert out["captions"] == 1 and out["skipped"] == 4


def test_a_message_from_someone_else_is_never_edited(monkeypatch):
    led = Ledger(pictures=[{"id": 1, "channel_id": 555, "message_id": 10, "first_guild": 222}])
    bot, sent, edits = _setup(monkeypatch, led, msgs={10: "old"})
    bot.user.id = 12345                                                                  # the message author is 999
    run(bf.run_once(bot, gap=0))
    assert edits == [] and led.marks == [("picture", 1, "skipped")]


def test_batch_limit_and_adoptions_come_first(monkeypatch):
    led = Ledger([_row(id=i) for i in range(1, 6)], [{"id": 1, "channel_id": 555, "message_id": 10, "first_guild": 222}])
    bot, sent, edits = _setup(monkeypatch, led, msgs={10: "old"})
    run(bf.run_once(bot, limit=3, gap=0))
    assert len(sent) == 3 and edits == []                                                # the batch was used up by adoptions


# ---------- wiring ----------
def test_cog_runs_it_on_the_main_bot_only_and_the_game_is_untouched():
    src = Path("discord_bot/cogs/cat.py").read_text()
    assert 'if getattr(self.bot, "clone_id", None) is None:\n            self._backfill.start()' in src
    mod = Path("modules/cat_log_backfill.py").read_text()
    for forbidden in ("cat_wallets", "UPDATE cat_adoptions", "DELETE FROM cat_adoptions", "UPDATE cat_spawns", "DELETE FROM cat_spawns", "INSERT INTO cat_spawns"):
        assert forbidden not in mod


def test_migration_is_wired_and_idempotent():
    sql = Path("database/migrations/039_cat_log_backfill.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS cat_log_backfill" in sql and "PRIMARY KEY (kind, ref_id)" in sql
    assert "039_cat_log_backfill.sql" in Path("database.py").read_text()


# ---------- the real SQL, only when a Postgres with the cat tables is available ----------
DB = os.getenv("CAT_TEST_DATABASE_URL")


@pytest.mark.skipif(not DB, reason="set CAT_TEST_DATABASE_URL to a Postgres with migrations 032, 033 and 039 applied")
def test_real_sql_selection_ledger_and_retry_limit(monkeypatch):
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(DB, min_size=1, max_size=2)

        async def get():
            return pool
        monkeypatch.setattr(bf, "_pool", get)
        async with pool.acquire() as c:
            await c.execute("TRUNCATE cat_log_backfill, cat_adoptions, cat_spawns, cat_image_pool RESTART IDENTITY")
            for i, when in enumerate((OLD, NEW), start=1):
                await c.execute("""INSERT INTO cat_image_pool (sha256, source, width, height, sharpness, bytes, channel_id, message_id, created_at)
                                   VALUES ($1,'cataas',1,1,1,1,555,$2,$3)""", f"h{i}", 100 + i, when)
                await c.execute("""INSERT INTO cat_adoptions (owner_id, pool_id, name, adopted_guild_id, adopted_at)
                                   VALUES (111,$1,$2,222,$3)""", i, f"c{i}", when)
            await c.execute("INSERT INTO cat_spawns (guild_id, channel_id, pool_id, created_at) VALUES (900, 1, 1, $1), (800, 1, 1, $2)",
                            OLD + timedelta(hours=2), OLD + timedelta(hours=1))
        cut = datetime(2026, 10, 11, 12, 14, 30, tzinfo=timezone.utc)
        ads = await bf.pending_adoptions(10, cut)
        assert [a["name"] for a in ads] == ["c1"]                                            # the new one was logged live
        pics = await bf.pending_pictures(10, cut)
        assert [p["id"] for p in pics] == [1] and pics[0]["first_guild"] == 800              # earliest spawn wins
        assert await bf.mark("adoption", ads[0]["id"], "retry") == "retry"
        assert [a["id"] for a in await bf.pending_adoptions(10, cut)] == [ads[0]["id"]]      # retried later
        assert await bf.mark("adoption", ads[0]["id"], "retry") == "retry"
        assert await bf.mark("adoption", ads[0]["id"], "retry") == "skipped"                 # 3rd failure gives up
        assert await bf.pending_adoptions(10, cut) == []
        assert await bf.mark("picture", 1, "done") == "done" and await bf.pending_pictures(10, cut) == []
        assert await bf.mark("picture", 1, "done") == "done"                                 # marking twice is harmless
        await pool.close()
    run(go())
