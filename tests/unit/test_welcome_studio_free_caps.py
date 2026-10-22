"""Soft memory caps for servers without Premium: the in-memory preview picture is limited, nothing else is blocked."""
import io
import re

import pytest
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc


@pytest.fixture(autouse=True)
def _clean():
    for d in (cc._DRAFTS, cc._PIC_PREVIEW_LOG, cc._UNDO):
        d.clear()
    yield
    for d in (cc._DRAFTS, cc._PIC_PREVIEW_LOG, cc._UNDO):
        d.clear()


def jpeg(w, h):
    out = io.BytesIO()
    Image.effect_noise((w, h), 50).convert("RGB").save(out, format="JPEG", quality=90)
    return out.getvalue()


# ---------- picture size ----------

def test_preview_picture_is_downscaled_to_the_cap():
    small = cc._shrink_bg(jpeg(4000, 3000))
    im = Image.open(io.BytesIO(small))
    assert max(im.size) <= cc._PREVIEW_PIC_MAX_PX and im.format == "JPEG"
    assert len(small) < len(jpeg(4000, 3000)) / 4


def test_small_pictures_are_not_upscaled_and_junk_is_refused():
    im = Image.open(io.BytesIO(cc._shrink_bg(jpeg(300, 200))))
    assert im.size == (300, 200)
    assert cc._shrink_bg(b"not an image") is None


# ---------- per-person quota ----------

def test_quota_allows_a_few_pictures_then_says_how_long_to_wait():
    for _ in range(cc._PIC_PREVIEWS_PER_HOUR):
        assert cc._preview_picture_quota(1, None, 42) == (True, 0)
    ok, wait = cc._preview_picture_quota(1, None, 42)
    assert ok is False and 1 <= wait <= 60
    assert "everything else still work" in cc._quota_message(wait) and str(wait) in cc._quota_message(wait)


def test_quota_is_per_person_and_per_server_and_resets_after_an_hour(monkeypatch):
    for _ in range(cc._PIC_PREVIEWS_PER_HOUR):
        cc._preview_picture_quota(1, None, 42)
    assert cc._preview_picture_quota(1, None, 43)[0] is True      # someone else
    assert cc._preview_picture_quota(2, None, 42)[0] is True      # another server
    assert cc._preview_picture_quota(1, 7, 42)[0] is True         # a clone of the same server
    real = cc.time.monotonic()
    monkeypatch.setattr(cc.time, "monotonic", lambda: real + 3601)
    assert cc._preview_picture_quota(1, None, 42)[0] is True


def test_a_denied_picture_does_not_count_against_the_person():
    for _ in range(cc._PIC_PREVIEWS_PER_HOUR):
        cc._preview_picture_quota(1, None, 42)
    n = len(cc._PIC_PREVIEW_LOG[(1, None, 42)])
    cc._preview_picture_quota(1, None, 42)
    assert len(cc._PIC_PREVIEW_LOG[(1, None, 42)]) == n


# ---------- global byte budget ----------

def test_budget_drops_the_oldest_pictures_but_keeps_every_look(monkeypatch):
    monkeypatch.setattr(cc, "_DRAFT_BYTES_MAX", 250)
    for u in (1, 2, 3):
        cc._get_draft(9, None, u, create=True)["opts"]["font"] = "gothic"
        cc._set_draft_bg(9, None, u, b"x" * 100)
    have = {u: bool(cc._get_draft(9, None, u)["bg"]) for u in (1, 2, 3)}
    assert have == {1: False, 2: True, 3: True}                     # oldest went, newest two fit
    assert cc._draft_bytes_total() <= 250
    assert all(cc._get_draft(9, None, u)["opts"]["font"] == "gothic" for u in (1, 2, 3))   # looks untouched


def test_the_newest_picture_is_always_kept_when_it_fits():
    cc._set_draft_bg(9, None, 1, b"a" * 10)
    cc._set_draft_bg(9, None, 1, b"b" * 20)
    assert cc._get_draft(9, None, 1)["bg"] == b"b" * 20


def test_total_stays_inside_the_budget_over_many_users(monkeypatch):
    monkeypatch.setattr(cc, "_DRAFT_BYTES_MAX", 1000)
    for u in range(60):
        cc._set_draft_bg(9, None, u, b"x" * 90)
        assert cc._draft_bytes_total() <= 1000


# ---------- idle picture expiry ----------

def test_idle_preview_picture_goes_sooner_than_the_look(monkeypatch):
    d = cc._get_draft(9, None, 1, create=True)
    d["opts"]["font"] = "gothic"
    cc._set_draft_bg(9, None, 1, b"x" * 50)
    real = cc.time.monotonic()
    monkeypatch.setattr(cc.time, "monotonic", lambda: real + cc._DRAFT_PIC_TTL + 5)
    d2 = cc._get_draft(9, None, 2, create=True)      # any call runs the cleanup
    assert cc._DRAFTS[(9, None, 1)]["bg"] is None and cc._DRAFTS[(9, None, 1)]["opts"]["font"] == "gothic"
    assert d2 is not None


def test_an_active_editor_keeps_its_picture(monkeypatch):
    cc._set_draft_bg(9, None, 1, b"x" * 50)
    real = cc.time.monotonic()
    t = {"now": real}
    monkeypatch.setattr(cc.time, "monotonic", lambda: t["now"])
    for _ in range(4):                                # touches the draft every 5 minutes (renders do this)
        t["now"] += 300
        cc._get_draft(9, None, 1)
    assert cc._get_draft(9, None, 1)["bg"] == b"x" * 50


# ---------- undo ----------

def test_free_servers_keep_fewer_undo_steps_than_premium():
    free = {"ultra_pack_unlocked": False, "ultra_card_json": None, "avatar_shape": "circle"}
    paid = {"ultra_pack_unlocked": True, "ultra_card_json": None, "avatar_shape": "circle"}
    for _ in range(20):
        cc._push_undo(1, None, 42, free)
        cc._push_undo(2, None, 42, paid)
    assert len(cc._UNDO[(1, None, 42)]) == cc._FREE_UNDO_MAX == 5
    assert len(cc._UNDO[(2, None, 42)]) == cc._UNDO_MAX == 10


# ---------- every free picture path goes through the caps ----------

def test_no_code_writes_a_draft_picture_without_the_budget_helper():
    src = open(cc.__file__, encoding="utf-8").read()
    assert len(re.findall(r'\["bg"\]\s*=\s*small', src)) == 1     # only inside _set_draft_bg itself
    assert src.count("_set_draft_bg(self.guild_id") == 3
    assert src.count("_preview_picture_quota(self.guild_id") == 3     # each of the three free picture paths asks first


def test_the_editor_is_not_blocked_when_the_picture_quota_is_used_up():
    """Looks (options, shape) never touch the picture quota."""
    for _ in range(cc._PIC_PREVIEWS_PER_HOUR + 3):
        cc._preview_picture_quota(1, None, 42)
    d = cc._get_draft(1, None, 42, create=True)
    d["opts"]["font"] = "gothic"
    assert cc._get_draft(1, None, 42)["opts"]["font"] == "gothic"
