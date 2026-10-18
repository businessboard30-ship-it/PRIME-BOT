"""Scam Shield reads QR codes in pictures and checks the link inside against the scam rules."""
import asyncio
import io
from pathlib import Path

import pytest

from modules import scam_qr as sqr
from modules import scam_shield as ss

ROOT = Path(__file__).resolve().parents[2]
qrcode = pytest.importorskip("qrcode")
needs_zbar = pytest.mark.skipif(not sqr.available(), reason="pyzbar/libzbar0 not installed")


def run(c):
    return asyncio.run(c)


def qr_png(text: str, box: int = 8) -> bytes:
    img = qrcode.make(text, box_size=box, border=4).convert("L")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.setattr(ss._c, "domains", [(1, "hubowin.com")])
    monkeypatch.setattr(ss._c, "words", [(2, "fatowin")])


@needs_zbar
def test_decodes_a_qr_code():
    assert sqr.decode(qr_png("https://example.org/hello")) == ["https://example.org/hello"]


@needs_zbar
def test_decodes_a_small_qr_code():
    assert sqr.decode(qr_png("https://example.org/a", box=3)) == ["https://example.org/a"]


@needs_zbar
def test_scam_link_in_qr_is_a_hit(rules):
    hit = run(sqr.scan(qr_png("https://hubowin.com/claim?x=1"), ()))
    assert hit == ("qr", "domain: hubowin.com", 1)


@needs_zbar
def test_garbled_lookalike_link_in_qr_is_a_hit(rules):
    hit = run(sqr.scan(qr_png("https://hubowins.com/claim"), ()))
    assert hit and hit[0] == "qr" and hit[2] == 1


@needs_zbar
def test_benign_qr_codes_are_not_hits(rules):
    for text in ("https://github.com/anthropics", "https://discord.com/invite/abc", "WIFI:S:home;T:WPA;P:secret;;",
                 "just some words", "https://example.org/menu"):
        assert run(sqr.scan(qr_png(text), ())) is None, text


@needs_zbar
def test_allowed_domain_wins(rules):
    assert run(sqr.scan(qr_png("https://hubowin.com/x"), ("hubowin.com",))) is None


@needs_zbar
def test_picture_without_a_qr_code_gives_nothing():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(out, "PNG")
    assert sqr.decode(out.getvalue()) == []


@needs_zbar
def test_results_are_cached(monkeypatch):
    data = qr_png("https://example.org/cache")
    run(sqr.read_codes(data))
    monkeypatch.setattr(sqr, "decode", lambda d: (_ for _ in ()).throw(AssertionError("cached")))
    assert run(sqr.read_codes(data)) == ("https://example.org/cache",)


def test_fails_open(monkeypatch):
    assert sqr.decode(b"not an image") == []
    monkeypatch.setattr(sqr, "_lib", lambda: None)
    assert sqr.available() is False and sqr.decode(qr_png("https://hubowin.com")) == []
    assert run(sqr.scan(b"x", ())) is None


def test_decode_errors_never_raise(monkeypatch):
    class Boom:
        ZBarSymbol = type("Z", (), {"QRCODE": 1})
        @staticmethod
        def decode(*a, **k): raise RuntimeError("zbar crashed")
    monkeypatch.setattr(sqr, "_lib", lambda: Boom)
    assert sqr.decode(qr_png("https://hubowin.com")) == []


def test_off_switch(monkeypatch):
    monkeypatch.setenv("SCAM_QR", "0")
    assert sqr.available() is False


def test_huge_pictures_are_skipped(monkeypatch):
    monkeypatch.setattr(sqr, "MAX_PIXELS", 100)
    assert sqr.decode(qr_png("https://hubowin.com")) == []


def test_wired_into_cog_and_deploy_files():
    cog = ROOT.joinpath("discord_bot/cogs/scam_shield.py").read_text(encoding="utf-8")
    assert "sqr.scan(" in cog and cog.index("_match_qr(message, cache)") < cog.index("_match_ocr(message, cache)")
    assert "libzbar0" in ROOT.joinpath("railpack.json").read_text()
    assert "zbar" in ROOT.joinpath("nixpacks.toml").read_text()
    assert "pyzbar" in ROOT.joinpath("requirements.txt").read_text()
