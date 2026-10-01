"""Owner-panel Batch 2 tests: helper access, log tail, config viewer, database tools, secret masking."""
import asyncio
import importlib
import logging
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, HELPER, OTHER = 111, 333, 222
MAIN = "discord_bot.cogs._views_admin_panel"
CTRL = "discord_bot.cogs._views_admin_panel_controls"
OPSV = "discord_bot.cogs._views_admin_panel_ops"
AC = "modules.admin_controls"
OPS = "modules.admin_ops"
NOW = datetime.now(timezone.utc)
MODULES = (MAIN, CTRL, OPSV, AC, OPS)

# Fake credentials are assembled at runtime so secret scanners never see a literal.
BOT_TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4" + "." + "GabcDe" + "." + "abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    cfg.PREMIUM_GRACE_DAYS = 3
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    dbm.db.expire_old_pending_payments = AsyncMock(return_value=4)
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(OPSV)
    mod.main = main
    mod.ac = importlib.import_module(AC)
    mod.ops = importlib.import_module(OPS)
    mod.cfg = cfg
    yield mod
    for name in MODULES:
        sys.modules.pop(name, None)


def run(c):
    return asyncio.run(c)


def I(user=OWNER, values=None, guild_id=None):
    i = MagicMock()
    i.user.id = user
    i.guild_id = guild_id
    i.data = {"values": values or []}
    i.message.id = 555
    for n in ("send_message", "edit_message", "send_modal", "defer"):
        setattr(i.response, n, AsyncMock())
    i.response.is_done = MagicMock(return_value=False)
    i.followup.edit_message = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def cog():
    return MagicMock()


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


# ── fake pool ────────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self, helpers=(), fail=False, counts=None, stale=0):
        self.helpers = dict(helpers)          # user_id -> "a,b"
        self.fail, self.counts, self.stale = fail, counts or {}, stale
        self.executed = []

    async def fetch(self, sql, *a):
        if self.fail:
            raise RuntimeError("db down")
        if "admin_panel_helpers" in sql:
            import datetime as dt
            return [{"user_id": u, "sections": s, "added_by": OWNER, "created_at": NOW, "updated_at": NOW}
                    for u, s in self.helpers.items()]
        return []

    async def fetchval(self, sql, *a, **kw):
        if self.fail:
            raise RuntimeError("db down")
        if "payment_logs" in sql:
            return self.stale
        for table, n in self.counts.items():
            if f'"{table}"' in sql:
                if n is None:
                    raise RuntimeError("no such table")
                return n
        return 0

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        # behave like the real table so a refresh after a write sees the write
        if "DELETE FROM admin_panel_helpers" in sql:
            existed = a[0] in self.helpers
            self.helpers.pop(a[0], None)
            return "DELETE 1" if existed else "DELETE 0"
        if "INSERT INTO admin_panel_helpers" in sql:
            self.helpers[a[0]] = a[1]
        return "DELETE 1"


class FakePool:
    def __init__(self, conn, size=10, idle=7):
        self.conn, self._size, self._idle = conn, size, idle

    def get_size(self): return self._size
    def get_idle_size(self): return self._idle
    def get_min_size(self): return 2
    def get_max_size(self): return 20

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(s): return conn
            async def __aexit__(s, *e): return False
        return Ctx()


def patch_pool(vp, conn, monkeypatch, **kw):
    pool = FakePool(conn, **kw)
    monkeypatch.setattr(vp.ac, "_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(vp.ops, "_pool", AsyncMock(return_value=pool))
    vp.ac._helpers.update(ts=0.0, ok=False, map={})
    return pool


def grant(vp, user, *sections):
    vp.ac._helpers["map"][user] = set(sections)


# ── secret masking ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,leak", [
    ("login failed token=abc123XYZ for user", "abc123XYZ"),
    ("Authorization: Bearer " + "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r", "eyJhbGci"),
    ("connect postgresql://admin:hunter2@db.host:5432/prime failed", "hunter2"),
    ("paystack " + "sk_" + "live_abcdef1234567890" + " rejected", "abcdef1234567890"),
    (f"bot {BOT_TOKEN} ready", "GabcDe"),
    ("GROQ " + "gsk" + "_abcdefghijklmnopqrstuvwx failed", "abcdefghijklmnopqrstuvwx"),
    ("password: hunter2", "hunter2"),
    ("key blob 0123456789abcdef0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
])
def test_mask_secrets_hides_credentials(vp, raw, leak):
    out = vp.ops.mask_secrets(raw)
    assert leak not in out and "***" in out


def test_mask_secrets_leaves_ordinary_text_alone(vp):
    s = "guild 123456789012345678 joined, members 4021, path /app/modules/x.py:12"
    assert vp.ops.mask_secrets(s) == s


def test_mask_secrets_handles_none(vp):
    assert vp.ops.mask_secrets(None) == ""


# ── log buffer ───────────────────────────────────────────────────────────

def make_handler(vp):
    h = vp.ops.RingBufferHandler(capacity=5)
    lg = logging.Logger("t.ops")
    lg.addHandler(h)
    return h, lg


def test_ring_buffer_keeps_only_warning_and_above(vp):
    h, lg = make_handler(vp)
    lg.info("quiet"); lg.warning("careful"); lg.error("broken")
    assert [e.message for e in h.entries] == ["careful", "broken"]


def test_ring_buffer_is_bounded(vp):
    h, lg = make_handler(vp)
    for n in range(9):
        lg.error("e%d", n)
    assert len(h.entries) == 5 and h.entries[0].message == "e4"


def test_ring_buffer_records_exception_summary_not_traceback(vp):
    h, lg = make_handler(vp)
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("job failed")
    msg = h.entries[0].message
    assert "job failed" in msg and "ValueError: boom" in msg and "Traceback" not in msg


def test_recent_logs_newest_first_and_mode_filter(vp, monkeypatch):
    h, lg = make_handler(vp)
    monkeypatch.setattr(vp.ops, "_handler", h)
    lg.warning("w1"); lg.error("e1"); lg.warning("w2")
    assert [e.message for e in vp.ops.recent_logs(20, "warnings")] == ["w2", "e1", "w1"]
    assert [e.message for e in vp.ops.recent_logs(20, "errors")] == ["e1"]
    assert len(vp.ops.recent_logs(1, "warnings")) == 1


def test_recent_logs_empty_when_not_installed(vp, monkeypatch):
    monkeypatch.setattr(vp.ops, "_handler", None)
    assert vp.ops.recent_logs() == []


def test_install_log_buffer_is_idempotent(vp, monkeypatch):
    monkeypatch.setattr(vp.ops, "_handler", None)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        h1 = vp.ops.install_log_buffer()
        h2 = vp.ops.install_log_buffer()
        assert h1 is h2 and root.handlers.count(h1) == 1
    finally:
        root.handlers[:] = before


def test_format_log_line_masks_flattens_and_defuses_code_fences(vp):
    e = vp.ops.LogEntry(0, logging.ERROR, "pay", f"failed token={BOT_TOKEN}\n```boom```")
    line = vp.ops.format_log_line(e)
    assert "\n" not in line and "GabcDe" not in line and "```" not in line


def test_format_log_line_is_clipped(vp):
    e = vp.ops.LogEntry(0, logging.ERROR, "x", "word " * 100)
    assert len(vp.ops.format_log_line(e, width=100)) <= 100


def test_logs_as_text_is_masked_and_oldest_first(vp, monkeypatch):
    h, lg = make_handler(vp)
    monkeypatch.setattr(vp.ops, "_handler", h)
    lg.warning("first"); lg.error("second password=hunter2")
    text = vp.ops.logs_as_text("warnings")
    assert text.index("first") < text.index("second") and "hunter2" not in text


# ── config viewer ────────────────────────────────────────────────────────

def sample_config():
    m = types.ModuleType("sample")
    m.BOT_TOKEN = BOT_TOKEN
    m.PAYSTACK_SECRET_KEY = "sk_" + "live_zzzzzzzzzzzz"
    m.DATABASE_URL = "postgresql://u:p4ss@h/db"
    m.EMPTY_API_KEY = ""
    m.MAINTENANCE_NOTE = "back soon"
    m.PREMIUM_GRACE_DAYS = 3
    m.DEBUG_MODE = True
    m.PRICE = 2.5
    m.ADMIN_IDS = {1, 2, 3}
    m.SECRET_IDS = {9}
    m.SURPRISE_VALUE = "gh" + "p_" + "a" * 30        # secret-looking value under a harmless name
    m.CRED_ENDPOINT = "https://user:pw@example.com/x"
    m.lower_case = "ignored"
    m._private = "ignored"
    m.helper_fn = lambda: 1
    return m


def by_name(entries):
    return {e.name: e for e in entries}


def test_config_entries_mask_secret_names(vp):
    e = by_name(vp.ops.config_entries(sample_config()))
    for name in ("BOT_TOKEN", "PAYSTACK_SECRET_KEY", "DATABASE_URL", "SECRET_IDS"):
        assert e[name].secret and e[name].shown == "🔒 set"
    assert e["EMPTY_API_KEY"].shown == "🔒 not set"


def test_config_entries_mask_secret_looking_values_under_innocent_names(vp):
    e = by_name(vp.ops.config_entries(sample_config()))
    assert e["SURPRISE_VALUE"].secret and e["CRED_ENDPOINT"].secret


def test_config_entries_never_contain_secret_text(vp):
    blob = "\n".join(f"{e.name}={e.shown}" for e in vp.ops.config_entries(sample_config()))
    for leak in (BOT_TOKEN, "sk_live_zzzz", "p4ss", "ghp_aaaa", "user:pw"):
        assert leak not in blob


def test_config_entries_show_plain_values_and_collection_sizes(vp):
    e = by_name(vp.ops.config_entries(sample_config()))
    assert e["PREMIUM_GRACE_DAYS"].shown == "3" and e["DEBUG_MODE"].shown == "True"
    assert e["PRICE"].shown == "2.5" and e["MAINTENANCE_NOTE"].shown == "back soon"
    assert e["ADMIN_IDS"].shown == "3 items"        # sizes only, never the IDs


def test_config_entries_skip_non_settings(vp):
    names = {e.name for e in vp.ops.config_entries(sample_config())}
    assert not ({"lower_case", "_private", "helper_fn"} & names)


def test_config_entries_filter_is_case_insensitive(vp):
    names = {e.name for e in vp.ops.config_entries(sample_config(), "premium")}
    assert names == {"PREMIUM_GRACE_DAYS"}


def test_config_view_renders_page_and_paginates(vp):
    big = types.ModuleType("big")
    for n in range(30):
        setattr(big, f"SETTING_{n:02d}", n)
    v = vp.ConfigView(cog(), OWNER, module=big)
    assert "page 1/3" in text_of(v) and "SETTING_00" in text_of(v) and "SETTING_20" not in text_of(v)
    assert buttons(v)["Prev"].disabled and not buttons(v)["Next"].disabled
    run(v._next(I()))
    assert "page 2/3" in text_of(v) and "SETTING_12" in text_of(v)
    run(v._next(I())); run(v._next(I()))
    assert buttons(v)["Next"].disabled


def test_config_view_never_renders_secrets(vp):
    v = vp.ConfigView(cog(), OWNER, module=sample_config())
    rendered = text_of(v)
    assert "BOT_TOKEN" in rendered and "GabcDe" not in rendered and "sk_live" not in rendered


def test_config_view_filter_modal_and_clear(vp):
    v = vp.ConfigView(cog(), OWNER, module=sample_config())
    m = vp.ConfigFilterModal(v)
    m.query = MagicMock(value="grace")
    i = I()
    run(m.on_submit(i))
    assert v.query == "grace" and "PREMIUM_GRACE_DAYS" in text_of(v) and "DEBUG_MODE" not in text_of(v)
    assert not buttons(v)["Clear filter"].disabled
    run(v._clear(I()))
    assert v.query == "" and "DEBUG_MODE" in text_of(v)


def test_config_filter_modal_refuses_non_owner(vp):
    v = vp.ConfigView(cog(), OWNER, module=sample_config())
    m = vp.ConfigFilterModal(v)
    m.query = MagicMock(value="x")
    i = I(user=OTHER)
    run(m.on_submit(i))
    assert v.query == "" and "no longer authorized" in i.response.send_message.call_args[0][0]


# ── helper data layer ────────────────────────────────────────────────────

def test_parse_sections_drops_unknown_and_access(vp):
    assert vp.ac._parse_sections("audit, access ,bogus,logs") == {"audit", "logs"}
    assert vp.ac._parse_sections(None) == set()


def test_grantable_never_includes_owner_only_sections(vp):
    assert not ({"access", "config", "database", "controls", "payments", "broadcast"} & set(vp.ac.GRANTABLE))


def test_set_helper_stores_and_writes_through(vp, monkeypatch):
    conn = FakeConn()
    patch_pool(vp, conn, monkeypatch)
    stored = run(vp.ac.set_helper(HELPER, {"audit", "access", "nope"}, OWNER))
    assert stored == {"audit"} and vp.ac.helper_sections(HELPER) == {"audit"}
    assert conn.executed[0][1][1] == "audit"


def test_set_helper_with_nothing_valid_removes(vp, monkeypatch):
    conn = FakeConn()
    patch_pool(vp, conn, monkeypatch)
    grant(vp, HELPER, "audit")
    assert run(vp.ac.set_helper(HELPER, {"access"}, OWNER)) == set()
    assert vp.ac.helper_sections(HELPER) == set() and "DELETE" in conn.executed[-1][0]


def test_remove_helper_revokes_immediately(vp, monkeypatch):
    patch_pool(vp, FakeConn(helpers={HELPER: "audit"}), monkeypatch)
    grant(vp, HELPER, "audit")
    assert run(vp.ac.remove_helper(HELPER)) is True
    assert vp.ac.helper_sections(HELPER) == set()


def test_remove_helper_reports_false_when_nobody_was_removed(vp, monkeypatch):
    patch_pool(vp, FakeConn(), monkeypatch)
    assert run(vp.ac.remove_helper(HELPER)) is False


def test_refresh_loads_helpers_from_db(vp, monkeypatch):
    patch_pool(vp, FakeConn(helpers={HELPER: "audit,logs,access"}), monkeypatch)
    run(vp.ac.refresh_helpers(force=True))
    assert vp.ac.helper_sections(HELPER) == {"audit", "logs"}      # tampered 'access' ignored


def test_refresh_fails_closed_with_no_snapshot(vp, monkeypatch):
    patch_pool(vp, FakeConn(fail=True), monkeypatch)
    run(vp.ac.refresh_helpers(force=True))
    assert vp.ac.helper_sections(HELPER) == set()


def test_refresh_keeps_last_good_map_on_failure(vp, monkeypatch):
    conn = FakeConn(helpers={HELPER: "audit"})
    patch_pool(vp, conn, monkeypatch)
    run(vp.ac.refresh_helpers(force=True))
    conn.fail = True
    run(vp.ac.refresh_helpers(force=True))
    assert vp.ac.helper_sections(HELPER) == {"audit"}


def test_refresh_never_raises_when_pool_is_unavailable(vp, monkeypatch):
    monkeypatch.setattr(vp.ac, "_pool", AsyncMock(side_effect=RuntimeError("no pool")))
    run(vp.ac.refresh_helpers(force=True))   # must not raise


# ── allowed_sections / authorization ─────────────────────────────────────

def test_owner_gets_new_owner_only_sections(vp):
    a = vp.main.allowed_sections(OWNER)
    assert {"access", "logs", "config", "database"} <= a


def test_stranger_gets_nothing(vp):
    assert vp.main.allowed_sections(OTHER) == set()
    assert not vp.main.can_open_panel(OTHER)


def test_helper_gets_only_granted_sections(vp):
    grant(vp, HELPER, "audit", "logs")
    a = vp.main.allowed_sections(HELPER)
    assert a == {"audit", "logs"} and vp.main.can_open_panel(HELPER)


def test_helper_can_never_reach_owner_only_sections(vp):
    grant(vp, HELPER, *vp.ac.GRANTABLE)
    a = vp.main.allowed_sections(HELPER)
    for s in ("access", "config", "database", "controls", "payments", "broadcast", "servers", "system", "feedback"):
        assert s not in a


def test_interaction_check_blocks_helper_from_ungranted_section(vp, monkeypatch):
    patch_pool(vp, FakeConn(helpers={HELPER: "audit"}), monkeypatch)
    v = vp.ConfigView(cog(), HELPER, module=sample_config())
    i = I(user=HELPER)
    assert run(v.interaction_check(i)) is False
    assert "no longer authorized" in i.response.send_message.call_args[0][0]


def test_interaction_check_allows_helper_for_granted_section(vp, monkeypatch):
    patch_pool(vp, FakeConn(helpers={HELPER: "logs"}), monkeypatch)
    assert run(vp.LogsView(cog(), HELPER).interaction_check(I(user=HELPER))) is True


def test_revoked_helper_is_locked_out_on_next_click(vp, monkeypatch):
    patch_pool(vp, FakeConn(helpers={HELPER: "logs"}), monkeypatch)
    v = vp.LogsView(cog(), HELPER)
    assert run(v.interaction_check(I(user=HELPER))) is True     # loads the grant from the DB
    run(vp.ac.remove_helper(HELPER))
    vp.ac.invalidate_helpers()                                   # force a real reload on the next click
    assert run(v.interaction_check(I(user=HELPER))) is False


def test_home_enables_buttons_per_role(vp):
    owner_btns = buttons(vp.main.HomeView(cog(), OWNER))
    for label in ("Panel access", "Log tail", "Config", "Database"):
        assert not owner_btns[label].disabled
    grant(vp, HELPER, "logs")
    h = buttons(vp.main.HomeView(cog(), HELPER))
    assert not h["Log tail"].disabled
    for label in ("Panel access", "Config", "Database", "Payments", "Kill switches"):
        assert h[label].disabled


def test_home_stays_within_discord_limits(vp):
    v = vp.main.HomeView(cog(), OWNER)
    rows = [c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)]
    assert all(len(r.children) <= 5 for r in rows)


# ── access screens ───────────────────────────────────────────────────────

def helper_row(uid=HELPER, sections=("audit",)):
    return {"user_id": uid, "sections": set(sections), "added_by": OWNER, "created_at": NOW, "updated_at": NOW}


def test_access_view_lists_owners_and_helpers(vp):
    v = vp.AccessView(cog(), OWNER)
    v.helpers = [helper_row(sections=("audit", "logs"))]
    v._build()
    t = text_of(v)
    assert f"<@{OWNER}>" in t and f"<@{HELPER}>" in t and "Audit log, Log tail" in t
    assert len(selects(v)) == 1


def test_access_view_empty_state(vp):
    assert "No helpers yet" in text_of(vp.AccessView(cog(), OWNER))


def test_access_load_error_is_reported(vp, monkeypatch):
    monkeypatch.setattr(vp.ac, "list_helpers", AsyncMock(side_effect=RuntimeError("x")))
    v = vp.AccessView(cog(), OWNER)
    run(v.load())
    assert "Couldn't load helpers" in text_of(v)


def test_access_view_is_owner_only(vp):
    grant(vp, HELPER, *vp.ac.GRANTABLE)
    assert run(vp.AccessView(cog(), HELPER).interaction_check(I(user=HELPER))) is False


def test_add_helper_modal_rejects_bad_id(vp):
    m = vp.AddHelperModal(cog())
    m.target = MagicMock(value="12345")
    i = I()
    run(m.on_submit(i))
    assert "doesn't look like a Discord ID" in i.response.send_message.call_args[0][0]
    i.response.edit_message.assert_not_awaited()


def test_add_helper_modal_rejects_owner(vp):
    m = vp.AddHelperModal(cog())
    m.target = MagicMock(value=str(OWNER).ljust(12, "0") if False else str(OWNER))
    # OWNER (111) is too short to be a snowflake, so use a config owner with a real-looking ID
    vp.main.DISCORD_CLONE_ADMIN_IDS.add(123456789012345678)
    m.target = MagicMock(value="123456789012345678")
    i = I()
    run(m.on_submit(i))
    assert "already an owner" in i.response.send_message.call_args[0][0]


def test_add_helper_modal_refuses_non_owner(vp):
    grant(vp, HELPER, "audit")
    m = vp.AddHelperModal(cog())
    m.target = MagicMock(value="123456789012345678")
    i = I(user=HELPER)
    run(m.on_submit(i))
    assert "no longer authorized" in i.response.send_message.call_args[0][0]
    i.response.edit_message.assert_not_awaited()


def test_add_helper_modal_opens_editor_for_valid_id(vp):
    m = vp.AddHelperModal(cog())
    m.target = MagicMock(value="123456789012345678")
    i = I()
    run(m.on_submit(i))
    view = i.response.edit_message.call_args.kwargs["view"]
    assert isinstance(view, vp.HelperEditView) and view.target_id == 123456789012345678 and not view.existing


def test_editor_save_disabled_until_a_section_is_chosen(vp):
    v = vp.HelperEditView(cog(), OWNER, HELPER, set(), existing=False)
    assert buttons(v)["Save"].disabled
    run(v._pick(I(values=["audit"])))
    assert not buttons(v)["Save"].disabled and "Audit log" in text_of(v)


def test_editor_ignores_non_grantable_values(vp):
    v = vp.HelperEditView(cog(), OWNER, HELPER, set(), existing=False)
    run(v._pick(I(values=["audit", "access", "payments"])))
    assert v.chosen == {"audit"}


def test_editor_save_persists_audits_and_returns(vp, monkeypatch):
    patch_pool(vp, FakeConn(), monkeypatch)
    logged = []
    monkeypatch.setattr(vp, "audit", lambda i, action, **kw: logged.append((action, kw)))
    v = vp.HelperEditView(cog(), OWNER, HELPER, {"audit", "logs"}, existing=False)
    i = I()
    run(v._save(i))
    assert vp.ac.helper_sections(HELPER) == {"audit", "logs"}
    assert logged == [("access.helper_set", {"target": HELPER, "sections": "audit,logs"})]
    assert isinstance(i.response.edit_message.call_args.kwargs["view"], vp.AccessView)


def test_editor_save_db_failure_changes_nothing(vp, monkeypatch):
    monkeypatch.setattr(vp.ac, "set_helper", AsyncMock(side_effect=RuntimeError("db")))
    v = vp.HelperEditView(cog(), OWNER, HELPER, {"audit"}, existing=False)
    i = I()
    run(v._save(i))
    assert "Nothing was changed" in i.response.send_message.call_args[0][0]
    i.response.edit_message.assert_not_awaited()


def test_editor_remove_is_two_step(vp, monkeypatch):
    patch_pool(vp, FakeConn(), monkeypatch)
    grant(vp, HELPER, "audit")
    v = vp.HelperEditView(cog(), OWNER, HELPER, {"audit"}, existing=True)
    run(v._remove(I()))
    assert v._confirm and vp.ac.helper_sections(HELPER) == {"audit"}      # first click only arms it
    assert "Confirm remove" in buttons(v)
    run(v._remove(I()))
    assert vp.ac.helper_sections(HELPER) == set()


def test_editor_changing_selection_disarms_remove(vp):
    v = vp.HelperEditView(cog(), OWNER, HELPER, {"audit"}, existing=True)
    v._confirm = True
    run(v._pick(I(values=["logs"])))
    assert v._confirm is False


def test_new_helper_has_no_remove_button(vp):
    assert "Remove helper" not in buttons(vp.HelperEditView(cog(), OWNER, HELPER, set(), existing=False))


# ── log screen ───────────────────────────────────────────────────────────

def logs_with(vp, monkeypatch, *records):
    h, lg = make_handler(vp)
    h.entries = type(h.entries)(maxlen=200)
    monkeypatch.setattr(vp.ops, "_handler", h)
    for level, msg in records:
        lg.log(level, msg)
    return h


def test_logs_view_empty_state(vp, monkeypatch):
    monkeypatch.setattr(vp.ops, "_handler", None)
    assert "No errors logged" in text_of(vp.LogsView(cog(), OWNER))


def test_logs_view_shows_masked_lines_in_a_code_block(vp, monkeypatch):
    logs_with(vp, monkeypatch, (logging.ERROR, f"webhook failed token={BOT_TOKEN}"), (logging.WARNING, "slow"))
    v = vp.LogsView(cog(), OWNER)
    t = text_of(v)
    assert "```" in t and "webhook failed" in t and "GabcDe" not in t and "slow" not in t   # errors only


def test_logs_view_toggle_includes_warnings(vp, monkeypatch):
    logs_with(vp, monkeypatch, (logging.WARNING, "slow query"))
    v = vp.LogsView(cog(), OWNER)
    run(v._toggle(I()))
    assert v.mode == "warnings" and "slow query" in text_of(v) and "Errors only" in buttons(v)


def test_logs_view_caps_at_twenty_and_fits_budget(vp, monkeypatch):
    logs_with(vp, monkeypatch, *[(logging.ERROR, f"error number {n} " + "x" * 120) for n in range(60)])
    v = vp.LogsView(cog(), OWNER)
    assert len(text_of(v)) < 4000
    assert text_of(v).count("error number") <= 20


def test_logs_send_as_file_is_masked_and_audited(vp, monkeypatch):
    logs_with(vp, monkeypatch, (logging.ERROR, "bad password=hunter2"))
    logged = []
    monkeypatch.setattr(vp, "audit", lambda i, action, **kw: logged.append(action))
    i = I()
    run(vp.LogsView(cog(), OWNER)._file(i))
    kwargs = i.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True and kwargs["file"].filename == "prime-bot-logs.txt"
    assert b"hunter2" not in kwargs["file"].fp.read() and logged == ["logs.download"]


# ── database screen ──────────────────────────────────────────────────────

def test_pool_status_reports_busy_connections(vp):
    s = vp.ops.pool_status(FakePool(FakeConn(), size=10, idle=7))
    assert s == {"size": 10, "idle": 7, "busy": 3, "min": 2, "max": 20}


def test_pool_status_tolerates_missing_getters(vp):
    s = vp.ops.pool_status(object())
    assert s["busy"] is None and s["size"] is None


def test_table_counts_uses_n_a_for_missing_tables(vp):
    conn = FakeConn(counts={"users": 42, "bot_blacklist": None})
    counts = dict(run(vp.ops.table_counts(conn)))
    assert counts["users"] == 42 and counts["bot_blacklist"] is None


def test_table_names_are_a_safe_whitelist(vp):
    assert all(vp.ops._TABLE_OK.match(t) for t in vp.ops.COUNT_TABLES)


def test_database_view_renders_overview(vp, monkeypatch):
    patch_pool(vp, FakeConn(counts={"users": 1234}, stale=3), monkeypatch)
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    t = text_of(v)
    assert "3 busy" in t and "`users` 1,234" in t and "3 pending for more than 72h" in t
    assert "(3)" in "".join(buttons(v))


def test_database_view_error_state_disables_cleanup(vp, monkeypatch):
    patch_pool(vp, FakeConn(fail=True), monkeypatch)
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    assert "Couldn't read the database" in text_of(v)
    assert all(b.disabled for label, b in buttons(v).items() if label.startswith("Clean up"))


def test_cleanup_disabled_when_nothing_is_stale(vp, monkeypatch):
    patch_pool(vp, FakeConn(stale=0), monkeypatch)
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    assert buttons(v)["Clean up stale payments (0)"].disabled


def test_cleanup_is_two_step_and_calls_existing_sweep(vp, monkeypatch):
    patch_pool(vp, FakeConn(stale=4), monkeypatch)
    logged = []
    monkeypatch.setattr(vp, "audit", lambda i, action, **kw: logged.append((action, kw)))
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    sweep = sys.modules["database"].db.expire_old_pending_payments
    run(v._cleanup(I()))
    sweep.assert_not_awaited()                       # first press only asks to confirm
    assert "Press Confirm" in text_of(v)
    i2 = I(); i2.edit_original_response = AsyncMock()
    run(v._cleanup(i2))
    i2.response.defer.assert_awaited_once()          # acknowledged before the slow work
    sweep.assert_awaited_once_with(72)
    i2.edit_original_response.assert_awaited_once()
    assert logged == [("database.cleanup_stale_payments", {"expired": 4, "older_than_hours": 72})]
    assert "Marked 4 stale" in text_of(v)


def test_cleanup_failure_reports_and_does_not_audit(vp, monkeypatch):
    patch_pool(vp, FakeConn(stale=4), monkeypatch)
    sys.modules["database"].db.expire_old_pending_payments = AsyncMock(side_effect=RuntimeError("db"))
    logged = []
    monkeypatch.setattr(vp, "audit", lambda i, action, **kw: logged.append(action))
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    run(v._cleanup(I())); i = I()
    run(v._cleanup(i))
    assert "Nothing was changed" in i.followup.send.call_args[0][0] and logged == []


def test_database_view_is_owner_only(vp):
    grant(vp, HELPER, *vp.ac.GRANTABLE)
    assert run(vp.DatabaseView(cog(), HELPER).interaction_check(I(user=HELPER))) is False


# ── slow database: the button must answer Discord first, and never hang ──

class SlowConn(FakeConn):
    """Exact COUNT(*) on the named tables times out, like a huge table on a remote DB."""
    def __init__(self, slow=(), estimates=None, **kw):
        super().__init__(**kw)
        self.slow, self.estimates = set(slow), estimates or {}

    async def fetchval(self, sql, *a, **kw):
        if "pg_class" in sql:
            return self.estimates.get(a[0], -1)
        for t in self.slow:
            if f'"{t}"' in sql:
                raise asyncio.TimeoutError()
        return await super().fetchval(sql, *a, **kw)


def test_slow_count_falls_back_to_estimate_and_is_marked_approximate(vp):
    conn = SlowConn(slow={"users"}, estimates={"users": 987654}, counts={"discord_guilds": 7})
    approx = set()
    counts = dict(run(vp.ops.table_counts(conn, approx)))
    assert counts["users"] == 987654 and approx == {"users"}
    assert counts["discord_guilds"] == 7


def test_slow_count_without_stats_is_n_a_not_zero(vp):
    conn = SlowConn(slow={"users"}, estimates={})            # reltuples -1 = never analysed
    approx = set()
    assert dict(run(vp.ops.table_counts(conn, approx)))["users"] is None and approx == set()


def test_missing_table_is_still_n_a_not_an_estimate(vp):
    conn = SlowConn(estimates={"bot_blacklist": 5}, counts={"bot_blacklist": None})
    approx = set()
    assert dict(run(vp.ops.table_counts(conn, approx)))["bot_blacklist"] is None
    assert approx == set()


def test_view_marks_estimates(vp, monkeypatch):
    patch_pool(vp, SlowConn(slow={"users"}, estimates={"users": 1500000}, stale=0), monkeypatch)
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    t = text_of(v)
    assert "`users` ~1,500,000" in t and "estimate" in t


def test_home_database_button_defers_before_loading(vp, monkeypatch):
    order = []
    patch_pool(vp, FakeConn(counts={"users": 5}, stale=0), monkeypatch)
    home = vp.main.HomeView(cog(), OWNER)
    i = I()
    i.response.defer = AsyncMock(side_effect=lambda *a, **k: order.append("defer"))
    i.edit_original_response = AsyncMock(side_effect=lambda **k: order.append("edit"))
    orig = vp.ops.db_overview
    async def spy():
        order.append("load")
        return await orig()
    monkeypatch.setattr(vp.ops, "db_overview", spy)
    run(home._database(i))
    assert order == ["defer", "load", "edit"]
    i.response.edit_message.assert_not_awaited()


def test_refresh_defers_before_loading(vp, monkeypatch):
    patch_pool(vp, FakeConn(counts={"users": 5}, stale=0), monkeypatch)
    v = vp.DatabaseView(cog(), OWNER)
    run(v.load())
    i = I(); i.edit_original_response = AsyncMock()
    run(v._refresh(i))
    i.response.defer.assert_awaited_once()
    i.edit_original_response.assert_awaited_once()
