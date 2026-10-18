"""Scam Shield reads the words out of a caption-less scam picture and runs them through the text rules."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules import scam_ocr as sco
from modules import scam_shield as ss

IMG = Path(__file__).parent / "data" / "scam_fake_streamer_post.jpg"
needs_tesseract = pytest.mark.skipif(not sco.tesseract_path(), reason="tesseract not installed")


def run(c):
    return asyncio.run(c)


def test_short_or_empty_text_is_never_matched(monkeypatch):
    monkeypatch.setattr(ss, "match_text", lambda t, a=(): ("word", "x", 1))
    assert sco.match("", ()) is None and sco.match("hubowin", ()) is None
    assert sco.match("go to hubowin.com and use promo code BET today", ())[0] == "image_text"


def test_hit_is_labelled_and_keeps_the_rule_id(monkeypatch):
    monkeypatch.setattr(ss, "match_text", lambda t, a=(): ("domain", "hubowin.com", 7))
    assert sco.match("x" * 40, ()) == ("image_text", "domain: hubowin.com", 7)


def test_off_switch(monkeypatch):
    monkeypatch.setenv("SCAM_OCR", "0")
    assert sco.available() is False


def test_not_a_picture_or_too_big_gives_no_text():
    assert sco.prepare(b"not an image") is None
    assert run(sco.read_text(b"not an image")) == ""


def test_missing_tesseract_fails_open(monkeypatch):
    monkeypatch.setattr(sco, "tesseract_path", lambda: None)
    assert run(sco.read_text(IMG.read_bytes())) == ""


@needs_tesseract
def test_reads_the_fake_streamer_post_and_the_text_rules_catch_it():
    text = run(sco.read_text(IMG.read_bytes()))
    assert "hubowin" in text.lower() and "promo code" in text.lower()
    hit = sco.match(text, ())            # no seeded rules needed: the promo-code + "will be deleted" bait rule fires
    assert hit and hit[0] == "image_text"


@needs_tesseract
def test_seeded_domain_rule_catches_it_and_allowed_domains_still_win(monkeypatch):
    monkeypatch.setattr(ss._c, "domains", [(1, "hubowin.com")])
    monkeypatch.setattr(ss._c, "words", [])
    text = run(sco.read_text(IMG.read_bytes()))
    assert sco.match(text, ())[1] == "domain: hubowin.com"
    assert "domain" not in (sco.match(text, ("hubowin.com",)) or ("", ""))[1]


@needs_tesseract
def test_second_read_comes_from_the_cache(monkeypatch):
    data = IMG.read_bytes()
    run(sco.read_text(data))
    monkeypatch.setattr(sco, "prepare", lambda d: (_ for _ in ()).throw(AssertionError("should be cached")))
    assert run(sco.read_text(data))


def test_starter_rules_are_seeded_and_wired():
    root = Path(__file__).resolve().parents[2]
    sql = root.joinpath("database/migrations/055_scam_shield_hubowin.sql").read_text(encoding="utf-8")
    assert "'hubowin.com'" in sql and "ON CONFLICT (kind, pattern) DO NOTHING" in sql
    h = ss.dhash(IMG.read_bytes())
    assert f"{h:016x}" in sql
    assert "055_scam_shield_hubowin.sql" in root.joinpath("database.py").read_text(encoding="utf-8")
    assert "tesseract-ocr" in root.joinpath("railpack.json").read_text()
