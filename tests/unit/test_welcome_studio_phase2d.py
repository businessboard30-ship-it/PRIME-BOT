"""Welcome Card Studio Phase 2d: Add pictures (multi-upload), downscale, caption, daily limit, migrate old picture."""
import asyncio
import io
import re
from pathlib import Path
from types import SimpleNamespace
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


def img_bytes(w, h, fmt="PNG", mode="RGB"):
    b = io.BytesIO()
    Image.new(mode, (w, h), (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(b, format=fmt)
    return b.getvalue()


# ---------- prepare_picture ----------

def test_wide_picture_is_downscaled_to_card_width_as_jpeg():
    out = wg.prepare_picture(img_bytes(4000, 2000))
    im = Image.open(io.BytesIO(out))
    assert im.format == "JPEG" and im.size[0] == wg.CARD_WIDTH and im.size[1] == 768


def test_small_picture_is_not_upscaled():
    im = Image.open(io.BytesIO(wg.prepare_picture(img_bytes(400, 300))))
    assert im.size == (400, 300)


def test_transparent_png_and_garbage():
    assert wg.prepare_picture(img_bytes(100, 100, mode="RGBA")) is not None
    assert wg.prepare_picture(b"not an image") is None
    assert wg.prepare_picture(b"") is None


def test_huge_pixel_count_is_refused_without_decoding(monkeypatch):
    monkeypatch.setattr(wg, "MAX_SOURCE_PIXELS", 1000)
    assert wg.prepare_picture(img_bytes(100, 100)) is None


# ---------- daily limit ----------

def test_daily_limit_counts_memory_and_db_and_expires():
    wg._RECENT.clear()
    assert wg.uploads_left(1, None) == wg.DAILY_UPLOAD_LIMIT
    for _ in range(5):
        wg.note_upload(1, None, now=1000.0)
    assert wg.uploads_left(1, None, now=1001.0) == wg.DAILY_UPLOAD_LIMIT - 5
    assert wg.uploads_left(1, None, db_recent=12, now=1001.0) == wg.DAILY_UPLOAD_LIMIT - 12   # larger count wins
    assert wg.uploads_left(1, None, now=1000.0 + 86401) == wg.DAILY_UPLOAD_LIMIT              # a day later
    assert wg.uploads_left(2, None) == wg.DAILY_UPLOAD_LIMIT                                  # per server
    assert wg.uploads_left(1, 7, now=1001.0) == wg.DAILY_UPLOAD_LIMIT                         # per clone
    assert wg.uploads_left(1, None, db_recent=999) == 0


# ---------- the Add button ----------

def test_add_button_in_more_mode_only_and_for_every_server():
    unlocked0 = {"unlocked": True, "pictures": 0, "needs": [], "slots": [], "mode": "random"}
    free = {"unlocked": False, "mode": "random"}
    for st in (unlocked0, free, {"unlocked": True, "pictures": 2, "needs": [], "slots": [1, 2], "mode": "random"}):
        assert "picadd" in kinds(build(st))
        assert "picadd" not in kinds(build(st, more=False))


def test_empty_premium_gallery_shows_add_but_not_remove_or_first():
    k = kinds(build({"unlocked": True, "pictures": 0, "needs": [], "slots": [], "mode": "random"}))
    assert "picadd" in k and not {"picremove", "picfirst"} & k


def test_more_mode_stays_inside_discord_limits():
    v = build({"unlocked": True, "pictures": 3, "needs": [], "slots": [1, 2, 3], "mode": "random"})
    assert len(comps(v)) < 40
    for row in v.walk_children():
        if isinstance(row, discord.ui.ActionRow):
            assert len(row.children) <= 5


def test_add_button_registered_and_modal_takes_ten_files():
    assert cc.CardPictureAddButton in cc.DYNAMIC_ITEMS
    m = cc.CardPicturesAddModal(1, None, 42)
    assert m.upload.max_values == wg.MAX_PER_UPLOAD == 10


# ---------- _add_pictures ----------

class FakeDB:
    def __init__(self, rows=None, recent=0):
        self.rows = list(rows or [])
        self.recent = recent
        self.migrated = 0
        self.added = []

    async def gallery_migrate_single(self, g, c):
        self.migrated += 1
        return False

    async def gallery_count_recent(self, g, c, hours=24):
        return self.recent

    async def gallery_list(self, g, c=None):
        return [{"slot": s} for s in self.rows]

    async def gallery_add(self, g, c, ch, msg, url, label=None):
        used = set(self.rows)
        if len(used) >= 10:
            return None
        slot = next(n for n in range(1, 11) if n not in used)
        self.rows.append(slot)
        self.added.append((slot, ch, msg, label))
        return slot


def patch(monkeypatch, fake, host):
    monkeypatch.setattr(cc, "db", fake)
    import modules.image_host as ih
    monkeypatch.setattr(ih, "post_to_host", host)
    wg._RECENT.clear()


def test_adds_prepared_pictures_with_caption_and_migrates_first(monkeypatch):
    fake = FakeDB()
    posted = []

    async def host(bot, data, label):
        posted.append((data, label))
        return 11, 100 + len(posted), "https://cdn/x"

    patch(monkeypatch, fake, host)
    guild = SimpleNamespace(name="My Server")
    added, skipped = run(cc._add_pictures(MagicMock(), guild, 5, None,
                                          [("a.png", img_bytes(3000, 1500), None), ("b.jpg", img_bytes(800, 800, "JPEG"), None)]))
    assert added == [1, 2] and skipped == []
    assert fake.migrated == 1
    assert "Studio picture 1" in posted[0][1] and "`5`" in posted[0][1] and "My Server" in posted[0][1]
    assert "Studio picture 2" in posted[1][1]
    assert Image.open(io.BytesIO(posted[0][0])).size[0] == wg.CARD_WIDTH       # downscaled before hosting


def test_full_gallery_skips_the_rest(monkeypatch):
    fake = FakeDB(rows=list(range(1, 10)))
    host = AsyncMock(return_value=(11, 1, None))
    patch(monkeypatch, fake, host)
    added, skipped = run(cc._add_pictures(MagicMock(), None, 5, None, [(f"{i}.png", img_bytes(50, 50), None) for i in range(3)]))
    assert added == [10] and len(skipped) == 2 and "full" in skipped[0][1]


def test_daily_limit_stops_uploads(monkeypatch):
    fake = FakeDB(recent=wg.DAILY_UPLOAD_LIMIT - 1)
    host = AsyncMock(return_value=(11, 1, None))
    patch(monkeypatch, fake, host)
    added, skipped = run(cc._add_pictures(MagicMock(), None, 5, None, [(f"{i}.png", img_bytes(50, 50), None) for i in range(3)]))
    assert len(added) == 1 and len(skipped) == 2 and "daily limit" in skipped[0][1]


def test_bad_files_are_named_and_good_ones_still_added(monkeypatch):
    fake = FakeDB()
    host = AsyncMock(return_value=(11, 1, None))
    patch(monkeypatch, fake, host)
    added, skipped = run(cc._add_pictures(MagicMock(), None, 5, None, [
        ("doc.pdf", None, "not a png/jpg"), ("junk.png", b"junk", None), ("ok.png", img_bytes(50, 50), None)]))
    assert added == [1]
    assert dict(skipped)["doc.pdf"] == "not a png/jpg" and dict(skipped)["junk.png"] == "not a readable image"


def test_hosting_down_stops_and_stores_nothing(monkeypatch):
    fake = FakeDB()
    patch(monkeypatch, fake, AsyncMock(return_value=None))
    added, skipped = run(cc._add_pictures(MagicMock(), None, 5, None, [("a.png", img_bytes(50, 50), None), ("b.png", img_bytes(50, 50), None)]))
    assert added == [] and fake.added == [] and len(skipped) == 1 and "hosting" in skipped[0][1]


def test_raced_full_deletes_the_orphan_and_never_raises(monkeypatch):
    class RaceDB(FakeDB):
        async def gallery_list(self, g, c=None):
            return []                       # looked free, but the insert says full
        async def gallery_add(self, *a, **k):
            return None
    patch(monkeypatch, RaceDB(), AsyncMock(return_value=(11, 77, None)))
    deleted = AsyncMock()
    bot = MagicMock()
    bot.get_channel.return_value.get_partial_message.return_value.delete = deleted
    added, skipped = run(cc._add_pictures(bot, None, 5, None, [("a.png", img_bytes(50, 50), None)]))
    assert added == [] and skipped and deleted.await_count == 1


def test_unexpected_error_is_reported_not_raised(monkeypatch):
    class BrokenDB(FakeDB):
        async def gallery_migrate_single(self, g, c):
            raise RuntimeError("boom")
    patch(monkeypatch, BrokenDB(), AsyncMock())
    added, skipped = run(cc._add_pictures(MagicMock(), None, 5, None, [("a.png", img_bytes(50, 50), None)]))
    assert added == [] and skipped


# ---------- migration + wiring ----------

def test_migration_file_is_additive_and_registered():
    sql = (ROOT / "database/migrations/079_welcome_gallery_migrated.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS gallery_migrated" in sql
    assert not re.search(r"\bDROP\b|\bDELETE\b|\bTRUNCATE\b", sql, re.I)
    assert "079_welcome_gallery_migrated.sql" in (ROOT / "database.py").read_text()


def test_migrate_method_never_touches_old_columns_and_is_once():
    src = (ROOT / "database.py").read_text()
    body = src[src.index("async def gallery_migrate_single"):src.index("async def gallery_remove")]
    assert "gallery_migrated" in body and "SET gallery_migrated = TRUE" in body
    assert "custom_bg_message_id = NULL" not in body and "custom_background_url = NULL" not in body


def test_join_render_and_renderer_are_untouched_by_2d():
    assert "welcome_gallery" not in (ROOT / "modules/welcome_card.py").read_text()
