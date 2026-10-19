"""Drawn cat profile card: safe text, safe downloads, and the plain embed always stays as the fallback."""
import asyncio
from discord_bot.cogs._views_cat_adopt import view_text
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from PIL import Image

from discord_bot.cogs import _views_cat_features as feats
from modules import cat_adopt as ca
from modules import cat_announce, cat_pool, cat_share
from modules import cat_profile_card as pc


def run(c):
    return asyncio.run(c)


def png_of(color=(200, 100, 100), size=(120, 90)):
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


def cat(i, rarity="rare", name="Mochi", trait="Sleepy"):
    return ca.Cat(id=i, owner_id=10, clone_id=None, pool_id=100 + i, name=name, rarity=rarity, trait=trait)


def profile(**kw):
    base = {"bio": "Hello", "show_profile": True, "collection_size": 3, "rarest": cat(1, "legendary", "Biscuit"),
            "pinned": [cat(1), cat(2)]}
    base.update(kw)
    return base


# ── drawing ───────────────────────────────────────────────────────────────
def test_card_is_a_png_of_the_fixed_size_with_and_without_pictures():
    for avatar, thumbs in ((png_of(), [png_of(), None]), (None, []), (b"not an image", [b"junk"])):
        out = pc.draw_card("Ama", "bio", 3, ("Biscuit", "legendary"), 2, [("Mochi", "Sleepy", "rare")], avatar, thumbs)
        img = Image.open(io.BytesIO(out))
        assert img.format == "PNG" and img.size == (pc.W, pc.H)


def test_empty_profile_and_odd_text_still_draw():
    out = pc.draw_card("", "", 0, None, 0, [], None, [])
    assert Image.open(io.BytesIO(out)).size == (pc.W, pc.H)
    long = pc.draw_card("N" * 200, "word " * 400, 10 ** 9, ("X" * 80, "mythic"), 99,
                        [("C" * 99, "T" * 99, "unknown")] * 5, None, [None] * 5)
    assert Image.open(io.BytesIO(long)).size == (pc.W, pc.H)


def test_emoji_and_symbols_are_dropped_because_the_font_cannot_draw_them():
    assert pc.clean_text("Ama 🐱✨ Serwaa") == "Ama Serwaa"
    assert pc.clean_text("Kofi\n\n  Mensah") == "Kofi Mensah"
    assert pc.clean_text("Ɔkɔ ɛ") == "Ɔkɔ ɛ"                           # Twi letters are kept
    assert pc.clean_text(None) == "" and len(pc.clean_text("a" * 999, 50)) == 50


def test_wrap_never_exceeds_the_line_count_and_marks_a_cut():
    from PIL import ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    lines = pc._wrap(d, "word " * 200, pc._font(22), 400, 3)
    assert len(lines) == 3 and lines[-1].endswith("...")
    assert pc._wrap(d, "short bio", pc._font(22), 400, 3) == ["short bio"]


# ── downloads only from Discord's CDN ────────────────────────────────────
@pytest.mark.parametrize("url,ok", [
    ("https://cdn.discordapp.com/attachments/1/2/c.jpg", True), ("https://media.discordapp.net/x.png", True),
    ("http://cdn.discordapp.com/x.png", False), ("https://evil.example/x.png", False),
    ("https://cdn.discordapp.com.evil.example/x.png", False), ("file:///etc/passwd", False),
    ("https://127.0.0.1/x.png", False), ("", False), (None, False)])
def test_only_discord_cdn_https_is_fetched(url, ok):
    assert pc._allowed(url) is ok


def test_a_blocked_url_is_never_requested():
    assert run(pc._download("https://evil.example/x.png")) is None


# ── render(): gathers pictures, never raises ─────────────────────────────
def _user(read=None):
    asset = SimpleNamespace(read=read or AsyncMock(return_value=png_of()))
    asset.replace = lambda **k: asset
    return SimpleNamespace(display_avatar=asset)


def test_render_returns_a_png(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value="https://cdn.discordapp.com/a/b.jpg"))
    monkeypatch.setattr(pc, "_download", AsyncMock(return_value=png_of()))
    out = run(pc.render(SimpleNamespace(), _user(), "Ama", profile(), {"rare-hunter"}))
    assert Image.open(io.BytesIO(out)).size == (pc.W, pc.H)


def test_render_still_draws_when_every_picture_fails(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(side_effect=RuntimeError("down")))
    out = run(pc.render(SimpleNamespace(), _user(AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=404, reason="Not Found"), "x"))),
                        "Ama", profile(), set()))
    assert out is not None


def test_render_returns_none_instead_of_raising(monkeypatch):
    monkeypatch.setattr(pc, "draw_card", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    assert run(pc.render(SimpleNamespace(), _user(), "Ama", profile(), set())) is None


# ── on screen ─────────────────────────────────────────────────────────────
class FI:
    def __init__(self):
        self.client = SimpleNamespace(clone_id=None)
        self.edits = []

    async def edit_original_response(self, **k):
        self.edits.append(k)


def test_plain_embed_is_upgraded_to_the_card(monkeypatch):
    monkeypatch.setattr(pc, "render", AsyncMock(return_value=png_of()))
    i = FI()
    run(feats.upgrade_to_card(i, SimpleNamespace(display_name="Ama"), profile(), set(), footer="Hidden: only you can see this."))
    view = i.edits[0]["view"]
    pics = [g.items[0].media.url for g in view.walk_children() if isinstance(g, discord.ui.MediaGallery)]
    text = view_text(view)
    assert pics == ["attachment://cat_profile.png"] and "Ama" in text and "-# Hidden: only you can see this." in text
    assert "embed" not in i.edits[0]
    assert i.edits[0]["attachments"][0].filename == pc.FILE_NAME


def test_if_the_card_fails_the_plain_embed_stays(monkeypatch):
    monkeypatch.setattr(pc, "render", AsyncMock(return_value=None))
    i = FI()
    run(feats.upgrade_to_card(i, SimpleNamespace(display_name="Ama"), profile(), set()))
    assert i.edits == []
    monkeypatch.setattr(pc, "render", AsyncMock(return_value=png_of()))
    i.edit_original_response = AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="Err"), "x"))
    run(feats.upgrade_to_card(i, SimpleNamespace(display_name="Ama"), profile(), set()))          # must not raise


def test_card_embed_has_no_private_data():
    text = view_text(cat_share.profile_card_view("Ama"))
    assert text == "## \U0001F4D6 Ama's cat profile" and "coin" not in text.lower()


def test_share_posts_the_picture_with_the_embed(monkeypatch):
    sent = []

    class I:
        guild = SimpleNamespace(id=1)
        client = SimpleNamespace(clone_id=None)
        user = SimpleNamespace(id=10)
        channel = SimpleNamespace(id=2)
        followup = SimpleNamespace(send=AsyncMock(side_effect=lambda **k: sent.append(k)))
    monkeypatch.setattr(cat_announce, "mode_for", AsyncMock(return_value="all"))
    ok = run(cat_announce.announce_card(I(), cat_share.profile_card_view("Ama"), kind="share", png=png_of()))
    assert ok and sent[0]["file"].filename == "cat_profile.png" and "embed" not in sent[0]
    assert isinstance(sent[0]["view"], discord.ui.LayoutView)
    sent.clear()
    assert run(cat_announce.announce_card(I(), cat_share.profile_card_view("Ama"), kind="share")) and "file" not in sent[0]
    sent.clear()                                          # the older classic-embed call still works
    assert run(cat_announce.announce_embed(I(), discord.Embed(title="x"), kind="share")) and "embed" in sent[0]
