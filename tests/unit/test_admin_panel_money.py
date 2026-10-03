"""Owner-panel Batch 4 tests: failed payments, revenue trend, reverse payment,
discount codes, upcoming expiries, webhook failure recording."""
import asyncio
import importlib
import io
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, HELPER, OTHER = 111, 333, 222
MAIN = "discord_bot.cogs._views_admin_panel"
CTRL = "discord_bot.cogs._views_admin_panel_controls"
MONV = "discord_bot.cogs._views_admin_panel_money"
AC = "modules.admin_controls"
MON = "modules.admin_money"
NOW = datetime.now(timezone.utc)
MODULES = (MAIN, CTRL, MONV, AC, MON)
ROOT = pathlib.Path(__file__).resolve().parents[2]
GID = 555000000000000001


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    cfg.PREMIUM_DAYS = 30
    cfg.PREMIUM_GRACE_DAYS = 3
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    for name in MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(MONV)
    mod.main = main
    mod.ac = importlib.import_module(AC)
    mod.money = importlib.import_module(MON)
    mod.money._noisy_last.clear()
    mod.audit = MagicMock()
    mod.cfg = cfg
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


def cog():
    c = MagicMock()
    c.bot.get_guild.return_value = None
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


def load(view):
    run(view.load())
    return view


# ── fake database that behaves like the real tables ──────────────────────

class _Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *e): return False


class FakeConn:
    def __init__(self):
        self.fail = False
        self.failures = {}       # id -> dict
        self.payments = {}       # payment_id -> dict
        self.subs = {}           # (guild_id, clone_id) -> dict(expires_at)
        self.coupons = {}        # code -> dict
        self.revenue_rows = []
        self.pending_rows = []
        self.expiry_rows = []
        self._next = 1
        self.sleep = 0.0

    def _check(self):
        if self.fail:
            raise RuntimeError("db down")

    def transaction(self):
        return _Tx()

    async def execute(self, sql, *a):
        self._check()
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if sql.startswith("INSERT INTO payment_failures"):
            self.failures[self._next] = dict(id=self._next, source=a[0], kind=a[1], reference=a[2], detail=a[3],
                                             created_at=NOW, dismissed_at=None)
            self._next += 1
            return "INSERT 0 1"
        if sql.startswith("UPDATE payment_failures"):
            f = self.failures.get(a[0])
            if f and f["dismissed_at"] is None:
                f["dismissed_at"] = NOW
                return "UPDATE 1"
            return "UPDATE 0"
        if sql.startswith("INSERT INTO discount_codes"):
            if a[0] in self.coupons:
                return "INSERT 0 0"
            self.coupons[a[0]] = dict(code=a[0], percent_off=a[1], max_uses=a[2], uses=0, expires_at=a[3], active=True)
            return "INSERT 0 1"
        if sql.startswith("UPDATE discount_codes"):
            c = self.coupons.get(a[0])
            if c:
                c["active"] = a[1]
                return "UPDATE 1"
            return "UPDATE 0"
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        self._check()
        if "FROM payment_failures" in sql:
            rows = [f for f in self.failures.values() if f["dismissed_at"] is None]
            rows.sort(key=lambda r: -r["id"])
            return rows[: a[0]]
        if "FROM payment_logs" in sql and "status = 'pending'" in sql:
            return list(self.pending_rows)[: a[0]]
        if "FROM payment_logs" in sql:
            return list(self.revenue_rows)
        if "FROM discount_codes" in sql:
            return list(self.coupons.values())[: a[0]]
        if "FROM discord_guild_subscriptions" in sql:
            return list(self.expiry_rows)[: a[1]]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        self._check()
        if sql.startswith("SELECT * FROM payment_logs"):
            for p in self.payments.values():
                if p["paystack_reference"] == a[0]:
                    return dict(p)
            return None
        if sql.startswith("UPDATE payment_logs SET status = 'reversed'"):
            p = self.payments.get(a[0])
            if p and p["status"] == "completed" and p["payment_type"] in a[2]:
                p["status"] = "reversed"
                p["reversed_by"] = a[1]
                return dict(p)
            return None
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        self._check()
        if sql.startswith("SELECT COUNT(*) FROM payment_logs"):
            return len(self.pending_rows)
        if sql.startswith("UPDATE discord_guild_subscriptions"):
            s = self.subs.get((a[0], a[1]))
            if s:
                s["expires_at"] = s["expires_at"] - timedelta(days=a[2])
                return s["expires_at"]
            return None
        raise AssertionError(sql)


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
    monkeypatch.setattr(vp.money, "_pool", AsyncMock(return_value=FakePool(conn)))
    vp.ac._helpers.update(ts=0.0, ok=False, map={})
    return conn


def payment(pid=1, ref="gum_premium_1_abc", ptype="premium", status="completed", chat=GID, clone=None, amount=5.0):
    return dict(payment_id=pid, paystack_reference=ref, payment_type=ptype, status=status, chat_id=chat,
                clone_id=clone, user_id=42, amount=amount, provider="gumroad")


# ── failure recording ────────────────────────────────────────────────────

def test_record_failure_writes_a_masked_row(vp, db):
    fake_key = "sk_" + "live_abc123456"                       # built at runtime: no token-shaped literal in the repo
    assert run(vp.money.record_failure("paystack", "handler_error", "ref_1", f"boom key={fake_key} Bearer abc.def")) is True
    row = db.failures[1]
    assert fake_key not in row["detail"] and "Bearer abc" not in row["detail"]
    assert row["source"] == "paystack" and row["reference"] == "ref_1"


def test_record_failure_never_raises_when_db_is_down(vp, db):
    db.fail = True
    assert run(vp.money.record_failure("paystack", "handler_error", "r", "x")) is False
    assert vp.money.record_failure_sync("gumroad", "underpaid") is False


def test_record_failure_gives_up_on_a_slow_database(vp, db, monkeypatch):
    monkeypatch.setattr(vp.money, "RECORD_TIMEOUT", 0.05)
    db.sleep = 1.0
    assert run(vp.money.record_failure("paystack", "handler_error")) is False
    assert db.failures == {}


def test_record_failure_never_raises_when_pool_cannot_be_built(vp, monkeypatch):
    monkeypatch.setattr(vp.money, "_pool", AsyncMock(side_effect=RuntimeError("no pool")))
    assert run(vp.money.record_failure("paystack", "handler_error")) is False


def test_noisy_rejections_are_throttled_but_real_problems_are_not(vp, db):
    for _ in range(5):
        run(vp.money.record_failure("gumroad", "bad_secret"))
        run(vp.money.record_failure("gumroad", "underpaid", "r"))
    kinds = [f["kind"] for f in db.failures.values()]
    assert kinds.count("bad_secret") == 1 and kinds.count("underpaid") == 5


# ── pending payments screen ──────────────────────────────────────────────

def pending(pid, ref, **kw):
    return dict(payment_id=pid, paystack_reference=ref, user_id=42, amount=5.0, payment_type="premium",
                provider="selar", chat_id=GID, created_date=NOW, **kw)


def test_pending_lists_references_and_changes_nothing_until_confirmed(vp, db):
    db.pending_rows = [pending(1, "ref_pending_a"), pending(2, "ref_pending_b")]
    v = load(vp.PendingView(cog(), OWNER))
    assert "ref_pending_a" in text_of(v) and "ref_pending_b" in text_of(v)
    assert {"Refresh", "Back"} <= set(buttons(v)) and any(k.startswith("Clear old") for k in buttons(v))
    vp.audit.assert_not_called()


def test_pending_clear_is_two_step_and_audited(vp, db, monkeypatch):
    from unittest.mock import AsyncMock
    db.pending_rows = [pending(1, "ref_pending_a"), pending(2, "ref_pending_b")]
    clear = AsyncMock(return_value=2)
    monkeypatch.setattr(vp.money, "clear_old_pending", clear)
    v = load(vp.PendingView(cog(), OWNER))
    label = next(k for k in buttons(v) if k.startswith("Clear old"))
    press(v, label)
    clear.assert_not_awaited()                       # first press only arms it
    assert "Confirm clear" in buttons(v) and "Press Confirm clear" in text_of(v)
    press(v, "Confirm clear")
    clear.assert_awaited_once()
    assert vp.audit.call_args.args[1] == "money.pending.clear" and "Cleared 2" in text_of(v)


def test_pending_empty_and_error_states(vp, db):
    assert "Nothing pending" in text_of(load(vp.PendingView(cog(), OWNER)))
    db.fail = True
    assert "Couldn't load" in text_of(load(vp.PendingView(cog(), OWNER)))


def test_pending_hub_button_opens_screen_and_fits_limits(vp, db):
    db.pending_rows = [pending(n, f"ref_{n}_" + "x" * 40) for n in range(60)]
    hub = vp.MoneyHubView(cog(), OWNER)
    press(hub, "Pending payments")
    v = load(vp.PendingView(cog(), OWNER))
    assert len(text_of(v)) < 4000


# ── failed payments screen ───────────────────────────────────────────────

def test_failures_list_and_dismiss_refreshes(vp, db):
    run(vp.money.record_failure("paystack", "no_matching_row", "ref_a", "listing_boost: paid but no pending row"))
    run(vp.money.record_failure("gumroad", "underpaid", "ref_b", "sale 1"))
    v = load(vp.FailuresView(cog(), OWNER))
    assert "ref_a" in text_of(v) and "ref_b" in text_of(v)
    pick(v, 0, 1)
    press(v, "Dismiss")
    assert db.failures[1]["dismissed_at"] is not None
    assert "ref_a" not in text_of(v) and "ref_b" in text_of(v)
    assert vp.audit.call_args.args[1] == "money.failure.dismiss"


def test_failures_dismiss_db_error_changes_nothing(vp, db):
    run(vp.money.record_failure("gumroad", "underpaid", "ref_b"))
    v = load(vp.FailuresView(cog(), OWNER))
    pick(v, 0, 1)
    db.fail = True
    press(v, "Dismiss")
    db.fail = False
    assert db.failures[1]["dismissed_at"] is None
    assert "Nothing was changed" in text_of(v)
    vp.audit.assert_not_called()


def test_failures_load_error_is_shown_not_raised(vp, db):
    db.fail = True
    v = load(vp.FailuresView(cog(), OWNER))
    assert "Couldn't load" in text_of(v)


def test_failures_fit_discord_limits(vp, db):
    for n in range(40):
        run(vp.money.record_failure("paystack", "no_matching_row", f"ref_{n}", "d" * 480))
    v = load(vp.FailuresView(cog(), OWNER))
    assert len(selects(v)[0].options) <= 25
    assert len(text_of(v)) < 4000


# ── revenue trend ────────────────────────────────────────────────────────

def test_sparkline_basics(vp):
    s = vp.money.sparkline
    assert s([]) == "" and s([0, 0]) == "▁▁" and s([0, 5, 10]) == "▁▅█"


def test_revenue_series_fills_gaps_and_splits_currencies(vp, db):
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    db.revenue_rows = [
        {"bucket": today, "currency": "GHS", "total": 30.0, "n": 2},
        {"bucket": today - timedelta(days=2), "currency": "USD", "total": 5.0, "n": 1},
    ]
    out = run(vp.money.revenue_series("day", 14, now=NOW))
    assert len(out["GHS"]) == 14 and len(out["USD"]) == 14
    assert out["GHS"][-1]["total"] == 30.0 and out["GHS"][-2]["total"] == 0.0
    assert out["USD"][-3]["total"] == 5.0 and sum(p["total"] for p in out["USD"]) == 5.0


def test_revenue_view_toggles_and_survives_db_error(vp, db):
    v = load(vp.RevenueView(cog(), OWNER))
    assert "daily" in text_of(v) and "no completed payments" in text_of(v)
    press(v, "Weekly")
    assert "weekly" in text_of(v)
    db.fail = True
    press(v, "Refresh")
    assert "Couldn't load" in text_of(v)


def test_revenue_view_shows_sparkline_when_there_is_data(vp, db):
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    db.revenue_rows = [{"bucket": today, "currency": "GHS", "total": 30.0, "n": 2}]
    v = load(vp.RevenueView(cog(), OWNER))
    assert "30 from 2 payment" in text_of(v) and "█" in text_of(v)


# ── reverse a payment ────────────────────────────────────────────────────

def lookup(vp, ref):
    modal = vp.ReferenceModal(cog())
    modal.ref._value = ref
    i = I()
    run(modal.on_submit(i))
    return i.response.edit_message.call_args.kwargs["view"]


def test_reverse_is_two_step_and_idempotent(vp, db):
    db.payments = {1: payment()}
    db.subs = {(GID, None): {"expires_at": NOW + timedelta(days=45)}}
    v = lookup(vp, "gum_premium_1_abc")
    assert "can be reversed" in text_of(v)
    press(v, "Reverse")
    assert db.payments[1]["status"] == "completed" and "Confirm reversal" in buttons(v)   # first press only arms
    press(v, "Confirm reversal")
    assert db.payments[1]["status"] == "reversed"
    assert db.subs[(GID, None)]["expires_at"] == pytest.approx(NOW + timedelta(days=15), abs=timedelta(seconds=1))
    assert vp.audit.call_args.args[1] == "money.reverse"
    # second reversal is a no-op: no more days removed, no second audit line
    assert run(vp.money.reverse_payment(1, OWNER))["changed"] is False
    assert db.subs[(GID, None)]["expires_at"] == pytest.approx(NOW + timedelta(days=15), abs=timedelta(seconds=1))
    assert vp.audit.call_count == 1


def test_reverse_refuses_other_types_unknown_and_already_reversed(vp, db):
    db.payments = {1: payment(ptype="xp_boost"), 2: payment(2, "r2", status="reversed"),
                   3: payment(3, "r3", status="pending")}
    for ref, words in (("nope", "No payment found"), ("gum_premium_9", "No payment found"),
                       ("r2", "already reversed"), ("r3", "Only completed")):
        v = lookup(vp, ref)
        assert words in text_of(v) and buttons(v)["Reverse"].disabled
    db.payments[1]["paystack_reference"] = "r1"
    v = lookup(vp, "r1")
    assert "no exact, safe inverse" in text_of(v) and buttons(v)["Reverse"].disabled


def test_reverse_db_failure_leaves_everything_untouched(vp, db):
    db.payments = {1: payment()}
    db.subs = {(GID, None): {"expires_at": NOW + timedelta(days=45)}}
    v = lookup(vp, "gum_premium_1_abc")
    press(v, "Reverse")
    db.fail = True
    press(v, "Confirm reversal")
    db.fail = False
    assert db.payments[1]["status"] == "completed"
    assert db.subs[(GID, None)]["expires_at"] > NOW + timedelta(days=44)
    assert "Nothing was changed" in text_of(v)
    vp.audit.assert_not_called()


def test_reverse_without_a_server_only_marks_the_row(vp, db):
    db.payments = {1: payment(chat=None)}
    res = run(vp.money.reverse_payment(1, OWNER))
    assert res["ok"] and res["expires_at"] is None and db.payments[1]["status"] == "reversed"


def test_reverse_modal_rechecks_authorization(vp, db):
    db.payments = {1: payment()}
    modal = vp.ReferenceModal(cog())
    modal.ref._value = "gum_premium_1_abc"
    i = I(user=OTHER)
    run(modal.on_submit(i))
    i.response.send_message.assert_awaited_once()
    i.response.edit_message.assert_not_awaited()


# ── discount codes ───────────────────────────────────────────────────────

def submit_coupon(vp, code="SAVE10", pct="10", uses="", days="", user=OWNER):
    modal = vp.CouponModal(cog())
    modal.code._value, modal.percent._value, modal.uses._value, modal.days._value = code, pct, uses, days
    i = I(user=user)
    run(modal.on_submit(i))
    return i


def test_coupon_create_list_and_toggle(vp, db):
    i = submit_coupon(vp, "save-10", "10", "50", "30")
    view = i.response.edit_message.call_args.kwargs["view"]
    assert db.coupons["SAVE-10"]["percent_off"] == 10 and db.coupons["SAVE-10"]["max_uses"] == 50
    assert "Not live yet" in text_of(view) and "SAVE-10" in text_of(view)
    pick(view, 0, "SAVE-10")
    press(view, "Disable")
    assert db.coupons["SAVE-10"]["active"] is False and "disabled" in text_of(view)
    press(view, "Enable")
    assert db.coupons["SAVE-10"]["active"] is True
    assert [c.args[1] for c in vp.audit.call_args_list] == ["money.coupon.create", "money.coupon.disable",
                                                             "money.coupon.enable"]


def test_coupon_validation_and_duplicates(vp, db):
    for kw in (dict(code="a b"), dict(code="ab"), dict(pct="0"), dict(pct="101"), dict(pct="x"),
               dict(uses="0"), dict(days="-1")):
        i = submit_coupon(vp, **kw)
        i.response.send_message.assert_awaited_once()
    assert db.coupons == {}
    submit_coupon(vp)
    i = submit_coupon(vp)
    assert "already exists" in text_of(i.response.edit_message.call_args.kwargs["view"])
    assert len(db.coupons) == 1


def test_coupon_modal_rechecks_authorization_and_db_failure(vp, db):
    i = submit_coupon(vp, user=OTHER)
    i.response.send_message.assert_awaited_once()
    assert db.coupons == {}
    db.fail = True
    i = submit_coupon(vp)
    assert "Nothing was changed" in i.response.send_message.call_args.args[0]
    vp.audit.assert_not_called()


def test_coupon_state(vp):
    f = vp.money.coupon_state
    assert f({"active": True, "expires_at": None, "max_uses": None, "uses": 0}) == "active"
    assert f({"active": False, "expires_at": None, "max_uses": None, "uses": 0}) == "disabled"
    assert f({"active": True, "expires_at": NOW - timedelta(days=1), "max_uses": None, "uses": 0}) == "expired"
    assert f({"active": True, "expires_at": None, "max_uses": 2, "uses": 2}) == "used up"


# ── upcoming expiries ────────────────────────────────────────────────────

def test_expiries_view_cycles_windows(vp, db):
    db.expiry_rows = [{"guild_id": GID, "clone_id": None, "expires_at": NOW + timedelta(days=2), "auto_renews": False},
                      {"guild_id": GID + 1, "clone_id": 7, "expires_at": NOW + timedelta(days=3), "auto_renews": True}]
    v = load(vp.ExpiriesView(cog(), OWNER))
    assert str(GID) in text_of(v) and "🔁" in text_of(v) and "next 7 days" in text_of(v)
    press(v, "14 days")
    assert "next 14 days" in text_of(v)
    db.fail = True
    press(v, "Refresh")
    assert "Couldn't load" in text_of(v)


# ── access ───────────────────────────────────────────────────────────────

def test_money_is_owner_only(vp, db):
    assert "money" in vp.main.allowed_sections(OWNER)
    assert "money" not in vp.ac.GRANTABLE
    assert "money" not in vp.main.allowed_sections(OTHER)
    # even a helper granted every grantable section never gets it
    vp.ac._helpers.update(ts=1e18, ok=True, map={HELPER: set(vp.ac.GRANTABLE)})
    assert "money" not in vp.main.allowed_sections(HELPER)


def test_money_screens_refuse_non_owners(vp, db):
    for view in (vp.MoneyHubView(cog(), OWNER), vp.ReverseView(cog(), OWNER), vp.CouponsView(cog(), OWNER)):
        assert run(view.interaction_check(I(user=OTHER))) is False
        assert run(view.interaction_check(I(user=OWNER))) is True


def test_home_has_a_money_button_enabled_only_for_owners(vp, db):
    home = vp.main.HomeView(cog(), OWNER)
    assert buttons(home)["Money"].disabled is False
    run(buttons(home)["Money"].callback(I()))
    home_other = vp.main.HomeView(cog(), OTHER)
    assert buttons(home_other)["Money"].disabled is True


def test_hub_buttons_fit_discord_rows(vp, db):
    hub = vp.MoneyHubView(cog(), OWNER)
    assert set(buttons(hub)) == {"Pending payments", "Failed payments", "Revenue trend", "Reverse payment", "Discount codes",
                                 "Upcoming expiries", "Back"}
    for row in (c for c in hub.walk_children() if isinstance(c, discord.ui.ActionRow)):
        assert len(row.children) <= 5
    home = vp.main.HomeView(cog(), OWNER)
    assert len(buttons(home)) <= 25


# ── schema / deploy wiring ───────────────────────────────────────────────

def test_migration_is_additive_and_idempotent_and_schema_is_bumped():
    sql = (ROOT / "database/migrations/023_admin_money.sql").read_text()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).upper()
    assert "DROP " not in code and "DELETE " not in code and "TRUNCATE" not in code
    for stmt in ("CREATE TABLE", "CREATE INDEX", "ALTER TABLE"):
        assert stmt in code
    assert code.count("CREATE TABLE") == code.count("CREATE TABLE IF NOT EXISTS")
    assert code.count("CREATE INDEX") == code.count("CREATE INDEX IF NOT EXISTS")
    assert code.count("ADD COLUMN") == code.count("ADD COLUMN IF NOT EXISTS")
    src = (ROOT / "database.py").read_text()
    import re as _re
    assert int(_re.search(r'^SCHEMA_VERSION = "(\d+)"', src, _re.M).group(1)) >= 40   # bumped at least once for this migration
    assert "023_admin_money.sql" in src


# ── webhook hooks never break the webhook ────────────────────────────────

def _gumroad_handler(monkeypatch, result, record):
    cfg = types.ModuleType("config")
    cfg.GUMROAD_WEBHOOK_SECRET = "s3cret-value"
    monkeypatch.setitem(sys.modules, "config", cfg)
    gp = types.ModuleType("gumroad_payments")
    gp.process_gumroad_ping = AsyncMock(side_effect=result if isinstance(result, Exception) else None,
                                        return_value=None if isinstance(result, Exception) else result)
    gp._alert_owner = AsyncMock()
    monkeypatch.setitem(sys.modules, "gumroad_payments", gp)
    sys.modules.pop("api.gumroad_webhook", None)
    mod = importlib.import_module("api.gumroad_webhook")
    monkeypatch.setattr(mod, "record_failure_sync", record)
    h = object.__new__(mod.handler)
    body = b"sale_id=S1&product_name=Premium&price=500&url_params%5Breference%5D=ref_9"
    h.path = "/api/gumroad_webhook?secret=s3cret-value"
    h.headers = {"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h.send_response, h.send_header, h.end_headers = MagicMock(), MagicMock(), MagicMock()
    return h


@pytest.mark.parametrize("result,recorded", [((200, "ok"), False), ((200, "already processed"), False),
                                             ((200, "product mismatch"), True), ((200, "underpaid"), True),
                                             ((500, "unlock failed"), True)])
def test_gumroad_webhook_records_only_real_failures(vp, monkeypatch, result, recorded):
    rec = MagicMock(return_value=True)
    h = _gumroad_handler(monkeypatch, result, rec)
    h.do_POST()
    h.send_response.assert_called_once_with(result[0])
    assert rec.called is recorded
    if recorded:
        assert rec.call_args.args[:2] == ("gumroad", result[1]) and rec.call_args.kwargs["reference"] == "ref_9"


def test_gumroad_webhook_crash_is_recorded_and_still_answered(vp, monkeypatch):
    rec = MagicMock(return_value=True)
    h = _gumroad_handler(monkeypatch, RuntimeError("boom"), rec)
    h.do_POST()
    h.send_response.assert_called_once_with(500)
    assert rec.call_args.args[:2] == ("gumroad", "error")


def test_gumroad_webhook_still_replies_when_the_recorder_cannot_write(vp, db, monkeypatch):
    db.fail = True                                              # real record_failure_sync against a dead database
    h = _gumroad_handler(monkeypatch, (200, "underpaid"), vp.money.record_failure_sync)
    h.do_POST()
    h.send_response.assert_called_once_with(200)
