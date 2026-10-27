import asyncio

from modules import data_purge


def run(coro):
    return asyncio.run(coro)


class FakeConn:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on

    async def execute(self, sql, *args):
        if any(f'"{t}"' in sql for t in self.fail_on):
            raise RuntimeError("boom")
        self.calls.append((sql, args))
        return "DELETE 3"


class FakePool:
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


PLAN = {
    "discord_xp": {"guild": "bigint", "clone": True, "user": "bigint"},
    "discord_welcome_designs": {"guild": "bigint", "clone": False, "user": None},
    "discord_guild_subscriptions": {"guild": "bigint", "clone": True, "user": None},
    "discord_quarantined": {"guild": "bigint", "clone": True, "user": "bigint"},
    "text_guild_table": {"guild": "text", "clone": False, "user": "text"},
}


def setup(monkeypatch, conn):
    async def fake_pool():
        return FakePool(conn)

    async def fake_plan(refresh=False):
        return PLAN
    monkeypatch.setattr(data_purge, "get_pool", fake_pool)
    monkeypatch.setattr(data_purge, "_plan", fake_plan)


def tables(conn):
    return {sql.split('"')[1] for sql, _ in conn.calls}


def test_guild_purge_main_bot_clears_everything_except_keep(monkeypatch):
    conn = FakeConn()
    setup(monkeypatch, conn)
    res = run(data_purge.purge_guild(42, None))
    assert tables(conn) == {"discord_xp", "discord_welcome_designs", "discord_quarantined", "text_guild_table"}
    assert "discord_guild_subscriptions" not in tables(conn)
    assert res["failed"] == []


def test_guild_purge_clone_only_touches_clone_scoped_tables(monkeypatch):
    conn = FakeConn()
    setup(monkeypatch, conn)
    run(data_purge.purge_guild(42, 7))
    assert tables(conn) == {"discord_xp", "discord_quarantined"}
    for sql, args in conn.calls:
        assert "IS NOT DISTINCT FROM" in sql and args[1] == 7


def test_text_columns_are_cast_and_bound_as_text(monkeypatch):
    conn = FakeConn()
    setup(monkeypatch, conn)
    run(data_purge.purge_guild(42, None))
    sql, args = [c for c in conn.calls if '"text_guild_table"' in c[0]][0]
    assert '::text = $1' in sql and args == ("42",)


def test_member_purge_is_scoped_to_one_user_and_skips_safety_tables(monkeypatch):
    conn = FakeConn()
    setup(monkeypatch, conn)
    run(data_purge.purge_member(42, 99, None))
    assert tables(conn) == {"discord_xp", "text_guild_table"}
    sql, args = [c for c in conn.calls if '"discord_xp"' in c[0]][0]
    assert args == (42, None, 99)
    assert "discord_quarantined" not in tables(conn)


def test_one_failing_table_does_not_stop_the_rest(monkeypatch):
    conn = FakeConn(fail_on=("discord_xp",))
    setup(monkeypatch, conn)
    res = run(data_purge.purge_guild(42, None))
    assert res["failed"] == ["discord_xp"]
    assert "discord_welcome_designs" in tables(conn)


def test_sweep_purges_confirmed_departures_and_skips_present_guilds(monkeypatch):
    purged = []

    async def fake_purge(gid, clone_id=None):
        purged.append(gid)
        return {"removed": {}, "failed": []}
    monkeypatch.setattr(data_purge, "purge_guild", fake_purge)
    rows = [{"guild_id": 1, "left_at": "x"}, {"guild_id": 2, "left_at": None}, {"guild_id": 3, "left_at": "x"}]
    run(data_purge.sweep_departed_guilds(rows, {2, 3}, None))
    assert purged == [1]


def test_sweep_refuses_mass_purge_of_still_joined_guilds(monkeypatch):
    purged = []

    async def fake_purge(gid, clone_id=None):
        purged.append(gid)
        return {"removed": {}, "failed": []}
    monkeypatch.setattr(data_purge, "purge_guild", fake_purge)
    rows = [{"guild_id": i, "left_at": None} for i in range(1, 21)]
    run(data_purge.sweep_departed_guilds(rows, {1, 2}, None))
    assert purged == []


def test_sweep_does_nothing_when_no_guilds_are_visible(monkeypatch):
    called = []

    async def fake_purge(*a, **k):
        called.append(1)
    monkeypatch.setattr(data_purge, "purge_guild", fake_purge)
    assert run(data_purge.sweep_departed_guilds([{"guild_id": 1, "left_at": "x"}], set(), None)) == []
    assert not called


# ── /mydata: one person across every server and bot ──────────────────────

class FakeConn2(FakeConn):
    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return 2


def test_user_purge_deletes_across_all_servers_and_bots_except_kept(monkeypatch):
    conn = FakeConn2()
    setup(monkeypatch, conn)
    res = run(data_purge.purge_user_everywhere(99))
    assert tables(conn) == {"discord_xp", "text_guild_table"}   # quarantine (kept) and no-user tables skipped
    for sql, args in conn.calls:
        assert "guild_id" not in sql and "clone_id" not in sql   # every server, every clone
    assert [a for s, a in conn.calls if '"discord_xp"' in s] == [(99,)]
    assert [a for s, a in conn.calls if '"text_guild_table"' in s] == [("99",)]
    assert res["failed"] == [] and res["removed"]["XP and levels"] == 3


def test_user_purge_reports_failed_tables(monkeypatch):
    conn = FakeConn2(fail_on=("discord_xp",))
    setup(monkeypatch, conn)
    res = run(data_purge.purge_user_everywhere(99))
    assert res["failed"] == ["discord_xp"] and "text guild table" in res["removed"]


def test_user_summary_counts_the_same_tables(monkeypatch):
    conn = FakeConn2()
    setup(monkeypatch, conn)
    out = run(data_purge.user_summary(99))
    assert out == {"XP and levels": 2, "text guild table": 2}


def test_mydata_delete_fails_cleanly_when_a_table_cannot_be_cleared(monkeypatch):
    import types as _t, sys as _s
    from modules import my_data

    async def fake_member_delete(uid, body, db):
        return {"deleted": True, "removed": {"card_design": 1}}
    fake_api = _t.ModuleType("api.dash_member")
    fake_api.member_data_delete = fake_member_delete
    fake_api.DELETE_PHRASE = "DELETE MY DATA"
    monkeypatch.setitem(_s.modules, "api.dash_member", fake_api)
    pkg = _t.ModuleType("api"); pkg.dash_member = fake_api
    monkeypatch.setitem(_s.modules, "api", pkg)

    async def fake_purge(uid):
        return {"removed": {"coins": 4}, "failed": ["discord_xp"]}
    monkeypatch.setattr(my_data.data_purge, "purge_user_everywhere", fake_purge)
    res = run(my_data.delete_all(99, object()))
    assert res["ok"] is False and "again" in res["message"] and res["removed"] == {"card_design": 1, "coins": 4}

    async def ok_purge(uid):
        return {"removed": {"coins": 4}, "failed": []}
    monkeypatch.setattr(my_data.data_purge, "purge_user_everywhere", ok_purge)
    res = run(my_data.delete_all(99, object()))
    assert res["ok"] is True and res["removed"] == {"card_design": 1, "coins": 4}
