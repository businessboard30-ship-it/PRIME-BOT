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


# ── domain extraction + fuzzy matching ───────────────────────────────────
PAD = " and claim your reward today at the site"


@pytest.fixture
def rules(monkeypatch):
    monkeypatch.setattr(ss._c, "domains", [(1, "hubowin.com"), (2, "fatowin.com")])
    monkeypatch.setattr(ss._c, "words", [])


@pytest.mark.parametrize("text", [
    "visit hubowin[.]com", "visit hubowin (dot) com", "visit hub0win . com", "visit hubovvin.com",
    "visit hubow1n.corn", "visit hubowins.com", "visit hubwin.com", "go to HUBOWIN,com",
])
def test_garbled_scam_domain_is_still_caught(rules, text):
    hit = sco.match(text + PAD, ())
    assert hit and hit[0] == "image_text" and hit[1].startswith("domain: hubowin.com") and hit[2] == 1


def test_fatowin_read_with_a_one_for_the_i(rules):
    assert sco.match("use fatow1n dot com now" + PAD, ())[2] == 2


@pytest.mark.parametrize("text", [
    "discord.com/channels and discordapp.com", "https://github.com and google.com and wikipedia.org",
    "check out hubo.com and win.com and hub.com", "the quick brown fox. Com and more words here friends",
    "a long sentence about win and hub with www.example.org inside",
])
def test_benign_text_and_real_sites_are_not_hit(rules, text):
    assert sco.match(text + PAD, ()) is None


def test_allowed_domain_and_official_host_win_over_fuzzy(rules):
    assert sco.match("visit hubowins.com" + PAD, ("hubowins.com",)) is None
    monkey_rules = [(3, "discorb.com")]
    ss._c.domains = monkey_rules
    assert sco.match("login at discord.com now" + PAD, ()) is None


def test_short_names_need_an_exact_match(monkeypatch):
    monkeypatch.setattr(ss._c, "domains", [(5, "abc.com")])
    assert sco.match("visit abd.com" + PAD, ()) is None      # 3-letter name: no edit-distance guesses


def test_punycode_lookalike_is_caught(rules):
    host = "hub\u043ewin.com".encode("idna").decode()         # Cyrillic 'o'
    assert host.startswith("xn--")
    hit = sco.match(f"visit {host}" + PAD, ())
    assert hit and hit[2] == 1


def test_clean_urls_only_touches_dot_gaps_before_a_tld():
    assert sco.clean_urls("hubowin [.] com") == "hubowin.com"
    assert sco.clean_urls("it ended. Then we left") == "it ended. Then we left"


def test_within_one():
    assert sco._within_one("hubowin.com", "hubowins.com") and sco._within_one("hubowin.com", "hubwin.com")
    assert not sco._within_one("hubowin.com", "hubxxin.com")


# ── extra passes ─────────────────────────────────────────────────────────
def test_scan_skips_extra_passes_when_first_read_hits(monkeypatch, rules):
    async def first(d): return "visit hubowin.com now" + PAD
    async def more(d, only_urlbar=False): raise AssertionError("no extra pass needed")
    monkeypatch.setattr(sco, "read_text", first)
    monkeypatch.setattr(sco, "read_more", more)
    assert run(sco.scan(b"x", ()))[2] == 1


def test_scan_runs_extra_passes_when_first_read_is_empty(monkeypatch, rules):
    calls = []
    async def first(d): return ""
    async def more(d, only_urlbar=False):
        calls.append(only_urlbar)
        return "hubowin [.] com" + PAD
    monkeypatch.setattr(sco, "read_text", first)
    monkeypatch.setattr(sco, "read_more", more)
    monkeypatch.setattr(sco, "available", lambda: True)
    assert run(sco.scan(b"x", ()))[2] == 1 and calls == [False]


def test_scan_only_reads_the_top_strip_when_text_is_fine_but_has_no_link(monkeypatch, rules):
    calls = []
    async def first(d): return "just a long chat about the weather and lunch today " * 2
    async def more(d, only_urlbar=False):
        calls.append(only_urlbar)
        return ""
    monkeypatch.setattr(sco, "read_text", first)
    monkeypatch.setattr(sco, "read_more", more)
    monkeypatch.setattr(sco, "available", lambda: True)
    assert run(sco.scan(b"x", ())) is None and calls == [True]


def test_scan_fails_open(monkeypatch):
    async def boom(d): raise RuntimeError("x")
    monkeypatch.setattr(sco, "read_text", boom)
    assert run(sco.scan(b"x", ())) is None


def test_variants_build_crops_and_inverted_copy():
    if not IMG.exists():
        pytest.skip("fixture missing")
    v = sco.variants(IMG.read_bytes())
    assert 3 <= len(v) <= 4 and all(psm in ("4", "6", "11") for _, psm in v)
    assert len(sco.variants(IMG.read_bytes(), only_urlbar=True)) == 1
    assert sco.variants(b"nope") == []


@needs_tesseract
def test_scan_on_the_real_fixture_still_hits(monkeypatch):
    monkeypatch.setattr(ss._c, "domains", [(1, "hubowin.com")])
    monkeypatch.setattr(ss._c, "words", [])
    assert run(sco.scan(IMG.read_bytes(), ()))[0] == "image_text"


# ── learned picture hashes ───────────────────────────────────────────────
class _Conn:
    def __init__(self, n=0): self.n = n
    async def fetchval(self, *a): return self.n
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Pool:
    def __init__(self, n=0): self.c = _Conn(n)
    def acquire(self): return self.c


def _patch_pool(monkeypatch, n=0):
    async def pool(): return _Pool(n)
    monkeypatch.setattr(ss, "_pool", pool)
    added = []
    async def add_rule(kind, pattern, by, note=None):
        added.append((kind, pattern, by, note))
        return 99
    monkeypatch.setattr(ss, "add_rule", add_rule)
    return added


def test_learn_image_stores_the_hash_with_a_learned_note(monkeypatch):
    added = _patch_pool(monkeypatch)
    monkeypatch.setattr(ss._c, "images", [])
    assert run(ss.learn_image(IMG.read_bytes(), "OCR domain: hubowin.com")) == 99
    kind, pattern, by, note = added[0]
    assert kind == "image" and pattern == f"{ss.dhash(IMG.read_bytes()):016x}" and note.startswith(ss.LEARN_NOTE)


def test_learn_image_skips_near_duplicates_caps_and_junk(monkeypatch):
    added = _patch_pool(monkeypatch)
    h = ss.dhash(IMG.read_bytes())
    monkeypatch.setattr(ss._c, "images", [(1, h ^ 0b11)])         # already known (2 bits apart)
    assert run(ss.learn_image(IMG.read_bytes(), "x")) is None
    monkeypatch.setattr(ss._c, "images", [])
    assert run(ss.learn_image(b"not an image", "x")) is None
    added2 = _patch_pool(monkeypatch, n=ss.LEARNED_MAX)           # table full of learned rows
    assert run(ss.learn_image(IMG.read_bytes(), "x")) is None and not added and not added2


def test_learn_image_never_raises(monkeypatch):
    async def pool(): raise RuntimeError("db down")
    monkeypatch.setattr(ss, "_pool", pool)
    monkeypatch.setattr(ss._c, "images", [])
    assert run(ss.learn_image(IMG.read_bytes(), "x")) is None


def test_cog_learns_only_from_curated_ocr_hits():
    src = Path(__file__).resolve().parents[2].joinpath("discord_bot/cogs/scam_shield.py").read_text(encoding="utf-8")
    assert 'startswith(("word:", "domain:"))' in src and "sco.scan(" in src
    assert "rapidfuzz" in Path(__file__).resolve().parents[2].joinpath("requirements.txt").read_text()
