"""Clone replacement: the live-clone blocker, the one-time confirmation, and the real data move (against a real Postgres when one is
available: set TEST_PG_URL or run a local one on :5433; otherwise those tests skip)."""
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules import clone_transfer as ct

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def run(c):
    return asyncio.run(c)


# ---- pure rules -----------------------------------------------------------------------------------------------------------

def test_a_clone_that_beat_recently_is_live_and_blocks_everything():
    row = {"status": "active"}
    assert ct.classify_source(row, NOW - timedelta(minutes=3), NOW) == ct.LIVE
    assert ct.classify_source(row, NOW - timedelta(minutes=19), NOW) == ct.LIVE         # "delayed" still counts as live (be careful)
    assert ct.classify_source(row, NOW - timedelta(minutes=21), NOW) == ct.DOWN
    assert ct.classify_source(row, None, NOW) == ct.DOWN
    assert ct.classify_source({"status": "inactive"}, NOW, NOW) == ct.INACTIVE           # switched off wins over a fresh heartbeat
    assert ct.classify_source(None, None, NOW) == ct.REMOVED
    assert ct.classify_source(row, datetime(2026, 11, 3, 11, 55), NOW) == ct.LIVE        # naive timestamps are treated as UTC


def test_pair_check_blocks_live_same_and_inactive_target():
    active = {"status": "active"}
    assert ct.check_pair(1, 2, ct.LIVE, active)[0] is False and "ONLINE" in ct.check_pair(1, 2, ct.LIVE, active)[1]
    assert ct.check_pair(1, 1, ct.DOWN, active)[0] is False
    assert ct.check_pair(1, 2, ct.DOWN, None)[0] is False and ct.check_pair(1, 2, ct.DOWN, {"status": "inactive"})[0] is False
    for state in (ct.DOWN, ct.INACTIVE, ct.REMOVED):
        assert ct.check_pair(1, 2, state, active) == (True, "")


def test_billing_listings_tokens_and_retired_tables_are_never_moved():
    for t in ("discord_premium_groups", "discord_guild_subscriptions", "discord_pro_guilds", "listing_boost_payments", "verification_purchases",
              "discord_guilds", "discord_dashboard_tokens", "bump_queue", "bump_listings", "server_listings", "server_listing_votes",
              "heist_runs", "card_trades", "guild_terms_acceptance", "discord_invite_cache"):
        assert not ct.is_movable(t) and ct.deny_reason(t), t
    for t in ("discord_xp", "discord_economy_balances", "discord_tickets", "discord_reaction_roles", "discord_scam_shield_guild"):
        assert ct.is_movable(t), t
    assert not ct.is_movable('x"; DROP TABLE y; --') and not ct.is_movable("Bad Name")


def test_every_deny_list_name_is_a_real_table():
    src = (ROOT / "database.py").read_text() + "".join(p.read_text() for p in (ROOT / "sql").glob("*.sql")) + \
        "".join(p.read_text() for p in (ROOT / "database/migrations").glob("*.sql"))
    for name in ct.DENY_EXACT:
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {name}\b", src), f"{name} in DENY_EXACT is not a table (typo = silently moved!)"


def test_panel_checklist_only_for_things_that_moved():
    out = ct.panel_checklist({"discord_reaction_roles", "discord_tickets", "discord_xp"})
    assert any("/reactionrole repost" in l for l in out) and any("Open tickets" in l for l in out) and len(out) == 2
    assert ct.panel_checklist({"discord_xp"}) == []


def test_wiring():
    bot = (ROOT / "discord_bot/bot.py").read_text()
    assert "clone_transfer.auto_migrate_on_join(guild.id, self.clone_id)" in bot
    assert "db.migrate_guild_data_if_orphaned(guild.id" not in bot                      # the unconditional migration is gone
    assert bot.index("cogs.clone_admin") < bot.index("cogs.clone_transfer")             # /admin exists before we mount under it
    cog = (ROOT / "discord_bot/cogs/clone_transfer.py").read_text()
    assert cog.count("if not _owner(interaction)") == 2 and 'subgroup="clones"' in cog
    assert "async def repost" in (ROOT / "discord_bot/cogs/reaction_roles.py").read_text()


def test_confirm_button_rechecks_the_blocker_and_only_the_owner_can_press_it(monkeypatch):
    from discord_bot.cogs import clone_transfer as cog
    view = cog._ConfirmView(None, 42, 1, 2, ["discord_xp"], [111])
    sent = []

    async def send_message(*a, **k):
        sent.append(("send", a, k))

    async def edit_message(*a, **k):
        sent.append(("edit", a, k))

    async def edit_original(*a, **k):
        sent.append(("orig", a, k))
    stranger = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(send_message=send_message))
    assert run(view.interaction_check(stranger)) is False and sent
    # the source comes back online between the preview and the click
    async def live(old):
        return ct.LIVE
    called = []

    async def authorize(*a):
        called.append(a)
    monkeypatch.setattr(ct, "source_state", live)
    monkeypatch.setattr(ct, "authorize", authorize)
    owner = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(edit_message=edit_message), edit_original_response=edit_original)
    run(view.confirm.callback(owner))
    assert called == [] and any("came back online" in str(s[2].get("content", "")) for s in sent if s[0] == "orig")


# ---- real Postgres -----------------------------------------------------------------------------------------------------

PG_URL = os.environ.get("TEST_PG_URL", "postgresql://postgres@localhost:5433/postgres")


def _pg_available():
    async def go():
        import asyncpg
        c = await asyncpg.connect(PG_URL, timeout=2)
        await c.close()
    try:
        run(go())
        return True
    except Exception:
        return False


needs_pg = pytest.mark.skipif(not _pg_available(), reason="no Postgres reachable (set TEST_PG_URL)")

SCHEMA = """
CREATE TABLE discord_guilds (guild_id BIGINT, clone_id INTEGER, left_at TIMESTAMPTZ);
CREATE TABLE discord_xp (guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, clone_id INTEGER, xp INT, UNIQUE (guild_id, user_id, clone_id));
CREATE TABLE discord_welcome_config (guild_id BIGINT NOT NULL, clone_id INTEGER, channel_id BIGINT);
CREATE UNIQUE INDEX wc_key ON discord_welcome_config (guild_id, COALESCE(clone_id, -1));
CREATE TABLE discord_reaction_roles (id SERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, clone_id INTEGER, channel_id BIGINT, message_id BIGINT, role_id BIGINT, UNIQUE (message_id, role_id));
CREATE TABLE discord_tickets (id SERIAL PRIMARY KEY, guild_id BIGINT NOT NULL, clone_id INTEGER, topic TEXT);
CREATE TABLE discord_premium_groups (guild_id BIGINT, clone_id INTEGER);
CREATE TABLE bump_queue (guild_id BIGINT, clone_id INTEGER);
CREATE TABLE no_clone_table (guild_id BIGINT);
CREATE TABLE discord_broken (guild_id BIGINT, clone_id INTEGER, CHECK (clone_id IS NULL OR clone_id <> 2));
"""
DATA = """
INSERT INTO discord_guilds VALUES (111,2,NULL),(222,2,NULL),(333,2,now());
INSERT INTO discord_xp VALUES (111,7,1,500),(111,8,1,300),(111,7,2,10),(222,9,1,50),(333,1,1,1);
INSERT INTO discord_welcome_config VALUES (111,1,5),(111,2,99);
INSERT INTO discord_reaction_roles (guild_id,clone_id,channel_id,message_id,role_id) VALUES (111,1,50,1000,5),(111,1,50,1000,6);
INSERT INTO discord_tickets (guild_id,clone_id,topic) VALUES (111,1,'a'),(111,1,'b'),(111,3,'someone else');
INSERT INTO discord_premium_groups VALUES (111,1);
INSERT INTO bump_queue VALUES (111,1);
INSERT INTO discord_broken VALUES (111,1);
"""


class OneConnPool:
    """Stand-in for database.get_pool() that hands out one connection pinned to the test schema."""
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self_):
                return pool.conn

            async def __aexit__(self_, *a):
                return False
        return Ctx()


def with_db(fn):
    """Runs `fn(conn, pool)` inside a throw-away schema; drops it afterwards."""
    async def go():
        import asyncpg
        conn = await asyncpg.connect(PG_URL)
        schema = "ct_test_" + os.urandom(3).hex()
        try:
            await conn.execute(f"CREATE SCHEMA {schema}; SET search_path TO {schema}")
            await conn.execute(SCHEMA)
            await conn.execute(DATA)
            ct._ready = False
            return await fn(conn, OneConnPool(conn))
        finally:
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA {schema} CASCADE")
            await conn.close()
    return run(go())


@needs_pg
def test_discovery_finds_every_guild_clone_table_and_explains_the_ones_it_skips():
    async def body(conn, pool):
        tables, skipped = await ct.discover_tables(conn)
        assert set(tables) == {"discord_xp", "discord_welcome_config", "discord_reaction_roles", "discord_tickets", "discord_broken"}
        assert set(skipped) == {"discord_guilds", "discord_premium_groups", "bump_queue"}      # no_clone_table is not a candidate at all
    with_db(body)


@needs_pg
def test_move_keeps_the_new_clones_rows_skips_collisions_and_touches_nothing_else():
    async def body(conn, pool):
        tables, _ = await ct.discover_tables(conn)
        plan = await ct.plan_guild(conn, tables, 111, 1)
        assert plan["discord_xp"] == 2 and plan["discord_tickets"] == 2
        res = await ct.transfer_guild(conn, tables, 111, 1, 2)
        assert res["moved"]["discord_tickets"] == 2 and res["moved"]["discord_reaction_roles"] == 2
        assert res["moved"]["discord_xp"] == 1 and res["skipped"]["discord_xp"] == 1           # user 8 moved, user 7 collided
        assert res["skipped"]["discord_welcome_config"] == 1
        assert dict(await conn.fetchrow("SELECT xp FROM discord_xp WHERE user_id = 7 AND clone_id = 2")) == {"xp": 10}   # new clone's row won
        assert await conn.fetchval("SELECT COUNT(*) FROM discord_tickets WHERE clone_id = 3") == 1        # another clone's data untouched
        assert await conn.fetchval("SELECT clone_id FROM discord_premium_groups") == 1                    # billing untouched
        assert await conn.fetchval("SELECT clone_id FROM bump_queue") == 1
        assert await conn.fetchval("SELECT COUNT(*) FROM discord_xp WHERE guild_id = 222 AND clone_id = 1") == 1   # other servers untouched
    with_db(body)


@needs_pg
def test_one_broken_table_is_reported_and_does_not_stop_or_undo_the_rest():
    async def body(conn, pool):
        tables, _ = await ct.discover_tables(conn)
        res = await ct.transfer_guild(conn, tables, 111, 1, 2)
        assert res["failed"] == {"discord_broken": "CheckViolationError"}
        assert res["moved"]["discord_tickets"] == 2
        assert await conn.fetchval("SELECT clone_id FROM discord_broken") == 1             # the failed table is unchanged, not half-moved
    with_db(body)


@needs_pg
def test_servers_to_move_only_lists_servers_where_the_new_clone_is_present_now():
    async def body(conn, pool):
        tables, _ = await ct.discover_tables(conn)
        assert await ct.servers_to_move(conn, tables, 1, 2) == [111, 222]                    # 333: the new clone left it
    with_db(body)


def _gate(monkeypatch, pool, state):
    async def p():
        return pool
    async def st(old):
        return state
    monkeypatch.setattr(ct, "_pool", p)
    monkeypatch.setattr(ct, "source_state", st)


@needs_pg
def test_live_source_is_never_touched_even_if_approved(monkeypatch):
    async def body(conn, pool):
        _gate(monkeypatch, pool, ct.LIVE)
        await ct.authorize(1, 2, 42)
        assert await ct.auto_migrate_on_join(111, 2) == []
        assert await conn.fetchval("SELECT COUNT(*) FROM discord_tickets WHERE clone_id = 1") == 2
        assert await conn.fetchval("SELECT COUNT(*) FROM clone_transfer_log") == 0
    with_db(body)


@needs_pg
def test_down_source_without_confirmation_is_blocked_and_logged_not_moved(monkeypatch):
    async def body(conn, pool):
        _gate(monkeypatch, pool, ct.DOWN)
        assert await ct.auto_migrate_on_join(111, 2) == []
        assert await conn.fetchval("SELECT COUNT(*) FROM discord_tickets WHERE clone_id = 1") == 2
        row = await conn.fetchrow("SELECT status, from_clone, to_clone FROM clone_transfer_log")
        assert (row["status"], row["from_clone"], row["to_clone"]) == ("blocked", 1, 2)
    with_db(body)


@needs_pg
def test_after_the_one_time_confirmation_joins_migrate_automatically_and_revoking_stops_it(monkeypatch):
    async def body(conn, pool):
        _gate(monkeypatch, pool, ct.DOWN)
        await ct.authorize(1, 2, 42)
        assert await ct.is_authorized(1, 2) and not await ct.is_authorized(2, 1)           # a pair, in one direction only
        assert await ct.auto_migrate_on_join(111, 2) == [1]
        assert await conn.fetchval("SELECT COUNT(*) FROM discord_tickets WHERE clone_id = 2") == 2
        assert await conn.fetchval("SELECT status FROM clone_transfer_log WHERE from_clone = 1") == "partial"   # discord_broken failed
        # server 111 also holds an unapproved third clone's data: it is reported as blocked, never moved
        assert await conn.fetchval("SELECT status FROM clone_transfer_log WHERE from_clone = 3") == "blocked"
        assert await conn.fetchval("SELECT COUNT(*) FROM discord_tickets WHERE clone_id = 3") == 1
        assert await ct.revoke(1, 2) and not await ct.is_authorized(1, 2)
        assert await ct.auto_migrate_on_join(222, 2) == []                                  # no longer approved: blocked again
        assert await conn.fetchval("SELECT status FROM clone_transfer_log WHERE guild_id = 222") == "blocked"
    with_db(body)


@needs_pg
def test_a_database_error_in_the_gate_moves_nothing(monkeypatch):
    async def body(conn, pool):
        async def boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(ct, "_pool", boom)
        assert await ct.auto_migrate_on_join(111, 2) == []
        assert not await ct.is_authorized(1, 2)                                             # unreadable authorization = not authorized
    with_db(body)


# ---- reaction-role panels after a swap -----------------------------------------------------------------------------------

@needs_pg
def test_repoint_moves_only_that_panel_of_that_server_and_clone(monkeypatch):
    import database

    async def body(conn, pool):
        await conn.execute("INSERT INTO discord_reaction_roles (guild_id,clone_id,channel_id,message_id,role_id) VALUES (111,2,50,2000,9),(222,1,60,1000,7)")

        async def gp():
            return pool
        monkeypatch.setattr(database, "get_pool", gp)
        assert await database.db.repoint_reaction_role_message(111, 1, 1000, 5555, 77) == 2
        rows = await conn.fetch("SELECT guild_id, clone_id, message_id, channel_id FROM discord_reaction_roles ORDER BY id")
        assert [(r["guild_id"], r["clone_id"], r["message_id"], r["channel_id"]) for r in rows] == [
            (111, 1, 5555, 77), (111, 1, 5555, 77), (111, 2, 2000, 50), (222, 1, 1000, 60)]
    with_db(body)


def test_repost_replaces_another_bots_panel_and_leaves_its_own_alone(monkeypatch):
    from discord_bot.cogs import reaction_roles as rr
    cog = rr.ReactionRolesCog.__new__(rr.ReactionRolesCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    sent, repointed, deleted = [], [], []

    class Chan:
        id = 50

        async def send(self, embed=None, view=None):
            sent.append((embed, len(view.children)))
            return SimpleNamespace(id=7000)

    class OldMsg:
        def __init__(self, author):
            self.author, self.channel = SimpleNamespace(id=author), Chan()
            self.embeds = [SimpleNamespace(to_dict=lambda: {"title": "Pick roles", "type": "rich"})]

        async def delete(self):
            deleted.append(1)

    msgs = {1000: OldMsg(111), 2000: OldMsg(999)}                           # 1000: old bot's panel, 2000: already ours

    async def find(guild, mid):
        return msgs.get(mid)
    cog._find_panel_message = find

    async def panels(gid, clone_id=None):
        return [{"channel_id": 50, "message_id": 1000, "roles": [{"role_id": 5, "label": "A", "emoji": None}, {"role_id": 6, "label": "B", "emoji": None}]},
                {"channel_id": 50, "message_id": 2000, "roles": [{"role_id": 9, "label": "C", "emoji": None}]}]

    async def repoint(*a):
        repointed.append(a)
        return 2
    monkeypatch.setattr(rr.db, "get_reaction_role_panels_for_guild", panels)
    monkeypatch.setattr(rr.db, "repoint_reaction_role_message", repoint)
    monkeypatch.setattr(rr, "_require_perm", lambda i, p: True)
    out = []

    async def defer(**k):
        pass

    async def follow(msg, **k):
        out.append(msg)
    inter = SimpleNamespace(guild=SimpleNamespace(), guild_id=5, client=SimpleNamespace(),
                            response=SimpleNamespace(defer=defer), followup=SimpleNamespace(send=follow))
    monkeypatch.setattr(rr, "_clone_id_of", lambda i: 2)
    run(rr.ReactionRolesCog.repost.callback(cog, inter))
    assert len(sent) == 1 and sent[0][1] == 2 and sent[0][0].title == "Pick roles"        # same embed, both role buttons
    assert repointed == [(5, 2, 1000, 7000, 50)] and deleted == [1]
    assert "Re-posted **1**" in out[0] and "1 were already mine" in out[0]
