"""Level card v2: bigger cards, shared rank card, real-level preview, reset / randomize / share, cached gradient."""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from PIL import Image

from discord_bot.cogs import _views_level_card as vlc
from modules import card_assets, level_card_design as lcd
from tests.unit.test_level_pass import FakeDB, Inter, with_db


def run(c):
    return asyncio.run(c)


def png(size, color=(30, 90, 160)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, format="PNG")
    return b.getvalue()


def size_of(data):
    return Image.open(io.BytesIO(data)).size


BG_ON = {"custom_bg": True}


def card(bg, design=None, **kw):
    d = {**BG_ON, **(design or {})}
    return lcd.render_custom_level_card(lcd.placeholder_avatar(), "Ama", 7, 40, 100, d, bg, None, **kw)


def test_card_is_the_image_size_and_900x300_without_one():
    av = lcd.placeholder_avatar()
    assert size_of(lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {})) == (900, 300)
    for size in ((3000, 1000), (1200, 1200), (600, 1800), (2400, 300), (1000, 400)):          # wide, square, tall, strip
        bg, err = card_assets.process("background", png(size))
        assert err is None
        assert size_of(card(bg, {"bio": "hi"}, rank=3)) == size, size
    # a background that is not switched on in the design does not change the size
    bg, _ = card_assets.process("background", png((3000, 1000)))
    assert size_of(lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {"custom_bg": False}, bg)) == (900, 300)


def test_layout_scales_by_the_smaller_ratio_and_centres_the_band():
    assert lcd.layout_scale(900, 300) == 1.0 and lcd.layout_scale(1800, 600) == 2.0
    assert lcd.layout_scale(1200, 1200) == 1200 / 900                                  # square: width limits
    assert lcd.layout_scale(2400, 300) == 1.0                                          # strip: height limits
    S, ox, oy = lcd._band(2400, 300)
    assert (S, ox, oy) == (1.0, 750.0, 0.0)                                            # wide: centred horizontally
    S, ox, oy = lcd._band(900, 1500)
    assert (S, ox) == (1.0, 0.0) and oy == 600.0                                       # tall: centred vertically


def test_preview_is_a_smaller_copy_of_the_same_card():
    bg, _ = card_assets.process("background", png((4000, 2000)))
    full = card(bg)
    small = card(bg, fit_box=lcd.PREVIEW_BOX)
    assert size_of(full) == (4000, 2000) and size_of(small) == (1200, 600)
    tall, _ = card_assets.process("background", png((1000, 4000)))
    assert size_of(card(tall, fit_box=lcd.PREVIEW_BOX)) == (300, 1200)
    assert size_of(card(None, {"custom_bg": False}, fit_box=lcd.PREVIEW_BOX)) == (900, 300)


def test_very_large_image_renders_at_full_size():
    bg, err = card_assets.process("background", png((5000, 4000)))                    # 20M pixels, under the cap
    assert err is None and size_of(card(bg)) == (5000, 4000)
    assert card_assets.process("background", png((6000, 5000)))[0] is None            # 30M: over the pixel cap


def test_send_fallback_for_a_card_over_the_discord_limit():
    import os
    big = Image.frombytes("RGB", (1200, 800), os.urandom(1200 * 800 * 3))
    b = io.BytesIO()
    big.save(b, format="PNG")
    data = b.getvalue()
    assert lcd.fit_for_discord(data, limit=len(data) + 1) == data and lcd.file_ext(data) == "png"   # small enough: untouched
    jpg = lcd.fit_for_discord(data, limit=len(data) - 1)
    assert lcd.file_ext(jpg) == "jpg" and Image.open(io.BytesIO(jpg)).size == (1200, 800)            # JPEG q92, same size
    tiny = lcd.fit_for_discord(data, limit=200_000)                                                  # still too big: a smaller copy
    assert len(tiny) <= 200_000 and Image.open(io.BytesIO(tiny)).width < 1200
    assert lcd.file_name("rank", jpg) == "rank.jpg" and lcd.file_name("rank", data) == "rank.png"


def test_render_for_send_never_alters_the_stored_background():
    bg, _ = card_assets.process("background", png((1500, 500)))
    before = bytes(bg)
    lcd.render_for_send(lcd.placeholder_avatar(), "Ama", 7, 40, 100, BG_ON, bg, None)
    assert bg == before


def test_hidden_elements_reflow_and_old_designs_show_everything():
    av = lcd.placeholder_avatar()
    # old saved design: none of the show_* keys -> everything shown
    old, err = lcd.validate({"background": "ocean", "accent": "#57F287", "font": "classic", "shape": "circle", "bio": "hi",
                             "custom_bg": False, "logo": False})
    assert err is None and all(old[k] is True for k in lcd.SHOW_KEYS)
    full = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, old)
    assert lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {**old, **{k: True for k in lcd.SHOW_KEYS}}) == full
    seen = {full}
    for key in lcd.SHOW_KEYS:
        d, err = lcd.validate({**old, key: False})
        assert err is None and d[key] is False
        out = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, d)
        assert size_of(out) == (900, 300) and out not in seen                          # every switch changes the picture
        seen.add(out)
    nothing, _ = lcd.validate({**old, **{k: False for k in lcd.SHOW_KEYS}})
    assert size_of(lcd.render_custom_level_card(av, "Ama", 7, 40, 100, nothing)) == (900, 300)   # avatar only, no crash
    # the remaining block is centred: with only the name left it sits in the vertical middle (no gap above)
    only_name, _ = lcd.validate({**nothing, "show_name": True})
    im = Image.open(io.BytesIO(lcd.render_custom_level_card(av, "Ama", 7, 40, 100, only_name, None))).convert("RGB")
    rows = [y for y in range(300) if any(im.getpixel((x, y)) == (255, 255, 255) for x in range(300, 800, 3))]
    assert rows and 100 < (rows[0] + rows[-1]) / 2 < 200


def test_show_keys_are_booleans_and_unknown_keys_are_not_stored():
    assert lcd.validate({"show_bar": "no"})[1] and lcd.validate({"show_name": 0})[1]
    d, err = lcd.validate({"show_bar": False, "show_bogus": False})
    assert err is None and d["show_bar"] is False and "show_bogus" not in d
    assert set(lcd.options()["show"]) == set(lcd.SHOW_KEYS) == {"show_title", "show_name", "show_level", "show_bar",
                                                                 "show_xp", "show_bio"}


def test_hidden_bio_is_not_drawn_even_when_set():
    av = lcd.placeholder_avatar()
    shown = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {"bio": "hello there"})
    hidden = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {"bio": "hello there", "show_bio": False})
    nobio = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {"bio": ""})
    assert shown != hidden and hidden == nobio


def test_title_and_rank_reuse_the_same_card():
    av = lcd.placeholder_avatar()
    a = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {})
    b = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {}, title="RANK", rank=3)
    assert a != b and size_of(b) == (900, 300)


def test_gradient_is_built_once_and_reused():
    lcd._gradient_raw.cache_clear()
    lcd._gradient("#05070d", "#11172b")
    lcd._gradient("#05070d", "#11172b")
    info = lcd._gradient_raw.cache_info()
    assert info.misses == 1 and info.hits == 1
    g = lcd._gradient("#000000", "#ffffff", 20, 10)
    assert g.size == (20, 10) and g.getpixel((0, 0)) == (0, 0, 0) and g.getpixel((19, 9)) == (255, 255, 255)


def test_moderation_gets_a_small_copy_of_a_big_image():
    big = png((1800, 600))
    small = card_assets.moderation_copy(big)
    assert max(size_of(small)) <= 1024 and card_assets.moderation_copy(png((300, 100))) == png((300, 100))


def test_member_card_only_for_pass_holders(monkeypatch):
    member = SimpleNamespace(id=7, display_name="Max",
                             display_avatar=SimpleNamespace(replace=lambda **k: SimpleNamespace(read=AsyncMock(return_value=lcd.placeholder_avatar()))))
    monkeypatch.setattr(lcd, "card_for_user", AsyncMock(return_value=None))
    assert run(lcd.render_member_card(None, member, 5, 10, 50, rank=2)) is None
    monkeypatch.setattr(lcd, "card_for_user", AsyncMock(return_value=({"background": "ocean", "accent": "#57F287", "font": "classic",
                                                                         "shape": "circle", "bio": "", "custom_bg": False, "logo": False}, None, None)))
    out = run(lcd.render_member_card(None, member, 5, 10, 50, rank=2))
    assert out and size_of(out) == (900, 300)
    monkeypatch.setattr(lcd, "card_for_user", AsyncMock(side_effect=RuntimeError("db")))
    assert run(lcd.render_member_card(None, member, 5, 10, 50)) is None                      # never raises


def test_randomize_is_always_a_valid_design():
    for _ in range(50):
        d, err = lcd.validate(vlc.random_design())
        assert err is None and d["background"] in lcd.BACKGROUNDS


def test_reset_and_randomize_only_change_the_draft(monkeypatch):
    fake = with_db(monkeypatch, FakeDB(pass_days=5))
    draft = run(vlc.draft_for(7, 1))
    draft.design.update(accent="#FF0000", bio="hello")
    view = vlc.CardWizardView(7, draft)
    i = Inter()
    run(view._reset(i))
    assert draft.design["accent"] == "#57F287" and draft.design["bio"] == "" and not fake.cards
    j = Inter()
    run(view._randomize(j))
    assert draft.dirty and not fake.cards and j.edits


def test_real_level_toggle_uses_the_members_xp(monkeypatch):
    fake = with_db(monkeypatch, FakeDB())
    fake.get_xp = AsyncMock(return_value={"total_xp": 1234})
    draft = run(vlc.draft_for(7, 1))
    view = vlc.CardWizardView(7, draft)
    i = Inter()
    run(view._toggle_real(i))
    assert draft.use_real and draft.real and draft.real[0] >= 1 and draft.stats() == draft.real
    run(view._toggle_real(Inter()))
    assert not draft.use_real and draft.stats() == vlc.PREVIEW_STATS
    # no server (DM): the button is disabled
    dm = run(vlc.draft_for(7, None))
    assert any(getattr(c, "disabled", False) and c.label in ("My real level", "Sample level") for c in vlc.CardWizardView(7, dm).children)


def test_share_posts_the_preview_publicly(monkeypatch):
    with_db(monkeypatch, FakeDB())
    draft = run(vlc.draft_for(7, 1))
    view = vlc.CardWizardView(7, draft)
    i = Inter()
    i.channel = object()
    i.user.mention = "<@7>"
    run(view._share(i))
    sent = i.private[0]
    assert sent["ephemeral"] is False and sent["file"] and "level card" in sent["content"]
    dm = Inter()
    dm.channel, dm.guild_id = None, None
    run(view._share(dm))
    assert dm.private[0]["ephemeral"] is True


def test_save_button_label_shows_the_pass_requirement(monkeypatch):
    with_db(monkeypatch, FakeDB().used_up())                     # had the plan before: no trial left, so the pass label
    free = run(vlc.draft_for(7, 1))
    labels = [getattr(c, "label", "") for c in vlc.CardWizardView(7, free).children]
    assert "Save (Level Pass)" in labels
    with_db(monkeypatch, FakeDB(pass_days=5))
    paid = run(vlc.draft_for(7, 1))
    assert "Save" in [getattr(c, "label", "") for c in vlc.CardWizardView(7, paid).children]


def test_the_designer_uses_at_most_five_rows():
    d = vlc.Draft(design=dict(lcd.DEFAULT_DESIGN))
    assert max(c.row for c in vlc.CardWizardView(7, d).children) <= 4


def test_designer_button_layout():
    async def go():
        d = vlc.Draft(design=dict(lcd.DEFAULT_DESIGN), has_pass=True, guild_id=1)
        v = vlc.CardWizardView(1, d)
        rows = {}
        for c in v.children:
            rows.setdefault(c.row, []).append(c)
        assert sorted(rows) == [0, 1, 2, 3, 4] and len(v.children) == 14
        assert [type(c) for c in rows[0]] == [vlc._Pick] and rows[0][0].field_name == "background"
        assert [c.label.split(":")[0] for c in rows[1]] == ["Accent colour", "Font", "Shape", "Show on card"]
        assert [c.label for c in rows[2]] == ["Background image", "Logo", "Bio", "Remove uploads"]
        assert [c.label for c in rows[3]] == ["My real level", "Randomize", "Reset", "Share"]
        assert [c.label for c in rows[4]] == ["Save"] and rows[4][0].style == discord.ButtonStyle.success
        nopass = vlc.CardWizardView(1, vlc.Draft(design=dict(lcd.DEFAULT_DESIGN), guild_id=1))
        assert [c.label for c in nopass.children if c.row == 4] == ["Save (Level Pass)"]
    run(go())


def test_font_and_shape_buttons_cycle_and_show_menu_sets_the_switches():
    async def go():
        d = vlc.Draft(design=dict(lcd.DEFAULT_DESIGN), has_pass=True)
        assert vlc._next_key(lcd.FONTS, "classic") == "clean" and vlc._next_key(lcd.FONTS, "clean") == "classic"
        assert vlc._next_key(lcd.SHAPES, "square") == "circle"
        d.design["show_bar"] = False
        m = vlc.ShowOnCardView(1, d)
        sel = m.children[0]
        assert sel.min_values == 0 and sel.max_values == 6 and len(sel.options) == 6
        assert {o.value for o in sel.options if o.default} == set(lcd.SHOW_KEYS) - {"show_bar"}
        assert "Hidden: XP bar" in vlc.build_embed(d).description
    run(go())


# ---------- free trial (5 days, once per member; no schema change) ----------
from datetime import datetime, timedelta, timezone

from modules import entitlements as ent, user_subs
import config as app_config


class TrialInter(Inter):
    """An interaction whose response can edit the message (the trial button's)."""

    def __init__(self, uid=7):
        super().__init__(uid)
        self.response.edited = []

        async def edit_message(**kw):
            self.response.edited.append(kw)
        self.response.edit_message = edit_message


def _offered_pass(monkeypatch):
    from discord_bot.cogs import _views_leveling_boost as boost

    async def offered(clone):
        return True
    monkeypatch.setattr(boost, "pass_offered", offered)


def test_trial_rule_is_any_card_plan_row_means_not_eligible():
    now = datetime.now(timezone.utc)
    row = lambda **k: {"product": "card_plan", "status": "active", "source": "trial", "expires_at": now + timedelta(days=5), **k}
    assert ent.trial_available([]) and ent.trial_available([{"product": "dev_monthly"}])
    assert not ent.trial_available([row()]) and not ent.trial_available([row(status="expired", source="paystack")])
    assert ent.on_trial([row()], now) and not ent.on_trial([row(source="paystack")], now)
    assert not ent.on_trial([row(expires_at=now - timedelta(days=1))], now)           # ended trial: no access any more
    assert app_config.LEVEL_CARD_TRIAL_DAYS == 5


def test_save_without_any_plan_offers_the_trial_and_stores_nothing(monkeypatch):
    fake = with_db(monkeypatch, FakeDB())
    draft = run(vlc.draft_for(7, 1))
    assert draft.trial_available and not draft.has_pass
    assert "free 5-day trial" in vlc.build_embed(draft).description
    i = Inter()
    run(vlc.save_draft(i, draft))
    sent = i.private[0]
    assert sent["embed"].title.startswith("🎁") and isinstance(sent["view"], vlc.TrialView) and sent["ephemeral"] is True
    assert [b.label for b in sent["view"].children][0] == "Start free 5-day trial"
    assert not fake.cards and "card_plan" not in fake.ent                                # nothing saved, nothing granted yet


def test_save_button_label_follows_the_state():
    async def go():
        base = dict(design=dict(lcd.DEFAULT_DESIGN), guild_id=1)
        label = lambda d: next(c.label for c in vlc.CardWizardView(7, d).children if c.row == 4)
        assert label(vlc.Draft(**base)) == "Save (Level Pass)"
        assert label(vlc.Draft(**base, trial_available=True)) == "Save (free trial)"
        assert label(vlc.Draft(**base, has_pass=True)) == "Save"
    run(go())


def test_starting_the_trial_grants_5_days_and_saves_the_draft(monkeypatch):
    fake = with_db(monkeypatch, FakeDB())
    draft = run(vlc.draft_for(7, 1))
    run(vlc.edit_draft(Inter(), draft, accent="#ff0000", bio="trial bio"))
    designer = Inter()
    run(vlc.save_draft(designer, draft))                                                  # Save pressed -> trial offer
    view = designer.private[0]["view"]
    press = TrialInter()
    run(view._start(press))
    row = fake.ent["card_plan"]
    assert row["source"] == "trial" and row["status"] == "active"
    days = (row["expires_at"] - datetime.now(timezone.utc)).total_seconds() / 86400
    assert 4.9 < days <= 5.0
    saved = json.loads(fake.cards["7"])
    assert saved["accent"] == "#FF0000" and saved["bio"] == "trial bio"                  # saved straight away
    assert press.response.edited and "trial has started" in press.response.edited[0]["content"]
    assert designer.edits and "Saved" in designer.edits[-1]["embed"].description         # the designer message was redrawn
    assert ent.has_access(run(fake.entitlements_list("7")), ("card_plan",))               # same gate the bot/website use
    assert not run(vlc.draft_for(7, 1)).trial_available and run(vlc.draft_for(7, 1)).on_trial
    assert "Free trial active" in vlc.build_embed(run(vlc.draft_for(7, 1))).description


def test_the_trial_cannot_be_started_twice_or_by_someone_who_had_the_plan(monkeypatch):
    fake = with_db(monkeypatch, FakeDB())
    _offered_pass(monkeypatch)
    assert run(fake.entitlement_start_trial("7", 5)) is True
    first = fake.ent["card_plan"]["expires_at"]
    assert run(fake.entitlement_start_trial("7", 5)) is False and fake.ent["card_plan"]["expires_at"] == first
    paid = with_db(monkeypatch, FakeDB(pass_days=10))                                     # holder of a pass: no trial row written
    assert run(paid.entitlement_start_trial("7", 5)) is False and paid.ent["card_plan"]["source"] == "t"
    # trial already used (ended): Save shows the Level Pass offer, not another trial
    ended = with_db(monkeypatch, FakeDB().used_up())
    draft = run(vlc.draft_for(7, 1))
    assert not draft.trial_available
    i = Inter()
    run(vlc.save_draft(i, draft))
    assert i.private[0]["embed"].title == "🎫 Level Pass" and not ended.cards
    # pressing Start after the trial was taken elsewhere (two tabs): told so, offered the pass
    racer = with_db(monkeypatch, FakeDB())
    draft2 = run(vlc.draft_for(7, 1))
    d = Inter()
    run(vlc.save_draft(d, draft2))
    run(racer.entitlement_start_trial("7", 5))
    press = TrialInter()
    run(d.private[0]["view"]._start(press))
    assert "already used" in press.response.edited[0]["content"] and not racer.cards


def test_buying_the_pass_after_the_trial_just_extends_it():
    now = datetime.now(timezone.utc)
    trial = {"product": "card_plan", "status": "active", "source": "trial", "expires_at": now + timedelta(days=2),
             "cancel_at_period_end": False}
    change = user_subs.transition(trial, "card_plan", "charge", now)
    assert change["status"] == "active" and change["expires_at"] == trial["expires_at"] + timedelta(days=31)


def test_trial_needs_no_schema_change():
    import inspect
    import database
    owner = next(c for c in vars(database).values() if inspect.isclass(c) and hasattr(c, "entitlement_start_trial"))
    src = inspect.getsource(owner.entitlement_start_trial)
    assert src and "DO NOTHING" in src and "DO UPDATE" not in src and "'trial'" in src
    assert "CREATE TABLE" not in src and "ALTER TABLE" not in src
