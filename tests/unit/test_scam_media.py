"""Scam Shield checks frames of animated GIF/WebP files and the pictures inside link-preview embeds."""
import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from discord_bot.cogs import scam_shield as cog_mod
from modules import scam_media as sm
from modules import scam_qr as sqr
from modules import scam_shield as ss

qrcode = pytest.importorskip("qrcode")
needs_zbar = pytest.mark.skipif(not sqr.available(), reason="pyzbar/libzbar0 not installed")
CDN = "https://cdn.discordapp.com/attachments/1/2/x.png"


def run(c):
    return asyncio.run(c)


def qr_img(text):
    return qrcode.make(text, box_size=8, border=4).convert("RGB")


def png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def animated(fmt, imgs):
    out = io.BytesIO()
    imgs[0].save(out, fmt, save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return out.getvalue()


def blank(size=(300, 300), color="white"):
    from PIL import Image
    return Image.new("RGB", size, color)


@pytest.fixture(autouse=True)
def rules(monkeypatch):
    monkeypatch.setattr(ss._c, "domains", [(1, "hubowin.com")])
    monkeypatch.setattr(ss._c, "words", [])
    monkeypatch.setattr(ss._c, "images", [])


# ── frames ───────────────────────────────────────────────────────────────
def test_still_picture_is_returned_as_is():
    data = png(blank())
    assert sm.frames(data) == [data]


@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
def test_animation_gives_first_middle_last(fmt):
    imgs = [blank(color=c) for c in ("red", "green", "blue", "black", "white")]
    fr = sm.frames(animated(fmt, imgs))
    assert len(fr) == 3 and all(f.startswith(b"\x89PNG") for f in fr)


def test_two_frame_animation_gives_two_and_junk_gives_none():
    assert len(sm.frames(animated("GIF", [blank(color="red"), blank(color="blue")]))) == 2
    assert sm.frames(b"not an image") == []


def test_huge_animation_is_skipped(monkeypatch):
    monkeypatch.setattr(sm, "MAX_PIXELS", 100)
    assert sm.frames(animated("GIF", [blank(), blank(color="red")])) == []


# ── embed urls ───────────────────────────────────────────────────────────
def emb(image=None, thumb=None, type="rich"):
    mk = lambda u: SimpleNamespace(proxy_url=u, url=u) if u else None
    return SimpleNamespace(type=type, image=mk(image), thumbnail=mk(thumb))


def test_only_discord_proxy_https_urls_are_used():
    msg = SimpleNamespace(embeds=[emb(image="https://evil.example.com/a.png"), emb(image="http://cdn.discordapp.com/a.png"),
                                  emb(image=CDN)])
    assert sm.embed_urls(msg) == [(CDN, True)]


def test_thumbnails_never_get_ocr_and_video_embeds_are_skipped():
    msg = SimpleNamespace(embeds=[emb(thumb=CDN, type="article"), emb(image=CDN + "?v", type="video")])
    assert sm.embed_urls(msg) == [(CDN, False)]


def test_embed_url_limit_and_dedupe():
    urls = [f"https://media.discordapp.net/a/{i}.png" for i in range(6)]
    msg = SimpleNamespace(embeds=[emb(image=u) for u in urls] + [emb(image=urls[0])])
    assert len(sm.embed_urls(msg)) == sm.MAX_EMBED_IMAGES


def test_fetch_refuses_other_hosts_without_a_request():
    assert run(sm.fetch("https://evil.example.com/a.png")) is None
    assert run(sm.fetch("http://cdn.discordapp.com/a.png")) is None


# ── the cog stage ────────────────────────────────────────────────────────
def make_cog(monkeypatch):
    cog = object.__new__(cog_mod.ScamShieldCog)
    cog.bot = SimpleNamespace(clone_id=None)
    monkeypatch.setattr(ss, "guild_settings", AsyncMock(return_value={"allowed_domains": ()}))
    monkeypatch.setattr(ss, "learn_image", AsyncMock(return_value=None))
    monkeypatch.setattr(cog_mod.sv, "MIN_BYTES", 10)         # the test pictures are tiny; the size gate has its own test
    return cog


def test_tiny_pictures_are_still_skipped(monkeypatch):
    cog = make_cog(monkeypatch)
    monkeypatch.setattr(cog_mod.sv, "MIN_BYTES", 15_000)
    data = animated("GIF", [blank(), blank(color="red")])
    att = gif_attachment(data)
    assert run(cog._match_extra(message([att]), {})) is None and att.read.await_count == 0


def gif_attachment(data, name="funny.gif"):
    return SimpleNamespace(id=11, filename=name, content_type="image/gif", size=len(data), width=300, height=300,
                           read=AsyncMock(return_value=data))


def message(attachments=(), embeds=()):
    return SimpleNamespace(guild=SimpleNamespace(id=5), attachments=list(attachments), embeds=list(embeds))


@needs_zbar
def test_qr_hidden_in_a_gif_frame_is_caught(monkeypatch):
    cog = make_cog(monkeypatch)
    frames = [blank(), blank(color="red"), qr_img("https://hubowin.com/claim"), blank(color="blue"), blank()]
    data = animated("GIF", frames)
    hit = run(cog._match_extra(message([gif_attachment(data)]), {}))
    assert hit == ("qr", "domain: hubowin.com", 1)


@needs_zbar
def test_benign_gif_is_not_a_hit(monkeypatch):
    cog = make_cog(monkeypatch)
    monkeypatch.setattr(cog_mod.sco, "available", lambda: False)
    data = animated("GIF", [blank(), blank(color="red"), blank(color="blue")])
    assert run(cog._match_extra(message([gif_attachment(data)]), {})) is None


@needs_zbar
def test_qr_in_a_link_preview_image_is_caught_and_learned(monkeypatch):
    cog = make_cog(monkeypatch)
    monkeypatch.setattr(sm, "fetch", AsyncMock(return_value=png(qr_img("https://hubowin.com/x"))))
    hit = run(cog._match_extra(message(embeds=[emb(image=CDN)]), {}))
    assert hit and hit[0] == "qr"
    ss.learn_image.assert_awaited_once()


@needs_zbar
def test_embed_fetch_failure_fails_open(monkeypatch):
    cog = make_cog(monkeypatch)
    monkeypatch.setattr(sm, "fetch", AsyncMock(return_value=None))
    assert run(cog._match_extra(message(embeds=[emb(image=CDN)]), {})) is None


def test_still_webp_attachment_is_left_to_the_normal_stages(monkeypatch):
    cog = make_cog(monkeypatch)
    from PIL import Image
    out = io.BytesIO()
    blank().save(out, "WEBP")
    att = SimpleNamespace(id=3, filename="a.webp", content_type="image/webp", size=len(out.getvalue()), width=300,
                          height=300, read=AsyncMock(return_value=out.getvalue()))
    monkeypatch.setattr(sqr, "scan", AsyncMock(side_effect=AssertionError("must not run")))
    assert run(cog._match_extra(message([att]), {})) is None


def test_text_only_message_costs_nothing(monkeypatch):
    cog = make_cog(monkeypatch)
    assert run(cog._match_pictures(message())) is None


# ── edit events ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("before,after,expect", [
    (dict(content="a", embeds=[], attachments=[]), dict(content="b", embeds=[], attachments=[]), True),
    (dict(content="a", embeds=[], attachments=[]), dict(content="a", embeds=[1], attachments=[]), True),
    (dict(content="a", embeds=[], attachments=[]), dict(content="a", embeds=[], attachments=[1]), True),
    (dict(content="a", embeds=[1], attachments=[]), dict(content="a", embeds=[1], attachments=[]), False),
    (dict(content="a", embeds=[1], attachments=[]), dict(content="a", embeds=[], attachments=[]), False),
])
def test_edit_inspects_on_new_text_embed_or_attachment(before, after, expect):
    cog = object.__new__(cog_mod.ScamShieldCog)
    cog._inspect = AsyncMock()
    run(cog.on_message_edit(SimpleNamespace(**before), SimpleNamespace(**after)))
    assert cog._inspect.await_count == (1 if expect else 0)
