"""Premium wizard (V2): perk registry, the three pages, plan routing, and the compact pay card.

The point of these tests is the buyer's path: every plan button must start the *right* checkout, the screen
must say which checkout they will get before they tap, and the pay card must be short enough for the pay
buttons to be on screen.
"""
import asyncio
import datetime as dt
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from discord_bot.cogs import _views_premium as vp
from discord_bot.cogs import _views_premium_v2 as v2
from modules import premium_perks as pp

ROOT = Path(__file__).resolve().parents[2]
GUILD = SimpleNamespace(id=4242, name="Test Server")
BUYER = 111


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


def inter(user_id=BUYER):
    i = MagicMock()
    i.user = SimpleNamespace(id=user_id, name="buyer")
    i.response.defer = AsyncMock()
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.followup.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    return i


def make(status=None, mode="split", **kw):
    return v2.PremiumWizardView(GUILD, None, BUYER, status or v2.PremiumStatus(), mode, **kw)


def buttons(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


def by_label(view, fragment):
    hits = [b for b in buttons(view) if fragment in (b.label or "")]
    assert len(hits) == 1, f"{fragment!r} matched {[b.label for b in buttons(view)]}"
    return hits[0]


def all_text(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def check_limits(view):
    """Discord's Components V2 limits: 40 components, 4000 characters of text, 5 buttons per row."""
    assert len(list(view.walk_children())) <= 40
    assert len(all_text(view)) <= 3800
    for row in (c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)):
        assert 1 <= len(row.children) <= 5
    for sel in (c for c in view.walk_children() if isinstance(c, discord.ui.Select)):
        assert len(sel.options) <= 25
    view.to_components()    # serialises without raising


# ── registry ─────────────────────────────────────────────────────────────

def test_registry_is_well_formed():
    ps = pp.perks()
    assert len({p.key for p in ps}) == len(ps) and len({p.title for p in ps}) == len(ps)
    assert {p.category for p in ps} <= set(pp.CATEGORY_BY_KEY)
    for c in pp.CATEGORIES:
        assert pp.perks_in(c.key), f"category {c.key} is empty"
    for p in ps:
        assert p.title and p.blurb and p.emoji


def test_registry_and_the_one_screen_pitch_never_disagree():
    """_PERKS (the pitch text, also what older callers show) and the registry must list the same perks."""
    pitch = (ROOT / "discord_bot/cogs/_views_premium.py").read_text(encoding="utf-8")
    pitch = pitch.split("_PERKS = (")[1].split("\n)\n")[0]
    pitch_titles = set(re.findall(r"\*\*(.+?)\*\*", pitch))
    in_pitch = {p.title for p in pp.perks() if p.in_pitch}
    assert in_pitch == pitch_titles, (in_pitch ^ pitch_titles)
    # ...and the perks that are Premium-gated in code but were never in the pitch are marked as such.
    assert {p.key for p in pp.perks() if not p.in_pitch} == {"honeypot", "auto_bump", "goodbye"}


def test_numbers_are_read_live_not_retyped(monkeypatch):
    monkeypatch.setattr(config, "PREMIUM_PLUS_REPLY_CAP", 77)
    monkeypatch.setattr(config, "PREMIUM_PLUS_SCAM_VISION_DAILY", 88)
    rows = {r[0]: r for r in pp.limit_rows()}
    assert rows["AI chat per day"][3] == "77" and rows["AI scam checks/day"][3] == "88"
    assert "77" in pp.plus_extras() and "88" in pp.plus_extras()
    ai = next(p for p in pp.perks() if p.key == "ai_chat")
    assert ai.plus == "77 a day per person"


def test_compare_table_is_phone_width():
    table = v2.render_compare_table(pp.limit_rows())
    for line in table.strip("`\n").splitlines():
        assert len(line) <= 40, line


# ── status text ──────────────────────────────────────────────────────────

def test_status_text_variants():
    assert "Not active" in v2.status_text(v2.PremiumStatus())
    assert "Lifetime" in v2.status_text(v2.PremiumStatus(active=True, lifetime=True, expires_ts=1))
    t = v2.status_text(v2.PremiumStatus(active=True, expires_ts=1700000000, plus_ts=1700000999))
    assert "<t:1700000000:D>" in t and "Plus is active" in t and "<t:1700000999:D>" in t
    assert "Couldn't check" in v2.status_text(v2.PremiumStatus(unknown=True))


def test_load_status_active_lifetime_and_failure(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)

    async def go():
        monkeypatch.setattr(v2.db, "get_guild_premium", AsyncMock(return_value={
            "expires_at": now + dt.timedelta(days=10), "plus_expires_at": now + dt.timedelta(days=5)}))
        monkeypatch.setattr(v2.db, "is_guild_premium_active", AsyncMock(return_value=True))
        monkeypatch.setattr(v2.db, "is_guild_plus_active", AsyncMock(return_value=True))
        s = await v2.load_status(1, None)
        assert s.active and not s.lifetime and s.plus_ts

        monkeypatch.setattr(v2.db, "get_guild_premium", AsyncMock(return_value={
            "expires_at": now + dt.timedelta(days=36500), "plus_expires_at": None}))
        assert (await v2.load_status(1, None)).lifetime

        monkeypatch.setattr(v2.db, "is_guild_premium_active", AsyncMock(return_value=False))
        assert not (await v2.load_status(1, None)).active

        monkeypatch.setattr(v2.db, "get_guild_premium", AsyncMock(side_effect=RuntimeError("db down")))
        assert (await v2.load_status(1, None)).unknown       # still lets the buyer pick a plan
    run(go())


# ── the pages ────────────────────────────────────────────────────────────

def test_every_page_fits_discord_limits():
    async def go():
        statuses = [v2.PremiumStatus(), v2.PremiumStatus(unknown=True),
                    v2.PremiumStatus(active=True, expires_ts=1, plus_ts=2),
                    v2.PremiumStatus(active=True, lifetime=True, expires_ts=1, plus_ts=2)]
        for s in statuses:
            check_limits(make(s))
        for cat in (None, *(c.key for c in pp.CATEGORIES)):
            check_limits(make(page="perks", category=cat))
        check_limits(make(page="compare"))
    run(go())


def test_home_is_compact_and_shows_the_prices():
    async def go():
        v = make()
        labels = [b.label for b in buttons(v)]
        for needle in (f"Monthly ${config.PREMIUM_FEE_USD:g}", f"Yearly ${config.PREMIUM_YEARLY_FEE_USD:g}",
                       f"Lifetime ${config.PREMIUM_LIFETIME_FEE_USD:g}", "50% off first year",
                       f"Plus ${config.PREMIUM_PLUS_FEE_USD:g}/mo", f"Plus ${config.PREMIUM_PLUS_YEARLY_FEE_USD:g}/yr",
                       "What's included", "Compare"):
            assert any(needle in l for l in labels), needle
        text = all_text(v)
        assert "PrimeBot Premium" in text and "✨ Plus" in text
        # compact: three rows of buttons for plans and info, no more
        rows = [c for c in v.walk_children() if isinstance(c, discord.ui.ActionRow)]
        assert len(rows) == 3, len(rows)
    run(go())


def test_home_says_which_checkout_the_buyer_will_get():
    async def go():
        split = all_text(make(mode="split"))
        assert "Paystack" in split and "Gumroad" in split and "next screen" in split
        gum = all_text(make(mode="gumroad"))
        assert "Gumroad" in gum and "Paystack" not in gum.replace("first-time offer", "")
        auto = all_text(make(mode="auto"))
        assert "Pay Now" in auto and "Verify" in auto
        # the 50% offer is Gumroad-only (premium_discount -> start_gumroad_payment): the note sitting under
        # the offer button itself must say so, so a Ghana buyer isn't surprised by a missing Paystack button
        for mode in ("split", "auto", "gumroad"):
            v = make(mode=mode)
            note = next(c.content for c in v.walk_children()
                        if isinstance(c, discord.ui.TextDisplay) and "First-time offer: Gumroad only" in c.content)
            assert "Gumroad" in note and "PayPal" in note
    run(go())


def test_lifetime_server_sees_plus_but_not_premium_plans():
    async def go():
        v = make(v2.PremiumStatus(active=True, lifetime=True, expires_ts=1))
        labels = " | ".join(b.label for b in buttons(v))
        assert "/mo" in labels and "/yr" in labels
        assert "Monthly $" not in labels and "Yearly $" not in labels and "Lifetime $" not in labels
        assert "50% off" not in labels
        assert "Lifetime Premium is active" in all_text(v)
    run(go())


# ── navigation ───────────────────────────────────────────────────────────

def test_navigation_between_pages_edits_in_place():
    async def go():
        v = make()
        i = inter()
        await by_label(v, "What's included").callback(i)
        assert v.page == "perks" and v.category is None
        i.response.edit_message.assert_awaited_once_with(view=v)
        assert "What's included" in all_text(v)

        sel = next(c for c in v.walk_children() if isinstance(c, discord.ui.Select))
        sel._values = ["ai"]                        # what Discord sends after a pick
        i2 = inter()
        await sel.callback(i2)
        assert v.page == "perks" and v.category == "ai" and "AI Chat" in all_text(v)

        await by_label(v, "Compare plans").callback(inter())
        assert v.page == "compare" and "Free vs Premium vs Plus" in all_text(v)

        await by_label(v, "Back to plans").callback(inter())
        assert v.page == "home" and "50% off first year" in " ".join(b.label for b in buttons(v))
    run(go())


def test_category_page_shows_free_premium_and_plus_for_every_perk():
    async def go():
        v = make(page="perks", category="mod")
        text = all_text(v)
        for p in pp.perks_in("mod"):
            assert p.title in text
        assert "Free:" in text and "💎 Premium:" in text and "✨ Plus:" in text   # Scam Shield has a Plus value
    run(go())


def test_compare_page_lists_the_premium_only_perks_and_prices():
    async def go():
        text = all_text(make(page="compare"))
        for t in pp.premium_only_titles():
            assert t in text
        assert f"${config.PREMIUM_PLUS_YEARLY_FEE_USD:g}/year" in text
    run(go())


def test_only_the_buyer_can_press_buttons():
    async def go():
        v = make()
        stranger = inter(user_id=999)
        assert await v.interaction_check(stranger) is False
        stranger.response.send_message.assert_awaited_once()
        assert await v.interaction_check(inter()) is True
    run(go())


# ── paying: the right plan reaches the right checkout ────────────────────

PLANS = [
    ("Monthly $", "premium"),
    ("Yearly $", "premium_yearly"),
    ("Lifetime $", "premium_lifetime"),
    ("/mo", "premium_plus"),
    ("/yr", "premium_plus_yearly"),
]


@pytest.mark.parametrize("fragment,plan", PLANS)
def test_each_plan_button_starts_its_own_checkout(monkeypatch, fragment, plan):
    async def go():
        start = AsyncMock()
        monkeypatch.setattr(vp, "_start_plan_checkout", start)
        v = make()
        btn = next(b for b in buttons(v) if fragment in b.label)
        i = inter()
        await btn.callback(i)
        i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        start.assert_awaited_once_with(i, GUILD, None, plan)
    run(go())


def test_the_offer_button_starts_the_discount_checkout(monkeypatch):
    async def go():
        import premium_discount as pd
        start = AsyncMock()
        monkeypatch.setattr(pd, "start_discount_checkout", start)
        monkeypatch.setattr(v2, "load_status", AsyncMock(return_value=v2.PremiumStatus()))
        i = inter()
        await by_label(make(), "50% off first year").callback(i)
        start.assert_awaited_once_with(i, GUILD, None)
    run(go())


def test_the_offer_is_refused_on_a_lifetime_server(monkeypatch):
    async def go():
        import premium_discount as pd
        start = AsyncMock()
        monkeypatch.setattr(pd, "start_discount_checkout", start)
        monkeypatch.setattr(v2, "load_status", AsyncMock(return_value=v2.PremiumStatus(active=True, lifetime=True)))
        i = inter()
        await by_label(make(), "50% off first year").callback(i)
        start.assert_not_awaited()
        assert "Lifetime" in i.followup.send.await_args.args[0]
    run(go())


def test_a_second_tap_while_the_checkout_opens_is_ignored(monkeypatch):
    async def go():
        gate = asyncio.Event()

        async def slow(*a, **k):
            await gate.wait()
        start = AsyncMock(side_effect=slow)
        monkeypatch.setattr(vp, "_start_plan_checkout", start)
        v = make()
        first = asyncio.ensure_future(v._checkout(inter(), "premium"))
        await asyncio.sleep(0)
        second = inter()
        await v._checkout(second, "premium")
        second.response.send_message.assert_awaited_once()
        assert "one moment" in second.response.send_message.await_args.args[0]
        gate.set()
        await first
        assert start.await_count == 1 and v._busy is False       # and a later tap works again
    run(go())


def test_a_checkout_error_tells_the_buyer_and_unlocks_the_buttons(monkeypatch):
    async def go():
        monkeypatch.setattr(vp, "_start_plan_checkout", AsyncMock(side_effect=RuntimeError("gateway down")))
        v = make()
        i = inter()
        await v._checkout(i, "premium_yearly")
        assert "tap the plan again" in i.followup.send.await_args.args[0]
        assert v._busy is False
    run(go())


# ── the pay card ─────────────────────────────────────────────────────────

def _checkout_env(monkeypatch, *, premium_row=None, plus_active=False):
    sent = {}

    async def fake_start(interaction, **kw):
        sent.update(kw)
    import payments_manual
    monkeypatch.setattr(payments_manual, "start_dual_mode_payment", fake_start)
    monkeypatch.setattr(vp.db, "get_guild_premium", AsyncMock(return_value=premium_row))
    monkeypatch.setattr(vp.db, "is_guild_premium_active", AsyncMock(return_value=bool(premium_row)))
    monkeypatch.setattr(vp.db, "is_guild_plus_active", AsyncMock(return_value=plus_active))
    return sent


@pytest.mark.parametrize("plan,fee", [
    ("premium", config.PREMIUM_FEE_USD), ("premium_yearly", config.PREMIUM_YEARLY_FEE_USD),
    ("premium_lifetime", config.PREMIUM_LIFETIME_FEE_USD), ("premium_plus", config.PREMIUM_PLUS_FEE_USD),
    ("premium_plus_yearly", config.PREMIUM_PLUS_YEARLY_FEE_USD)])
def test_pay_card_is_short_and_carries_the_right_payment(monkeypatch, plan, fee):
    async def go():
        sent = _checkout_env(monkeypatch)
        await vp._start_plan_checkout(inter(), GUILD, None, plan)
        assert sent["payment_type"] == plan and sent["price_usd"] == fee and sent["guild_id"] == GUILD.id
        assert GUILD.name in sent["product_title"]
        desc = sent["product_description"]
        # short: the pay buttons must not be pushed off screen by a wall of perks (the old text was ~1,400 chars)
        assert len(desc) < 450, len(desc)
        assert "Not included: clones" in desc
        if plan.startswith("premium_plus"):
            assert "Cancel the old Premium membership" in desc and "Everything in Premium" in desc
        else:
            assert "Premium perks" in desc
    run(go())


def test_lifetime_server_can_buy_plus_but_not_premium(monkeypatch):
    async def go():
        row = {"expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=36500), "plus_expires_at": None}
        sent = _checkout_env(monkeypatch, premium_row=row)
        i = inter()
        await vp._start_plan_checkout(i, GUILD, None, "premium_yearly")
        assert not sent and "Lifetime Premium" in i.followup.send.await_args.args[0]
        await vp._start_plan_checkout(inter(), GUILD, None, "premium_plus")
        assert sent["payment_type"] == "premium_plus"
    run(go())


def test_renewal_note_shows_when_time_will_be_added(monkeypatch):
    async def go():
        row = {"expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=9), "plus_expires_at": None}
        sent = _checkout_env(monkeypatch, premium_row=row)
        await vp._start_plan_checkout(inter(), GUILD, None, "premium")
        assert "adds its time on top" in sent["product_description"]
    run(go())


# ── entry points ─────────────────────────────────────────────────────────

def test_open_wizard_sends_one_ephemeral_layout_view(monkeypatch):
    async def go():
        monkeypatch.setattr(v2, "load_status", AsyncMock(return_value=v2.PremiumStatus()))
        monkeypatch.setattr(v2.db, "get_payment_mode", AsyncMock(return_value="gumroad"))
        i = inter()
        await v2.open_premium_wizard(i, GUILD, None)
        i.followup.send.assert_awaited_once()
        kw = i.followup.send.await_args.kwargs
        assert kw["ephemeral"] is True and isinstance(kw["view"], discord.ui.LayoutView)
        assert "content" not in kw and kw["view"].message is i.followup.send.return_value
        assert kw["view"].mode == "gumroad"
    run(go())


def test_payment_mode_lookup_failure_falls_back_to_showing_both(monkeypatch):
    async def go():
        monkeypatch.setattr(v2.db, "get_payment_mode", AsyncMock(side_effect=RuntimeError("x")))
        assert await v2._payment_mode(None) == "split"
    run(go())


def test_every_old_entry_point_now_lands_in_the_wizard(monkeypatch):
    async def go():
        opened = AsyncMock()
        monkeypatch.setattr(v2, "open_premium_wizard", opened)
        guild = SimpleNamespace(id=1, name="g")
        i = inter()
        i.client.get_guild = MagicMock(return_value=guild)
        await vp.send_premium_pitch(i, 1, None)                 # ~25 callers: locked features, /premium, join DM ...
        opened.assert_awaited_once_with(i, guild, None)

        # renewal DM "Subscribe" button
        opened.reset_mock()
        item = vp.PremiumSubscribeButton(1, None)
        i2 = inter()
        i2.client.get_guild = MagicMock(return_value=guild)
        await item.callback(i2)
        opened.assert_awaited_once()

        # guild not cached: the plain-text pitch still goes out (no buttons to build)
        i3 = inter()
        i3.client.get_guild = MagicMock(return_value=None)
        monkeypatch.setattr(vp.db, "get_guild_premium", AsyncMock(return_value=None))
        await vp.send_premium_pitch(i3, 1, None)
        assert "Go Premium" in i3.followup.send.await_args.args[0]
    run(go())


def test_timed_out_menu_retires_itself_instead_of_leaving_dead_buttons():
    async def go():
        v = make()
        v.message = MagicMock(edit=AsyncMock())
        await v.on_timeout()
        v.message.edit.assert_awaited_once()
        assert "timed out" in all_text(v) and not buttons(v)
        # a failing edit (token expired) must never raise out of on_timeout
        v.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=404), "gone"))
        await v.on_timeout()
    run(go())
