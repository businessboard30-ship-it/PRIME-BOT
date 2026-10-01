"""Owner-panel Batch 5 tests: ads approval and marketplace moderation."""
import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

OWNER, HELPER, OTHER = 111, 333, 222
MAIN = "discord_bot.cogs._views_admin_panel"
CTRL = "discord_bot.cogs._views_admin_panel_controls"
ADSV = "discord_bot.cogs._views_admin_panel_ads"
AC = "modules.admin_controls"
WIZ = "discord_bot.cogs._views_ads_wizard"
NOW = datetime.now(timezone.utc)
MODULES = (MAIN, CTRL, ADSV, AC)


class FakeAds:
    """Behaves like modules/ads_marketplace: same status guards, same return values,
    and (like the real one) it never raises on its own."""

    def __init__(self):
        self.ads = {}
        self.listings = {}
        self.fail = False           # database down: reads empty, writes return False
        self.boom = False           # unexpected exception
        self.deactivate_listing_calls = []

    def add_ad(self, id, status="pending", **kw):
        self.ads[id] = dict(id=id, user_id=900 + id, company_name=f"Co{id}", ad_title=f"Title {id}",
                            ad_description="desc", target_url="https://example.com", budget_usd=5,
                            status=status, submitted_at=NOW, image_message_id=None,
                            rejection_reason=None, **kw)

    def add_listing(self, id, status="active", **kw):
        self.listings[id] = dict(id=id, user_id=700, service_name="svc", service_title=f"Svc {id}",
                                 description="d", price_usd=9.5, category="general",
                                 status=status, clicks=2, **kw)

    def _chk(self):
        if self.boom:
            raise RuntimeError("boom")

    async def count_ads_by_status(self):
        if self.fail:
            return {}
        out = {}
        for a in self.ads.values():
            out[a["status"]] = out.get(a["status"], 0) + 1
        return out

    async def search_ads(self, user_id=None, query="", statuses=None, limit=25):
        if self.fail:
            return []
        rows = [a for a in self.ads.values() if not statuses or a["status"] in statuses]
        return sorted(rows, key=lambda a: -a["id"])[:limit]

    async def get_ad(self, ad_id):
        return None if self.fail else (dict(self.ads[ad_id]) if ad_id in self.ads else None)

    def _move(self, ad_id, frm, to):
        self._chk()
        if self.fail:
            return False
        a = self.ads.get(ad_id)
        if a and a["status"] == frm:
            a["status"] = to
            return True
        return False

    async def approve_ad(self, ad_id): return self._move(ad_id, "pending", "approved")
    async def deactivate_ad(self, ad_id): return self._move(ad_id, "approved", "deactivated")
    async def reactivate_ad(self, ad_id): return self._move(ad_id, "deactivated", "approved")

    async def reject_ad(self, ad_id, reason):
        ok = self._move(ad_id, "pending", "rejected")
        if ok:
            self.ads[ad_id]["rejection_reason"] = reason
        return ok

    async def get_marketplace_listings(self, limit=10, offset=0):
        if self.fail:
            return []
        rows = sorted((l for l in self.listings.values() if l["status"] == "active"), key=lambda l: l["id"], reverse=True)
        return rows[offset:offset + limit]

    async def get_listing(self, listing_id):
        return None if self.fail else (dict(self.listings[listing_id]) if listing_id in self.listings else None)

    async def deactivate_listing(self, user_id, listing_id):
        self._chk()
        self.deactivate_listing_calls.append((user_id, listing_id))
        l = self.listings.get(listing_id)
        if self.fail or not l or l["user_id"] != user_id:
            return False
        l["status"] = "removed"
        return True


@pytest.fixture()
def vp(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.DISCORD_CLONE_ADMIN_IDS = {OWNER}
    cfg.DISCORD_OWNER_BROADCAST_IDS = {OWNER}
    dbm = types.ModuleType("database")
    dbm.db = MagicMock()
    fake = FakeAds()
    fake_mod = types.ModuleType("modules.ads_marketplace")
    for n in ("count_ads_by_status", "search_ads", "get_ad", "approve_ad", "deactivate_ad", "reactivate_ad",
              "reject_ad", "get_marketplace_listings", "get_listing", "deactivate_listing"):
        setattr(fake_mod, n, getattr(fake, n))
    wiz = types.ModuleType(WIZ)
    wiz.notify_rejected = AsyncMock(return_value=True)
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.setitem(sys.modules, "database", dbm)
    monkeypatch.setitem(sys.modules, "modules.ads_marketplace", fake_mod)
    monkeypatch.setitem(sys.modules, WIZ, wiz)
    import modules
    monkeypatch.setattr(modules, "ads_marketplace", fake_mod, raising=False)
    for name in MODULES:
        sys.modules.pop(name, None)
    main = importlib.import_module(MAIN)
    mod = importlib.import_module(ADSV)
    mod.main, mod.fake, mod.wiz, mod.cfg = main, fake, wiz, cfg
    mod.ac = importlib.import_module(AC)
    mod.audit = MagicMock()          # the view module's own reference
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
    i.edit_original_response = AsyncMock()
    i.followup.send = AsyncMock()
    return i


def buttons(view):
    return {getattr(c, "label", None): c for c in view.walk_children() if isinstance(c, discord.ui.Button)}


def selects(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]


def text_of(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def ads_view(vp, flt="pending"):
    v = vp.AdsView(MagicMock(), OWNER)
    v.flt = flt
    run(v.load())
    return v


def pick(v, value):
    i = I(values=[str(value)])
    run(v._pick(i))
    return i


def market_view(vp):
    v = vp.MarketView(MagicMock(), OWNER)
    run(v.load())
    return v


# ── access ───────────────────────────────────────────────────────────────

def test_ads_is_owner_only(vp, monkeypatch):
    assert "ads" in vp.main.allowed_sections(OWNER)
    assert "ads" not in vp.main.allowed_sections(OTHER)
    assert "ads" not in vp.ac.GRANTABLE
    monkeypatch.setattr(vp.ac, "helper_sections", lambda uid: {"logs"} if uid == HELPER else set())
    assert "ads" not in vp.main.allowed_sections(HELPER)
    v = vp.AdsView(MagicMock(), HELPER)
    i = I(user=HELPER)
    assert run(v.interaction_check(i)) is False
    i.response.send_message.assert_awaited()


def test_someone_elses_panel_refuses(vp):
    v = ads_view(vp)
    i = I(user=OTHER)
    assert run(v.interaction_check(i)) is False


def test_home_has_enabled_ads_button_for_owner_and_navigates(vp):
    home = vp.main.HomeView(MagicMock(), OWNER)
    b = buttons(home)
    assert "Ads" in b and not b["Ads"].disabled
    i = I()
    run(home._ads(i))
    assert isinstance(i.response.edit_message.await_args.kwargs["view"], vp.AdsView)


def test_home_ads_button_disabled_without_access(vp):
    home = vp.main.HomeView(MagicMock(), OTHER)
    assert buttons(home)["Ads"].disabled


# ── ads: list, select, approve ───────────────────────────────────────────

def test_pending_list_shows_counts_and_options(vp):
    vp.fake.add_ad(1); vp.fake.add_ad(2); vp.fake.add_ad(3, "approved")
    v = ads_view(vp)
    t = text_of(v)
    assert "**Pending** 2" in t and "**Live** 1" in t
    opts = selects(v)[0].options
    assert [o.value for o in opts] == ["2", "1"]          # newest first, pending only
    assert "Approve" not in buttons(v)                     # nothing selected yet


def test_select_shows_detail_and_actions(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp)
    pick(v, 1)
    assert "#1" in text_of(v) and "Title 1" in text_of(v)
    assert {"Approve", "Reject"} <= set(buttons(v))


def test_approve_happy_path(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    i = I()
    run(v._approve(i))
    assert vp.fake.ads[1]["status"] == "approved"
    vp.audit.assert_called_once()
    assert vp.audit.call_args.args[1] == "ads.approve"
    assert "approved" in v.notice
    assert v.selected is None and v.rows == []              # moved out of Pending
    i.response.edit_message.assert_awaited()


def test_approve_when_already_handled_is_reported_not_faked(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    vp.fake.ads[1]["status"] = "rejected"                   # someone else got there first
    i = I()
    run(v._approve(i))
    assert vp.fake.ads[1]["status"] == "rejected"
    vp.audit.assert_not_called()
    assert "Nothing was changed" in v.notice


def test_approve_database_down_changes_nothing(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    vp.fake.fail = True
    run(v._approve(I()))
    vp.audit.assert_not_called()
    assert "Nothing was changed" in v.notice
    vp.fake.fail = False
    assert vp.fake.ads[1]["status"] == "pending"


def test_approve_unexpected_exception_never_escapes(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    vp.fake.boom = True
    i = I()
    run(v._approve(i))
    vp.audit.assert_not_called()
    assert "Nothing was changed" in i.response.send_message.await_args.args[0]
    assert vp.fake.ads[1]["status"] == "pending"


def test_action_without_selection_is_refused(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp)
    i = I()
    run(v._approve(i))
    assert vp.fake.ads[1]["status"] == "pending"
    i.response.send_message.assert_awaited()


# ── ads: reject ──────────────────────────────────────────────────────────

def _modal(vp, v, reason):
    m = vp.RejectAdModal(v, v.selected)
    m.reason = types.SimpleNamespace(value=reason)
    return m


def test_reject_button_opens_modal_only_for_pending(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    i = I()
    run(v._reject(i))
    assert isinstance(i.response.send_modal.await_args.args[0], vp.RejectAdModal)


def test_reject_happy_path_notifies_and_audits(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    i = I()
    run(_modal(vp, v, "  looks like spam  ").on_submit(i))
    assert vp.fake.ads[1]["status"] == "rejected"
    assert vp.fake.ads[1]["rejection_reason"] == "looks like spam"
    vp.wiz.notify_rejected.assert_awaited_once()
    assert vp.audit.call_args.args[1] == "ads.reject"
    assert "DM" in v.notice
    i.edit_original_response.assert_awaited()


def test_reject_dm_failure_does_not_undo_rejection(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    vp.wiz.notify_rejected.side_effect = RuntimeError("dm blew up")
    run(_modal(vp, v, "spam").on_submit(I()))
    assert vp.fake.ads[1]["status"] == "rejected"
    assert "Couldn't DM" in v.notice


def test_reject_modal_rechecks_authorization(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    i = I(user=OTHER)
    run(_modal(vp, v, "spam").on_submit(i))
    assert vp.fake.ads[1]["status"] == "pending"
    vp.wiz.notify_rejected.assert_not_awaited()
    vp.audit.assert_not_called()


def test_reject_requires_a_reason(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    i = I()
    run(_modal(vp, v, "   ").on_submit(i))
    assert vp.fake.ads[1]["status"] == "pending"
    i.response.send_message.assert_awaited()


def test_reject_when_already_handled_changes_nothing(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    vp.fake.ads[1]["status"] = "approved"
    run(_modal(vp, v, "spam").on_submit(I()))
    assert vp.fake.ads[1]["status"] == "approved"
    vp.audit.assert_not_called()
    vp.wiz.notify_rejected.assert_not_awaited()


def test_reject_database_down_changes_nothing(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    vp.fake.boom = True
    i = I()
    run(_modal(vp, v, "spam").on_submit(i))
    vp.fake.boom = False
    assert vp.fake.ads[1]["status"] == "pending"
    assert "Nothing was changed" in i.followup.send.await_args.args[0]


def test_reject_modal_field_limits(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    m = vp.RejectAdModal(v, 1)
    assert m.reason.max_length == vp.REASON_MAX == 200
    assert len(m.children) <= 5


# ── ads: live / paused / rejected ────────────────────────────────────────

def test_deactivate_then_reactivate(vp):
    vp.fake.add_ad(1, "approved")
    v = ads_view(vp, "live"); pick(v, 1)
    assert "Deactivate" in buttons(v) and "Approve" not in buttons(v)
    run(v._deactivate(I()))
    assert vp.fake.ads[1]["status"] == "deactivated"
    assert vp.audit.call_args.args[1] == "ads.deactivate"
    v.flt = "paused"; run(v.load()); pick(v, 1)
    assert "Reactivate" in buttons(v)
    run(v._reactivate(I()))
    assert vp.fake.ads[1]["status"] == "approved"
    assert vp.audit.call_args.args[1] == "ads.reactivate"


def test_reactivate_cannot_resurrect_a_rejected_ad(vp):
    vp.fake.add_ad(1, "rejected")
    v = ads_view(vp, "rejected"); pick(v, 1)
    assert not {"Approve", "Reject", "Deactivate", "Reactivate"} & set(buttons(v))
    v.selected = 1
    run(v._reactivate(I()))
    assert vp.fake.ads[1]["status"] == "rejected"


def test_rejected_ad_shows_its_reason(vp):
    vp.fake.add_ad(1, "rejected")
    vp.fake.ads[1]["rejection_reason"] = "Not allowed"
    v = ads_view(vp, "rejected"); pick(v, 1)
    assert "Not allowed" in text_of(v)


def test_double_approve_second_is_a_noop(vp):
    vp.fake.add_ad(1)
    v = ads_view(vp); pick(v, 1)
    run(v._approve(I()))
    v.selected = 1
    vp.audit.reset_mock()
    run(v._approve(I()))
    assert vp.fake.ads[1]["status"] == "approved"
    vp.audit.assert_not_called()


# ── text safety + limits ─────────────────────────────────────────────────

def test_user_text_is_defused_and_clipped(vp):
    vp.fake.add_ad(1)
    vp.fake.ads[1]["ad_description"] = "hi @everyone `code` " + "x" * 5000
    vp.fake.ads[1]["company_name"] = "@here Inc"
    v = ads_view(vp); pick(v, 1)
    t = text_of(v)
    assert "@everyone" not in t and "@here" not in t
    assert "`code`" not in t
    assert len(t) < 4000


def test_discord_limits_with_many_ads(vp):
    for n in range(1, 41):
        vp.fake.add_ad(n)
        vp.fake.ads[n]["company_name"] = "C" * 120
        vp.fake.ads[n]["ad_title"] = "T" * 300
    v = ads_view(vp); pick(v, 40)
    assert len(selects(v)[0].options) == 25
    assert all(len(o.label) <= 100 for o in selects(v)[0].options)
    assert len(list(v.walk_children())) <= 40
    for row in (c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)):
        assert len(row.children) <= 5
    assert len(text_of(v)) < 4000


def test_load_failure_builds_a_warning_not_a_crash(vp):
    vp.fake.boom = True
    vp.fake.add_ad(1)
    async def bad(*a, **k): raise RuntimeError("boom")
    vp.ads.count_ads_by_status = bad
    v = vp.AdsView(MagicMock(), OWNER)
    run(v.load())
    assert "Couldn't read ads" in text_of(v)


def test_database_down_shows_empty_with_hint(vp):
    vp.fake.add_ad(1)
    vp.fake.fail = True
    v = ads_view(vp)
    assert "Refresh" in text_of(v)


# ── marketplace ──────────────────────────────────────────────────────────

def test_market_list_and_pagination(vp):
    for n in range(1, 26):
        vp.fake.add_listing(f"L{n:02d}")
    v = market_view(vp)
    assert len(selects(v)[0].options) == vp.MARKET_PAGE == 10
    b = buttons(v)
    assert b["Prev"].disabled and not b["Next"].disabled
    run(v._next(I())); run(v._next(I()))
    assert v.offset == 20 and len(v.rows) == 5
    assert buttons(v)["Next"].disabled and not buttons(v)["Prev"].disabled


def test_market_remove_is_two_step(vp):
    vp.fake.add_listing("L1")
    v = market_view(vp)
    run(v._pick(I(values=["L1"])))
    i = I()
    run(v._remove(i))                                       # first press: only asks
    assert vp.fake.listings["L1"]["status"] == "active"
    assert vp.fake.deactivate_listing_calls == []
    assert "Confirm remove" in buttons(v)
    vp.audit.assert_not_called()
    run(v._remove(I()))                                     # second press: acts
    assert vp.fake.listings["L1"]["status"] == "removed"
    assert vp.fake.deactivate_listing_calls == [(700, "L1")]   # real seller's id
    assert vp.audit.call_args.args[1] == "market.remove"
    assert v.rows == []                                      # refreshed from the table


def test_market_confirm_does_not_carry_to_another_listing(vp):
    vp.fake.add_listing("L1"); vp.fake.add_listing("L2")
    v = market_view(vp)
    run(v._pick(I(values=["L1"])))
    run(v._remove(I()))
    run(v._pick(I(values=["L2"])))                          # changed selection resets the confirm
    run(v._remove(I()))
    assert vp.fake.listings["L1"]["status"] == vp.fake.listings["L2"]["status"] == "active"


def test_market_remove_twice_is_a_noop(vp):
    vp.fake.add_listing("L1")
    v = market_view(vp)
    run(v._pick(I(values=["L1"])))
    run(v._remove(I())); run(v._remove(I()))
    vp.audit.reset_mock()
    vp.fake.deactivate_listing_calls.clear()
    v.selected, v._confirm_id = "L1", "L1"                  # stale panel pressing Confirm again
    run(v._remove(I()))
    assert vp.fake.deactivate_listing_calls == []           # already gone: never even called
    vp.audit.assert_not_called()
    assert "already gone" in v.notice


def test_market_remove_database_down_changes_nothing(vp):
    vp.fake.add_listing("L1")
    v = market_view(vp)
    run(v._pick(I(values=["L1"])))
    run(v._remove(I()))
    vp.fake.boom = True
    i = I()
    run(v._remove(i))
    vp.fake.boom = False
    assert vp.fake.listings["L1"]["status"] == "active"
    vp.audit.assert_not_called()
    assert "Nothing was changed" in i.response.send_message.await_args.args[0]


def test_market_remove_write_returns_false_is_reported(vp):
    vp.fake.add_listing("L1")
    v = market_view(vp)
    run(v._pick(I(values=["L1"])))
    run(v._remove(I()))
    orig = vp.fake.deactivate_listing
    async def nope(uid, lid): return False
    vp.ads.deactivate_listing = nope
    run(v._remove(I()))
    assert vp.fake.listings["L1"]["status"] == "active"
    vp.audit.assert_not_called()
    assert "Nothing was changed" in v.notice


def test_market_owner_only_and_navigation(vp):
    v = vp.MarketView(MagicMock(), OTHER)
    assert run(v.interaction_check(I(user=OTHER))) is False
    v = market_view(vp)
    i = I()
    run(v._back_ads(i))
    assert isinstance(i.response.edit_message.await_args.kwargs["view"], vp.AdsView)
    i2 = I()
    ads_v = ads_view(vp)
    run(ads_v._market(i2))
    assert isinstance(i2.response.edit_message.await_args.kwargs["view"], vp.MarketView)


def test_market_limits(vp):
    for n in range(1, 30):
        vp.fake.add_listing(f"L{n:02d}")
        vp.fake.listings[f"L{n:02d}"]["description"] = "d" * 2000
        vp.fake.listings[f"L{n:02d}"]["service_title"] = "S" * 300
    v = market_view(vp)
    run(v._pick(I(values=["L29"])))
    assert len(list(v.walk_children())) <= 40
    assert all(len(o.label) <= 100 for o in selects(v)[0].options)
    assert len(text_of(v)) < 4000
