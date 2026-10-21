"""Welcome Card Studio Phase 2e: Replace a picture, editor preview uses the gallery, Clear background note."""
import asyncio
import io
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
from PIL import Image

from discord_bot.cogs import _views_card_customize as cc
from modules import welcome_gallery as wg

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def comps(v):
    return list(v.walk_children())


def kinds(v):
    return {re.match(r"cardwz_([a-z]+):", c.custom_id).group(1) for c in comps(v) if getattr(c, "custom_id", None)}


def build(studio, more=True):
    cfg = {"ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png", "_studio": studio}
    return cc.build_customize_view(1, None, 42, cfg, tab="bg", more=more)


def img_bytes(w, h):
    b = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(b, format="PNG")
    return b.getvalue()


# ---------- the Replace button ----------

def test_replace_button_is_registered_and_in_more_mode_only():
    assert cc.CardPictureReplaceButton in cc.DYNAMIC_ITEMS
    st = {"unlocked": True, "pictures": 2, "needs": [], "slots": [1, 2], "mode": "random"}
    assert "picreplace" in kinds(build(st))
    assert "picreplace" not in kinds(build(st, more=False))


def test_replace_shown_to_free_servers_hidden_for_empty_premium_gallery():
    assert "picreplace" in kinds(build({"unlocked": False, "mode": "random"}))
    assert "picreplace" not in kinds(build({"unlocked": True, "pictures": 0, "needs": [], "slots": [], "mode": "random"}))


def test_manage_row_stays_inside_discord_limits():
    v = build({"unlocked": True, "pictures": 3, "needs": [], "slots": [1, 2, 3], "mode": "random"})
    assert len(comps(v)) < 40
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5


def test_replace_modal_takes_one_file():
    m = cc.CardPictureReplaceModal(1, None, 42)
    assert m.upload.max_values == 1


# ---------- _replace_picture ----------

class FakeDB:
    def __init__(self, slots=(1, 2), recent=0, old=(11, 50)):
        self.slots, self.recent, self.old = list(slots), recent, old
        self.replaced = []

    async def gallery_list(self, g, c=None):
        return [{"slot": s} for s in self.slots]

    async def gallery_count_recent(self, g, c, hours=24):
        return self.recent

    async def gallery_replace(self, g, c, slot, ch, msg, url, label=None):
        if slot not in self.slots:
            return None
        self.replaced.append((slot, ch, msg))
        return self.old


def patch(monkeypatch, fake, host):
    monkeypatch.setattr(cc, "db", fake)
    import modules.image_host as ih
    monkeypatch.setattr(ih, "post_to_host", host)
    wg._RECENT.clear()


def bot_with_delete():
    bot = MagicMock()
    deleted = AsyncMock()
    bot.get_channel.return_value.get_partial_message.return_value.delete = deleted
    return bot, deleted


def test_replace_hosts_downscaled_picture_keeps_slot_and_deletes_old(monkeypatch):
    fake = FakeDB()
    posted = []

    async def host(bot, data, label):
        posted.append((data, label))
        return 12, 99, "https://cdn/new"

    patch(monkeypatch, fake, host)
    bot, deleted = bot_with_delete()
    ok, why = run(cc._replace_picture(bot, MagicMock(), 5, None, 2, img_bytes(3000, 1500)))
    assert ok and why == ""
    assert fake.replaced == [(2, 12, 99)]
    assert "Studio picture 2" in posted[0][1] and "`5`" in posted[0][1]
    assert Image.open(io.BytesIO(posted[0][0])).size[0] == wg.CARD_WIDTH
    assert deleted.await_count == 1                      # the old hosted post is cleaned up


def test_replace_empty_slot_is_refused_without_hosting(monkeypatch):
    host = AsyncMock(return_value=(12, 99, None))
    patch(monkeypatch, FakeDB(slots=(1,)), host)
    ok, why = run(cc._replace_picture(MagicMock(), None, 5, None, 4, img_bytes(50, 50)))
    assert not ok and "no picture 4" in why and host.await_count == 0


def test_replace_counts_toward_the_daily_limit(monkeypatch):
    host = AsyncMock(return_value=(12, 99, None))
    patch(monkeypatch, FakeDB(recent=wg.DAILY_UPLOAD_LIMIT), host)
    ok, why = run(cc._replace_picture(MagicMock(), None, 5, None, 1, img_bytes(50, 50)))
    assert not ok and "daily limit" in why and host.await_count == 0


def test_replace_notes_the_upload_in_memory(monkeypatch):
    patch(monkeypatch, FakeDB(), AsyncMock(return_value=(12, 99, None)))
    bot, _ = bot_with_delete()
    before = wg.uploads_left(5, None)
    run(cc._replace_picture(bot, None, 5, None, 1, img_bytes(50, 50)))
    assert wg.uploads_left(5, None) == before - 1


def test_replace_garbage_and_hosting_down(monkeypatch):
    host = AsyncMock(return_value=None)
    patch(monkeypatch, FakeDB(), host)
    ok, why = run(cc._replace_picture(MagicMock(), None, 5, None, 1, b"junk"))
    assert not ok and "readable" in why
    ok, why = run(cc._replace_picture(MagicMock(), None, 5, None, 1, img_bytes(50, 50)))
    assert not ok and "hosting" in why


def test_replace_raced_removed_deletes_the_orphan(monkeypatch):
    class Raced(FakeDB):
        async def gallery_replace(self, *a, **k):
            return None           # slot vanished after hosting
    patch(monkeypatch, Raced(), AsyncMock(return_value=(12, 99, None)))
    bot, deleted = bot_with_delete()
    ok, _why = run(cc._replace_picture(bot, None, 5, None, 1, img_bytes(50, 50)))
    assert not ok and deleted.await_count == 1


def test_replace_never_raises(monkeypatch):
    class Broken(FakeDB):
        async def gallery_list(self, g, c=None):
            raise RuntimeError("boom")
    patch(monkeypatch, Broken(), AsyncMock())
    ok, why = run(cc._replace_picture(MagicMock(), None, 5, None, 1, img_bytes(50, 50)))
    assert not ok and why


def test_old_hosted_delete_failure_does_not_fail_the_replace(monkeypatch):
    patch(monkeypatch, FakeDB(), AsyncMock(return_value=(12, 99, None)))
    bot = MagicMock()
    bot.get_channel.return_value.get_partial_message.return_value.delete = AsyncMock(side_effect=RuntimeError("no perms"))
    ok, _why = run(cc._replace_picture(bot, None, 5, None, 1, img_bytes(50, 50)))
    assert ok


# ---------- editor preview uses the gallery ----------

def preview_patch(monkeypatch, unlocked, rows, slot_url=("https://cdn/a.png", False), fetched=b"PIC"):
    import modules.welcome_studio as ws
    import discord_bot.cogs.welcome as wl
    cc._BG_CACHE.clear()
    monkeypatch.setattr(ws, "studio_unlocked", AsyncMock(return_value=unlocked))
    fake = MagicMock()
    fake.gallery_list = AsyncMock(return_value=rows)
    monkeypatch.setattr(cc, "db", fake)
    monkeypatch.setattr(wl, "_gallery_slot_url", AsyncMock(return_value=slot_url))
    monkeypatch.setattr(wl, "_fetch_custom_bg_bytes", AsyncMock(return_value=(fetched, None)))


def test_preview_uses_first_gallery_picture_for_premium(monkeypatch):
    preview_patch(monkeypatch, True, [{"slot": 1, "message_id": 7}, {"slot": 2, "message_id": 8}])
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) == b"PIC"


def test_preview_skips_flagged_and_gone_pictures(monkeypatch):
    preview_patch(monkeypatch, True, [{"slot": 1, "message_id": 7, "needs_new_picture": True}], slot_url=(None, True))
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) is None
    preview_patch(monkeypatch, True, [{"slot": 1, "message_id": 7}], slot_url=(None, True))
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) is None


def test_preview_falls_back_for_free_empty_gallery_or_errors(monkeypatch):
    preview_patch(monkeypatch, False, [{"slot": 1, "message_id": 7}])
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) is None     # not Premium/Plus
    preview_patch(monkeypatch, True, [])
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) is None     # empty gallery
    preview_patch(monkeypatch, True, [{"slot": 1, "message_id": 7}])
    cc.db.gallery_list = AsyncMock(side_effect=RuntimeError("boom"))
    assert run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None)) is None     # never raises
    assert run(cc._gallery_preview_bytes(MagicMock(), None, 1, None)) is None            # no bot


def test_preview_caches_the_picture(monkeypatch):
    preview_patch(monkeypatch, True, [{"slot": 1, "message_id": 7}])
    import discord_bot.cogs.welcome as wl
    run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None))
    run(cc._gallery_preview_bytes(MagicMock(), MagicMock(), 1, None))
    assert wl._fetch_custom_bg_bytes.await_count == 1


# ---------- wiring ----------

def test_migration_free_and_renderer_untouched():
    assert "welcome_gallery" not in (ROOT / "modules/welcome_card.py").read_text()
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def gallery_replace"):src.index("async def gallery_make_first")]
    assert "DELETE" not in body and "needs_new_picture = FALSE" in body


def test_clear_background_mentions_gallery_and_never_deletes_it():
    src = (ROOT / "discord_bot/cogs/_views_card_customize.py").read_text()
    body = src[src.index("class CardClearBackgroundButton"):src.index("class CardUnlockButton")]
    assert "gallery_list" in body and "gallery_remove" not in body
