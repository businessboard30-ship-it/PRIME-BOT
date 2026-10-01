"""Owner-panel Batch 3 tests: abuse watchlist, report queue, status editor, honeypot overview."""
import asyncio
import importlib
import pathlib
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, HELPER, OTHER = 111, 333, 222
MAIN = "discord_bot.cogs._views_admin_panel"
CTRL = "discord_bot.cogs._views_admin_panel_controls"
SAFV = "discord_bot.cogs._views_admin_panel_safety"
AC = "modules.admin_controls"
SAF = "modules.admin_safety"
NOW = datetime.now(timezone.utc)
MODULES = (MAIN, CTRL, SAFV, AC, SAF)
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(SAFV)
    mod.main = main
    mod.ac = importlib.import_module(AC)
    mod.safety = importlib.import_module(SAF)
    mod.audit = MagicMock()          # capture audit calls
    mod.cfg = cfg
    mod.safety._status_cache.update(ts=0.0, entries=[], presence="online")
    yield mod
    for name in MODULES:
        sys.modules.pop(name, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = None
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    i.response.is_done = MagicMock(return_value=False)
    i.followup.edit_message = AsyncMock()
    return i


def cog(guilds=None):
    c = MagicMock()
    c.bot.get_guild.return_value = None
    c.bot.guilds = guilds or []
    return c


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def press(view, label, **kw):
    i = I(**kw)
    run(buttons(view)[label].callback(i))
    return i


def pick(view, index, value, **kw):
    i = I(values=[str(value)], **kw)
    run(selects(view)[index].callback(i))
    return i


# ── fake database that behaves like the real tables ──────────────────────

class FakeConn:
    def __init__(self, fail=False):
        self.fail = fail
        self.watch = {"user": [], "guild": []}
        self.watch_args = None
        self.reports = {}            # id -> dict(guild_id, reason, created_at, status, reviewed_by)
        self.entries = {}            # id -> dict(kind, text)
        self.config = {}
        self.honeypot = []
        self.blacklist = {}
        self._next = 1

    def _check(self):
        if self.fail:
            raise RuntimeError("db down")

    async def fetch(self, sql, *a):
        self._check()
        if "FROM moderation_logs" in sql:
            kind = "user" if "target_user_id AS target_id" in sql else "guild"
            self.watch_args = (kind, a)
            return [dict(r) for r in self.watch[kind]][: a[1]]
        if "SELECT status, COUNT" in sql:
            out = {}
            for r in self.reports.values():
                out[r["status"]] = out.get(r["status"], 0) + 1
            return [{"status": k, "n": v} for k, v in out.items()]
        if "FROM server_listing_reports" in sql:
            rows = [{"id": k, **v} for k, v in self.reports.items() if v["status"] == "new"]
            rows.sort(key=lambda r: -r["id"])
            return rows[: a[0]]
        if "FROM bot_status_entries" in sql:
            return [{"id": k, "created_by": OWNER, "created_at": NOW, **v} for k, v in sorted(self.entries.items())][: a[0]]
        if "FROM bot_status_config" in sql:
            return [{"key": k, "value": v} for k, v in self.config.items()]
        if "COUNT(*) AS configured" in sql:
            en = sum(1 for r in self.honeypot if r["enabled"])
            return [{"configured": len(self.honeypot), "enabled": en,
                     "triggers": sum(r["triggered_count"] for r in self.honeypot)}]
        if "FROM discord_honeypot_config" in sql:
            return [dict(r) for r in self.honeypot][: a[0]]
        return []

    async def fetchval(self, sql, *a):
        self._check()
        if "COUNT(*) FROM bot_status_entries" in sql:
            return len(self.entries)
        return 0

    async def execute(self, sql, *a):
        self._check()
        if "INSERT INTO bot_blacklist" in sql:
            self.blacklist[(a[0], a[1])] = a[2]
            return "INSERT 0 1"
        if "UPDATE server_listing_reports" in sql:
            r = self.reports.get(a[0])
            if r and r["status"] == "new":
                r["status"], r["reviewed_by"] = a[1], a[2]
                return "UPDATE 1"
            return "UPDATE 0"
        if "INSERT INTO bot_status_entries" in sql:
            self.entries[self._next] = {"kind": a[0], "text": a[1]}
            self._next += 1
            return "INSERT 0 1"
        if "DELETE FROM bot_status_entries" in sql:
            if "WHERE" in sql:
                existed = a[0] in self.entries
                self.entries.pop(a[0], None)
                return "DELETE 1" if existed else "DELETE 0"
            n = len(self.entries)
            self.entries.clear()
            return f"DELETE {n}"
        if "INSERT INTO bot_status_config" in sql:
            self.config["presence"] = a[0]
            return "INSERT 0 1"
        if "DELETE FROM bot_status_config" in sql:
            self.config.pop("presence", None)
            return "DELETE 1"
        return "OK"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(s): return conn
            async def __aexit__(s, *e): return False
        return Ctx()


@pytest.fixture()
def db(vp, monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    monkeypatch.setattr(vp.ac, "_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(vp.safety, "_pool", AsyncMock(return_value=pool))
    vp.ac._helpers.update(ts=0.0, ok=False, map={})
    return conn


def wl_row(target, hits, guilds=1):
    return {"target_id": target, "hits": hits, "guilds": guilds, "last_at": NOW}


def report(guild=555000000000000001, reason="scam invite spam", status="new"):
    return {"guild_id": guild, "reason": reason, "created_at": NOW, "status": status, "reviewed_by": None}


def load(view):
    run(view.load())
    return view


# ── abuse watchlist ──────────────────────────────────────────────────────

def test_watchlist_shows_ranked_users(vp, db):
    db.watch["user"] = [wl_row(900000000000000001, 12, 3), wl_row(900000000000000002, 4)]
    v = load(vp.WatchlistView(cog(), OWNER))
    t = text_of(v)
    assert "900000000000000001" in t and "12 actions" in t and "3 server(s)" in t
    assert t.index("900000000000000001") < t.index("900000000000000002")
    assert db.watch_args == ("user", (7, 10))


def test_watchlist_toggles_to_servers_and_30_days(vp, db):
    db.watch["guild"] = [wl_row(800000000000000001, 40)]
    v = load(vp.WatchlistView(cog(), OWNER))
    press(v, "Show servers")
    press(v, "30 days")
    assert db.watch_args == ("guild", (30, 10))
    assert "800000000000000001" in text_of(v)


def test_watchlist_blacklist_user_happy_path(vp, db):
    db.watch["user"] = [wl_row(900000000000000001, 12)]
    v = load(vp.WatchlistView(cog(), OWNER))
    assert buttons(v)["Add to blacklist"].disabled
    pick(v, 0, 900000000000000001)
    assert not buttons(v)["Add to blacklist"].disabled
    press(v, "Add to blacklist")
    assert ("user", 900000000000000001) in db.blacklist
    assert "12 auto-mod actions" in db.blacklist[("user", 900000000000000001)]
    vp.audit.assert_called_once()
    assert vp.audit.call_args.args[1] == "watchlist.blacklist"
    assert v.selected is None


def test_watchlist_blacklist_server(vp, db):
    db.watch["guild"] = [wl_row(800000000000000001, 40)]
    v = load(vp.WatchlistView(cog(), OWNER))
    press(v, "Show servers")
    pick(v, 0, 800000000000000001)
    press(v, "Add to blacklist")
    assert ("guild", 800000000000000001) in db.blacklist


def test_watchlist_db_failure_on_blacklist_changes_nothing(vp, db):
    db.watch["user"] = [wl_row(900000000000000001, 12)]
    v = load(vp.WatchlistView(cog(), OWNER))
    pick(v, 0, 900000000000000001)
    db.fail = True
    press(v, "Add to blacklist")
    assert db.blacklist == {}
    assert "Nothing was changed" in text_of(v)
    vp.audit.assert_not_called()


def test_watchlist_load_failure_shows_message_not_exception(vp, db):
    db.fail = True
    v = load(vp.WatchlistView(cog(), OWNER))
    assert "Couldn't load" in text_of(v)


def test_watchlist_refuses_to_block_an_owner(vp, db):
    db.watch["user"] = [wl_row(OWNER, 99)]
    v = load(vp.WatchlistView(cog(), OWNER))
    pick(v, 0, OWNER)
    press(v, "Add to blacklist")
    assert db.blacklist == {}
    assert "owner" in text_of(v)


# ── report queue ─────────────────────────────────────────────────────────

def test_reports_queue_lists_only_new_with_counts(vp, db):
    db.reports = {1: report(status="reviewed"), 2: report(reason="fresh one"), 3: report(status="dismissed")}
    v = load(vp.ReportsView(cog(), OWNER))
    t = text_of(v)
    assert "fresh one" in t and "#1" not in t
    assert "New 1" in t and "reviewed 1" in t and "dismissed 1" in t


def test_reports_mark_reviewed_and_dismiss_refresh_the_queue(vp, db):
    db.reports = {1: report(reason="aaa"), 2: report(reason="bbb")}
    v = load(vp.ReportsView(cog(), OWNER))
    pick(v, 0, 1)
    press(v, "Mark reviewed")
    assert db.reports[1]["status"] == "reviewed" and db.reports[1]["reviewed_by"] == OWNER
    assert "aaa" not in text_of(v) and "bbb" in text_of(v)
    pick(v, 0, 2)
    press(v, "Dismiss")
    assert db.reports[2]["status"] == "dismissed"
    assert [c.args[1] for c in vp.audit.call_args_list] == ["report.reviewed", "report.dismissed"]


def test_reports_stale_second_press_is_a_noop(vp, db):
    db.reports = {1: report()}
    v = load(vp.ReportsView(cog(), OWNER))
    pick(v, 0, 1)
    db.reports[1]["status"] = "dismissed"     # someone else handled it meanwhile
    press(v, "Mark reviewed")
    assert db.reports[1]["status"] == "dismissed"
    assert "already handled" in text_of(v)
    vp.audit.assert_not_called()


def test_reports_blacklist_is_two_step(vp, db):
    gid = 555000000000000009
    db.reports = {1: report(guild=gid)}
    v = load(vp.ReportsView(cog(), OWNER))
    pick(v, 0, 1)
    press(v, "Blacklist server")
    assert db.blacklist == {} and db.reports[1]["status"] == "new"
    assert "Confirm blacklist" in buttons(v)
    press(v, "Confirm blacklist")
    assert ("guild", gid) in db.blacklist
    assert db.reports[1]["status"] == "reviewed"
    assert vp.audit.call_args.args[1] == "report.blacklist"


def test_reports_confirm_resets_when_selection_changes(vp, db):
    db.reports = {1: report(), 2: report()}
    v = load(vp.ReportsView(cog(), OWNER))
    pick(v, 0, 1)
    press(v, "Blacklist server")
    pick(v, 0, 2)
    assert "Blacklist server" in buttons(v) and "Confirm blacklist" not in buttons(v)
    assert db.blacklist == {}


def test_reports_blacklist_db_failure_leaves_everything_untouched(vp, db):
    db.reports = {1: report()}
    v = load(vp.ReportsView(cog(), OWNER))
    pick(v, 0, 1)
    press(v, "Blacklist server")
    db.fail = True
    press(v, "Confirm blacklist")
    assert db.blacklist == {} and db.reports[1]["status"] == "new"
    assert "Nothing was changed" in text_of(v)


def test_reports_resolve_db_failure_changes_nothing(vp, db):
    db.reports = {1: report()}
    v = load(vp.ReportsView(cog(), OWNER))
    pick(v, 0, 1)
    db.fail = True
    press(v, "Dismiss")
    assert db.reports[1]["status"] == "new"
    assert "Nothing was changed" in text_of(v)
    vp.audit.assert_not_called()


def test_reports_untrusted_text_cannot_ping_or_format(vp, db):
    db.reports = {1: report(reason="@everyone <@123456789012345678> **free nitro**")}
    v = load(vp.ReportsView(cog(), OWNER))
    t = text_of(v)
    assert "@everyone" not in t and "<@123456789012345678>" not in t and "**free nitro**" not in t


def test_reports_use_server_name_when_cached(vp, db):
    db.reports = {1: report(guild=555000000000000001)}
    c = cog()
    c.bot.get_guild.return_value = SimpleNamespace(name="Cool *Server*")
    t = text_of(load(vp.ReportsView(c, OWNER)))
    assert "Cool" in t and "*Server*" not in t.replace("\\*Server\\*", "")


def test_resolve_report_sql_only_touches_new_rows(vp, db):
    # The fake table enforces this itself, so also pin the real SQL: this guard
    # is what makes a double press (or two owners) a no-op in Postgres.
    db.reports = {1: report()}
    seen = []
    orig = db.execute

    async def spy(sql, *a):
        seen.append(sql)
        return await orig(sql, *a)
    db.execute = spy
    assert run(vp.safety.resolve_report(1, "reviewed", OWNER)) is True
    assert run(vp.safety.resolve_report(1, "dismissed", OWNER)) is False
    assert all("AND status = 'new'" in q for q in seen) and len(seen) == 2
    assert db.reports[1]["status"] == "reviewed"


# ── status editor ────────────────────────────────────────────────────────

def test_status_text_helpers(vp):
    s = vp.safety
    assert s.render_status_text("{servers} servers {members} {oops}", 5, 12345) == "5 servers 12,345 {oops}"
    assert s.render_status_text("a{b", 1, 1) == "a{b"
    assert s.clean_status_text("  hi \n  there ") == "hi there"
    assert len(s.clean_status_text("x" * 500)) == s.STATUS_TEXT_MAX


def test_status_add_via_modal_and_preview(vp, db):
    c = cog([SimpleNamespace(member_count=10), SimpleNamespace(member_count=5)])
    v = load(vp.StatusView(c, OWNER))
    assert "built-in rotation" in text_of(v)
    pick(v, 0, "watching")
    i = press(v, "Add entry")
    modal = i.response.send_modal.call_args.args[0]
    modal.text._value = "{servers} servers • {members} people"
    mi = I()
    run(modal.on_submit(mi))
    assert db.entries[1] == {"kind": "watching", "text": "{servers} servers • {members} people"}
    new_view = mi.response.edit_message.call_args.kwargs["view"]
    assert "Watching 2 servers • 15 people" in text_of(new_view)
    assert vp.audit.call_args.args[1] == "status.add"


def test_status_modal_rechecks_authorization(vp, db):
    modal = vp.StatusAddModal(cog(), "playing")
    modal.text._value = "hacked"
    mi = I(user=OTHER)
    run(modal.on_submit(mi))
    assert db.entries == {}
    assert "no longer authorized" in mi.response.send_message.call_args.args[0]


def test_status_modal_refuses_a_helper_even_with_every_grantable_section(vp, db):
    for s in vp.ac.GRANTABLE:
        vp.ac._helpers["map"].setdefault(HELPER, set()).add(s)
    modal = vp.StatusAddModal(cog(), "playing")
    modal.text._value = "nope"
    mi = I(user=HELPER)
    run(modal.on_submit(mi))
    assert db.entries == {}


def test_status_modal_rejects_empty_and_db_failure(vp, db):
    modal = vp.StatusAddModal(cog(), "playing")
    modal.text._value = "   "
    mi = I()
    run(modal.on_submit(mi))
    assert db.entries == {} and "can't be empty" in mi.response.send_message.call_args.args[0]
    modal.text._value = "fine"
    db.fail = True
    mi = I()
    run(modal.on_submit(mi))
    assert db.entries == {} and "Nothing was changed" in mi.response.send_message.call_args.args[0]


def test_status_list_is_capped(vp, db):
    for n in range(vp.safety.STATUS_MAX_ENTRIES):
        db.entries[n + 1] = {"kind": "playing", "text": f"t{n}"}
    db._next = 99
    v = load(vp.StatusView(cog(), OWNER))
    assert buttons(v)["Add entry"].disabled
    modal = vp.StatusAddModal(cog(), "playing")
    modal.text._value = "one too many"
    mi = I()
    run(modal.on_submit(mi))
    assert len(db.entries) == vp.safety.STATUS_MAX_ENTRIES
    assert "full" in text_of(mi.response.edit_message.call_args.kwargs["view"])


def test_status_remove_selected(vp, db):
    db.entries = {1: {"kind": "playing", "text": "keep"}, 2: {"kind": "playing", "text": "drop"}}
    v = load(vp.StatusView(cog(), OWNER))
    assert buttons(v)["Remove selected"].disabled
    pick(v, 2, 2)
    press(v, "Remove selected")
    assert list(db.entries) == [1]
    assert "drop" not in text_of(v) and "keep" in text_of(v)
    assert vp.audit.call_args.args[1] == "status.remove"


def test_status_presence_persists_and_db_failure_changes_nothing(vp, db):
    v = load(vp.StatusView(cog(), OWNER))
    pick(v, 1, "dnd")
    assert db.config["presence"] == "dnd" and "Do not disturb" in text_of(v)
    db.fail = True
    pick(v, 1, "idle")
    assert db.config["presence"] == "dnd"
    assert "Nothing was changed" in text_of(v)


def test_status_reset_is_two_step(vp, db):
    db.entries = {1: {"kind": "playing", "text": "x"}}
    db.config["presence"] = "idle"
    v = load(vp.StatusView(cog(), OWNER))
    press(v, "Reset to default")
    assert db.entries and "Confirm reset" in buttons(v)
    press(v, "Confirm reset")
    assert db.entries == {} and "presence" not in db.config
    assert "built-in rotation" in text_of(v)
    assert vp.audit.call_args.args[1] == "status.reset"


def test_status_reset_db_failure_keeps_entries(vp, db):
    db.entries = {1: {"kind": "playing", "text": "x"}}
    v = load(vp.StatusView(cog(), OWNER))
    press(v, "Reset to default")
    db.fail = True
    press(v, "Confirm reset")
    assert db.entries
    assert "Nothing was changed" in text_of(v)


def test_status_view_changing_a_select_cancels_pending_reset(vp, db):
    db.entries = {1: {"kind": "playing", "text": "x"}}
    v = load(vp.StatusView(cog(), OWNER))
    press(v, "Reset to default")
    pick(v, 0, "listening")
    assert "Reset to default" in buttons(v)


def test_status_text_stays_within_discord_limits(vp, db):
    for n in range(vp.safety.STATUS_MAX_ENTRIES):
        db.entries[n + 1] = {"kind": "competing", "text": "y" * vp.safety.STATUS_TEXT_MAX}
    v = load(vp.StatusView(cog(), OWNER))
    assert len(text_of(v)) < 4000
    for s in selects(v):
        assert len(s.options) <= 25


# ── status rotation loader (what bot.py reads) ───────────────────────────

def test_rotation_loader_returns_custom_entries(vp, db):
    db.entries = {1: {"kind": "watching", "text": "hello"}}
    db.config["presence"] = "idle"
    assert run(vp.safety.load_status_rotation()) == ([("watching", "hello")], "idle")


def test_rotation_loader_defaults_when_db_is_down_and_never_raises(vp, db):
    db.fail = True
    assert run(vp.safety.load_status_rotation()) == ([], "online")


def test_rotation_loader_keeps_last_good_value_when_db_goes_down(vp, db):
    db.entries = {1: {"kind": "playing", "text": "first"}}
    assert run(vp.safety.load_status_rotation())[0] == [("playing", "first")]
    vp.safety.invalidate_status()
    db.fail = True
    assert run(vp.safety.load_status_rotation())[0] == [("playing", "first")]


def test_rotation_loader_is_cached_until_invalidated(vp, db):
    db.entries = {1: {"kind": "playing", "text": "one"}}
    run(vp.safety.load_status_rotation())
    db.entries[2] = {"kind": "playing", "text": "two"}
    assert len(run(vp.safety.load_status_rotation())[0]) == 1
    vp.safety.invalidate_status()
    assert len(run(vp.safety.load_status_rotation())[0]) == 2


def test_panel_changes_invalidate_the_cache(vp, db):
    run(vp.safety.load_status_rotation())
    run(vp.safety.add_status_entry("playing", "fresh", OWNER))
    assert run(vp.safety.load_status_rotation())[0] == [("playing", "fresh")]


# ── honeypot overview ────────────────────────────────────────────────────

def test_honeypot_overview_is_read_only_and_shows_counters(vp, db):
    db.honeypot = [
        {"guild_id": 700000000000000001, "clone_id": None, "enabled": True, "action": "ban",
         "channel_id": 1, "triggered_count": 7, "last_triggered_at": NOW},
        {"guild_id": 700000000000000002, "clone_id": 4, "enabled": False, "action": "kick",
         "channel_id": 2, "triggered_count": 0, "last_triggered_at": None},
    ]
    v = load(vp.HoneypotView(cog(), OWNER))
    t = text_of(v)
    assert "**1** on of **2** configured" in t and "**7** total catches" in t
    assert "clone #4" in t and "never" in t
    assert set(buttons(v)) == {"Refresh", "Back"}
    assert "can't be listed" in t


def test_honeypot_failure_and_empty(vp, db):
    assert "No server has set up" in text_of(load(vp.HoneypotView(cog(), OWNER)))
    db.fail = True
    assert "Couldn't load" in text_of(load(vp.HoneypotView(cog(), OWNER)))


# ── access: owner-only ───────────────────────────────────────────────────

NEW_SECTIONS = {"watchlist", "reports", "status", "honeypot"}


def test_new_sections_are_not_grantable_to_helpers(vp):
    assert not (NEW_SECTIONS & set(vp.ac.GRANTABLE))


def test_owner_gets_new_sections_and_helper_never_does(vp, db):
    assert NEW_SECTIONS <= vp.main.allowed_sections(OWNER)
    for s in vp.ac.GRANTABLE:
        vp.ac._helpers["map"].setdefault(HELPER, set()).add(s)
    assert not (NEW_SECTIONS & vp.main.allowed_sections(HELPER))
    assert not (NEW_SECTIONS & vp.main.allowed_sections(OTHER))


@pytest.mark.parametrize("name", ["WatchlistView", "ReportsView", "StatusView", "HoneypotView"])
def test_views_refuse_helpers_and_strangers(vp, db, name):
    for s in vp.ac.GRANTABLE:
        vp.ac._helpers["map"].setdefault(HELPER, set()).add(s)
    view = getattr(vp, name)(cog(), HELPER)
    i = I(user=HELPER)
    assert run(view.interaction_check(i)) is False
    assert "no longer authorized" in i.response.send_message.call_args.args[0]


def test_home_has_buttons_for_new_sections_and_disables_them_for_helpers(vp, db):
    for s in vp.ac.GRANTABLE:
        vp.ac._helpers["map"].setdefault(HELPER, set()).add(s)
    labels = ["Watchlist", "Reports", "Status", "Honeypot"]
    owner_home = vp.main.HomeView(cog(), OWNER)
    assert all(not buttons(owner_home)[l].disabled for l in labels)
    helper_home = vp.main.HomeView(cog(), HELPER)
    assert all(buttons(helper_home)[l].disabled for l in labels)


def test_home_stays_within_discord_component_limit(vp, db):
    home = vp.main.HomeView(cog(), OWNER)
    assert len(list(home.walk_children())) <= 40


@pytest.mark.parametrize("label,cls", [("Watchlist", "WatchlistView"), ("Reports", "ReportsView"),
                                       ("Status", "StatusView"), ("Honeypot", "HoneypotView")])
def test_home_buttons_open_the_right_screen(vp, db, label, cls):
    home = vp.main.HomeView(cog(), OWNER)
    i = I()
    run(buttons(home)[label].callback(i))
    assert type(i.response.edit_message.call_args.kwargs["view"]).__name__ == cls


# ── discord limits across screens ────────────────────────────────────────

def test_every_screen_respects_row_and_option_limits(vp, db):
    db.watch["user"] = [wl_row(900000000000000000 + n, n) for n in range(10)]
    db.reports = {n: report(reason="r" * 400) for n in range(1, 30)}
    db.entries = {n: {"kind": "playing", "text": "z" * 128} for n in range(1, 21)}
    views = [vp.WatchlistView(cog(), OWNER), vp.ReportsView(cog(), OWNER),
             vp.StatusView(cog(), OWNER), vp.HoneypotView(cog(), OWNER)]
    for v in views:
        load(v)
        assert len(text_of(v)) < 4000
        for row in v.walk_children():
            if isinstance(row, discord.ui.ActionRow):
                assert len(row.children) <= 5
        for s in selects(v):
            assert len(s.options) <= 25
        assert len(list(v.walk_children())) <= 40


# ── migration / schema ───────────────────────────────────────────────────

def test_migration_is_additive_and_idempotent():
    sql = (ROOT / "database/migrations/022_admin_safety.sql").read_text()
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for s in (x for x in statements if "CREATE TABLE" in x):
        assert "IF NOT EXISTS" in s
    for s in (x for x in statements if "ALTER TABLE" in x):
        assert "ADD COLUMN IF NOT EXISTS" in s
    for bad in ("DROP ", "DELETE ", "TRUNCATE", "UPDATE "):
        assert bad not in sql.upper().replace("-- ", "")


def test_schema_version_bumped_and_migration_loaded():
    src = (ROOT / "database.py").read_text()
    import re
    # Batch 3 bumped to 39; later batches bump further, so only require "at least 39".
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)) >= 39
    assert "022_admin_safety.sql" in src
