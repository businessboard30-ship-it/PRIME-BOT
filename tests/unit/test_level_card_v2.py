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


def test_card_grows_with_a_big_background_and_stays_normal_without_one():
    av = lcd.placeholder_avatar()
    plain = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {})
    assert size_of(plain) == (900, 300)
    big_bg, err = card_assets.process("background", png((3000, 1000)))
    assert err is None
    big = lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {"custom_bg": True, "bio": "hi"}, big_bg, None, rank=3)
    assert size_of(big) == (1800, 600)
    # a background that is not switched on in the design does not change the size
    assert size_of(lcd.render_custom_level_card(av, "Ama", 7, 40, 100, {"custom_bg": False}, big_bg)) == (900, 300)


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
    with_db(monkeypatch, FakeDB())
    free = run(vlc.draft_for(7, 1))
    labels = [getattr(c, "label", "") for c in vlc.CardWizardView(7, free).children]
    assert "Save (Level Pass)" in labels
    with_db(monkeypatch, FakeDB(pass_days=5))
    paid = run(vlc.draft_for(7, 1))
    assert "Save" in [getattr(c, "label", "") for c in vlc.CardWizardView(7, paid).children]


def test_the_designer_uses_at_most_five_rows():
    d = vlc.Draft(design=dict(lcd.DEFAULT_DESIGN))
    assert max(c.row for c in vlc.CardWizardView(7, d).children) <= 4
