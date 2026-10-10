"""Scam Shield: matching, the message listener, the panel screen and the migration."""
import asyncio
import io
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from modules import scam_shield as ss

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


@pytest.fixture(autouse=True)
def rules(monkeypatch):
    ss._c.words = [(1, "fatowin")]
    ss._c.domains = [(2, "fatowin.com")]
    ss._c.images = []
    ss._c.enabled = True
    ss._c.loaded_at = 10 ** 12          # never "stale" during a test
    ss._last_hit.clear()
    monkeypatch.setattr(ss.time, "monotonic", lambda: 10 ** 12)
    yield


# ── text matching ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "go to FatoWin and use code GIFT",
    "fato win . com  free $3500",
    "F.A.T.O.W.I.N",
    "claim at https://www.fatowin.com/bonus",
    "http://promo.fatowin.com",
    "f\u200ba\u200bt\u200bo\u200bwin",
])
def test_scam_text_is_caught(text):
    assert ss.match_text(text) is not None


def test_mrbeast_casino_bait_is_caught_without_any_stored_rule():
    ss._c.words, ss._c.domains = [], []
    hit = ss.match_text("MrBeast launched his own crypto casino, promo code GIFT")
    assert hit and hit[0] == "heuristic"


@pytest.mark.parametrize("text", [
    "my brother loves mrbeast videos",
    "did you see the new casino royale film",
    "the withdrawal from the bank went through",
    "check https://example.com/fato",
    "",
])
def test_normal_chat_is_not_caught(text):
    ss._c.words, ss._c.domains = [(1, "fatowin")], [(2, "fatowin.com")]
    assert ss.match_text(text) is None


def test_lookalike_domain_is_not_a_subdomain_match():
    ss._c.words = []
    assert ss.match_text("https://notfatowin.com") is None
    assert ss.match_text("https://a.b.fatowin.com") is not None


def test_classify_and_parse_domain():
    assert ss.classify("https://www.FatoWin.com/x?y=1") == ("domain", "fatowin.com")
    assert ss.classify("free nitro gift") == ("word", "free nitro gift")
    assert ss.classify("hello.") [0] == "word"


# ── images ───────────────────────────────────────────────────────────────

def _img(size=(300, 200), seed=0, fmt="PNG", quality=None):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, (20 + seed, 40, 90))
    d = ImageDraw.Draw(im)
    for n in range(0, size[0], 25):
        d.rectangle([n, (n * 7 + seed * 13) % size[1], n + 18, size[1]], fill=(200 - n % 120, 60 + seed, 10 + n % 200))
    d.ellipse([size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2], fill=(250, 250, 250))
    buf = io.BytesIO()
    im.save(buf, fmt, **({"quality": quality} if quality else {}))
    return buf.getvalue()


def test_known_image_matches_even_when_resized_and_recompressed_but_other_images_do_not():
    original = _img()
    ss._c.images = [(9, ss.dhash(original))]
    assert ss.match_image(original)[0] == "image"
    from PIL import Image
    small = io.BytesIO()
    Image.open(io.BytesIO(original)).resize((150, 100)).convert("RGB").save(small, "JPEG", quality=40)
    assert ss.match_image(small.getvalue()) is not None
    other = _img(size=(300, 200), seed=50)          # a genuinely different layout (seed 200 is just a recolour)
    assert ss.hamming(ss.dhash(original), ss.dhash(other)) > ss.IMAGE_MAX_DISTANCE
    assert ss.match_image(other) is None


def test_unreadable_bytes_never_match_or_crash():
    ss._c.images = [(9, 123)]
    assert ss.match_image(b"not an image") is None


def test_seeded_hashes_in_migration_are_valid_64bit_hex():
    sql = (ROOT / "database/migrations/029_scam_shield.sql").read_text()
    hashes = re.findall(r"'image',\s*'([0-9a-f]+)'", sql)
    assert len(hashes) == 4 and all(len(h) == 16 and int(h, 16) >= 0 for h in hashes)


# ── the listener ─────────────────────────────────────────────────────────

def _msg(content="", staff=False, attachments=(), webhook=False):
    from discord_bot.cogs import scam_shield as cog_mod
    guild = SimpleNamespace(id=555, owner_id=1, get_channel=lambda cid: None)
    if webhook:
        author = SimpleNamespace(id=42, __str__=lambda s: "hook")
    else:
        perms = SimpleNamespace(administrator=False, manage_guild=False, manage_messages=staff)
        author = MagicMock(spec=discord.Member)
        author.id, author.guild_permissions = 42, perms
    m = MagicMock()
    m.guild, m.author, m.content, m.embeds, m.attachments = guild, author, content, [], list(attachments)
    m.type = discord.MessageType.default
    m.channel = SimpleNamespace(id=7, mention="#c")
    m.delete = AsyncMock()
    return m


@pytest.fixture()
def cog(monkeypatch):
    from discord_bot.cogs import scam_shield as cog_mod
    bot = MagicMock()
    bot.user = SimpleNamespace(id=999)
    c = cog_mod.ScamShieldCog.__new__(cog_mod.ScamShieldCog)
    c.bot = bot
    log = AsyncMock()
    monkeypatch.setattr(cog_mod.ss, "log_hit", log)
    monkeypatch.setattr(cog_mod.ss, "load", AsyncMock())
    monkeypatch.setattr(c, "_flag", AsyncMock(), raising=False)
    c.log = log
    return c


def test_scam_message_is_deleted_and_flagged(cog):
    m = _msg("free $3500 at fatowin.com use GIFT")
    run(cog._inspect(m))
    m.delete.assert_awaited_once()
    cog.log.assert_awaited_once()
    args = cog.log.await_args.args
    assert args[:3] == (555, 7, 42) and args[7] is True          # guild, channel, user ... deleted=True
    cog._flag.assert_awaited_once()


def test_normal_message_untouched(cog):
    m = _msg("anyone up for a game tonight?")
    run(cog._inspect(m))
    m.delete.assert_not_awaited()
    cog.log.assert_not_awaited()


def test_staff_are_never_checked(cog):
    m = _msg("fatowin.com", staff=True)
    run(cog._inspect(m))
    m.delete.assert_not_awaited()


def test_webhook_posts_get_no_free_pass(cog):
    m = _msg("fatowin.com", webhook=True)
    run(cog._inspect(m))
    m.delete.assert_awaited_once()


def test_switched_off_does_nothing(cog):
    ss._c.enabled = False
    m = _msg("fatowin.com")
    run(cog._inspect(m))
    m.delete.assert_not_awaited()


def test_dms_and_own_messages_are_ignored(cog):
    m = _msg("fatowin.com")
    m.guild = None
    run(cog._inspect(m))
    m.delete.assert_not_awaited()
    m2 = _msg("fatowin.com")
    m2.author.id = 999
    run(cog._inspect(m2))
    m2.delete.assert_not_awaited()


def test_raid_flood_deletes_every_message_but_logs_once(cog):
    msgs = [_msg("fatowin.com") for _ in range(5)]
    for m in msgs:
        run(cog._inspect(m))
    assert all(m.delete.await_count == 1 for m in msgs)
    assert cog.log.await_count == 1


def test_failed_delete_is_still_flagged_as_not_deleted(cog):
    m = _msg("fatowin.com")
    m.delete.side_effect = discord.HTTPException(MagicMock(status=403, reason="x"), "no perms")
    run(cog._inspect(m))
    assert cog.log.await_args.args[7] is False                    # recorded as NOT deleted


def test_images_are_not_downloaded_when_there_are_no_image_rules(cog):
    att = MagicMock(content_type="image/png", filename="a.png", size=1000)
    att.read = AsyncMock(return_value=_img())
    m = _msg("hello", attachments=[att])
    run(cog._inspect(m))
    att.read.assert_not_awaited()


def test_known_scam_image_is_caught_and_big_files_are_skipped(cog):
    data = _img()
    ss._c.images = [(9, ss.dhash(data))]
    att = MagicMock(content_type="image/png", filename="x.png", size=1000)
    att.read = AsyncMock(return_value=data)
    m = _msg("look", attachments=[att])
    run(cog._inspect(m))
    m.delete.assert_awaited_once()
    big = MagicMock(content_type="image/png", filename="x.png", size=ss.IMAGE_MAX_BYTES + 1)
    big.read = AsyncMock(return_value=data)
    m2 = _msg("look", attachments=[big])
    m2.author.id = 43
    run(cog._inspect(m2))
    big.read.assert_not_awaited()
    m2.delete.assert_not_awaited()


# ── migration / wiring ───────────────────────────────────────────────────

def test_migration_is_additive_idempotent_and_schema_is_bumped():
    sql = (ROOT / "database/migrations/029_scam_shield.sql").read_text()
    code = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--")).upper()
    assert "DROP " not in code and "DELETE " not in code and "TRUNCATE" not in code
    assert code.count("CREATE TABLE") == code.count("CREATE TABLE IF NOT EXISTS") == 3
    assert "ON CONFLICT (KIND, PATTERN) DO NOTHING" in code
    src = (ROOT / "database.py").read_text()
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)) >= 51
    assert "029_scam_shield.sql" in src
    assert 'discord_bot.cogs.scam_shield' in (ROOT / "discord_bot/bot.py").read_text()


# ── the /admin screen ────────────────────────────────────────────────────

def _panel(monkeypatch, rules=None, hits=None, enabled=True):
    from discord_bot.cogs import _views_admin_panel_scamshield as pv
    from datetime import datetime, timezone
    monkeypatch.setattr(pv.ss, "load", AsyncMock())
    monkeypatch.setattr(pv.ss, "is_enabled", lambda: enabled)
    monkeypatch.setattr(pv.ss, "list_rules", AsyncMock(return_value=rules if rules is not None else [
        dict(id=1, kind="word", pattern="fatowin", note="scam", created_at=None),
        dict(id=4, kind="image", pattern="3734283e39391d31", note="shot #1", created_at=None)]))
    monkeypatch.setattr(pv.ss, "recent_hits", AsyncMock(return_value=hits if hits is not None else [
        dict(guild_id=5, user_id=6, kind="word", matched="fatowin", deleted=True,
             created_at=datetime.now(timezone.utc))]))
    monkeypatch.setattr(pv.ss, "hit_total", AsyncMock(return_value=3))
    v = pv.ScamShieldView(MagicMock(), 1)
    run(v.load())
    return pv, v


def _text(v):
    return "\n".join(c.content for c in v.walk_children() if isinstance(c, discord.ui.TextDisplay))


def test_panel_shows_status_rules_and_catches_in_one_code_block_each(monkeypatch):
    pv, v = _panel(monkeypatch)
    t = _text(v)
    assert "ON" in t and "every server" in t and "#1 word" in t and "image 3734283e" in t and "Caught so far: **3**" in t
    assert t.count("```") == 4 and "<t:" not in t
    assert "NOT DELETED" not in t


def test_panel_flags_catches_that_could_not_be_deleted_and_handles_empty(monkeypatch):
    from datetime import datetime, timezone
    pv, v = _panel(monkeypatch, hits=[dict(guild_id=5, user_id=6, kind="image", matched="x", deleted=False,
                                           created_at=datetime.now(timezone.utc))])
    assert "NOT DELETED" in _text(v)
    pv, v = _panel(monkeypatch, rules=[], hits=[])
    assert "none yet" in _text(v)
    assert next(c for c in v.walk_children() if getattr(c, "label", None) == "Remove rule").disabled


def test_panel_toggle_flips_the_global_switch_and_is_audited(monkeypatch):
    pv, v = _panel(monkeypatch, enabled=True)
    set_enabled = AsyncMock()
    monkeypatch.setattr(pv.ss, "set_enabled", set_enabled)
    monkeypatch.setattr(pv, "audit", MagicMock())
    i = MagicMock()
    i.response.edit_message = AsyncMock()
    run(v._toggle(i))
    set_enabled.assert_awaited_once_with(False)
    pv.audit.assert_called_once()


def test_add_modal_refuses_tiny_words_and_unauthorised_users(monkeypatch):
    pv, v = _panel(monkeypatch)
    add = AsyncMock(return_value=7)
    monkeypatch.setattr(pv.ss, "add_rule", add)
    monkeypatch.setattr(pv, "audit", MagicMock())
    monkeypatch.setattr(pv, "allowed_sections", lambda uid: {pv.SECTION})
    m = pv.AddRuleModal(v)
    i = MagicMock()
    i.user.id = 1
    i.response.edit_message = AsyncMock()
    m.text._value = "hi"
    run(m.on_submit(i))
    add.assert_not_awaited()
    m.text._value = "https://Free-Nitro.example/claim"
    run(m.on_submit(i))
    add.assert_awaited_once_with("domain", "free-nitro.example", 1)
    monkeypatch.setattr(pv, "allowed_sections", lambda uid: set())
    pv._denied = AsyncMock()
    add.reset_mock()
    run(m.on_submit(i))
    add.assert_not_awaited()


def test_servers_hub_has_the_scam_shield_button_and_rows_still_fit(monkeypatch):
    from discord_bot.cogs import _views_admin_panel_servers as sv
    monkeypatch.setattr(sv, "allowed_sections", lambda uid: {"servers", "scamshield"})
    hub = sv.ServersHubView(MagicMock(), 1)
    labels = [getattr(c, "label", None) for c in hub.walk_children()]
    assert "Scam Shield" in labels
    for row in (c for c in hub.walk_children() if isinstance(c, discord.ui.ActionRow)):
        assert len(row.children) <= 5
    hub.to_components()


# ── fatawin variant (2026-10) ────────────────────────────────────────────

FAKE_POST = ("I am pleased to announce the launch of my own cryptocurrency casino! I am giving away $5,600 "
             "to everyone who registers. Enter the special promo code: BET. This post will be deleted an hour "
             "after publication so that only the fastest people will find out about the bonus!")


def test_fatawin_rules_cover_the_new_spelling():
    ss._c.words = [(1, "fatawin")]
    ss._c.domains = [(2, "fatawin.com")]
    for t in ("go to fatawin.com and enter BET", "Fata Win", "https://www.fatawin.com/profile/bonuses"):
        assert ss.match_text(t) is not None, t


def test_fake_giveaway_post_caught_by_heuristic_even_with_no_rules():
    ss._c.words, ss._c.domains = [], []
    assert ss.match_text(FAKE_POST)[0] == "heuristic"
    assert ss.match_text("giving away $5,600 to everyone who registers, promo code BET")[0] == "heuristic"
    assert ss.match_text("promo code BET - this post will be deleted an hour after publication")[0] == "heuristic"


@pytest.mark.parametrize("text", [
    "our giveaway ends friday, enter in #giveaways",
    "use promo code SAVE10 at checkout",
    "we are giving away a nitro, react to enter",
    "i will withdraw my application, thanks",
    "the casino scene in that movie was great",
])
def test_ordinary_chat_is_not_flagged_by_the_new_heuristics(text):
    ss._c.words, ss._c.domains = [], []
    assert ss.match_text(text) is None


def test_seed_inserts_once_and_leaves_removed_rules_removed():
    calls = []

    class Conn:
        def __init__(self, marked):
            self.marked = marked

        async def fetchval(self, q, *a):
            return 1 if self.marked else None

        async def execute(self, q, *a):
            calls.append(a)
    run(ss._seed_defaults(Conn(True)))
    assert calls == []
    run(ss._seed_defaults(Conn(False)))
    kinds = [c[0] for c in calls if len(c) == 3]
    assert kinds.count("image") == 8                         # four per batch, both batches on a fresh database
    assert ("domain", "fatawin.com", ss.SEED_NOTE) in [c for c in calls if len(c) == 3]
    assert ("domain", "dwinble.com", ss.SEED_DWINBLE_NOTE) in [c for c in calls if len(c) == 3]
    assert any(c == (ss.SEED_KEY,) for c in calls) and any(c == (ss.SEED_DWINBLE_KEY,) for c in calls)


def test_a_database_that_already_ran_the_first_batch_still_gets_the_dwinble_batch_once():
    calls = []

    class Conn:
        async def fetchval(self, q, *a):
            return 1 if a == (ss.SEED_KEY,) else None        # only the first marker exists

        async def execute(self, q, *a):
            calls.append(a)
    run(ss._seed_defaults(Conn()))
    inserted = [c for c in calls if len(c) == 3]
    assert {c[2] for c in inserted} == {ss.SEED_DWINBLE_NOTE}          # nothing from the first batch is re-added
    assert len(inserted) == len(ss.SEED_DWINBLE_RULES)
    assert any(c == (ss.SEED_DWINBLE_KEY,) for c in calls) and not any(c == (ss.SEED_KEY,) for c in calls)


def test_dwinble_image_hashes_are_valid_distinct_and_not_near_flat_images():
    existing = [0x3734283e39391d31, 0x5ebe31a7a925a525, 0x1f2024a826232323, 0x2cb08ea3e363b070]
    existing += [int(p, 16) for k, p in ss.SEED_RULES if k == "image"]
    new = [int(p, 16) for k, p in ss.SEED_DWINBLE_RULES if k == "image"]
    assert len(new) == 4 and all(len(p) == 16 for k, p in ss.SEED_DWINBLE_RULES if k == "image")
    for i, h in enumerate(new):
        assert all(ss.hamming(h, o) > ss.IMAGE_MAX_DISTANCE for o in existing)
        assert all(ss.hamming(h, o) > ss.IMAGE_MAX_DISTANCE for o in new[:i])
        assert ss.hamming(h, 0) > 2 * ss.IMAGE_MAX_DISTANCE         # a blank / dark screenshot stays far from a match


@pytest.mark.parametrize("text", [
    "register at dwinble.com and use promo code DRAKE",
    "https://www.dwinble.com/vi/promo",
    "http://bonus.dwinble.com",
    "d w i n b l e . c o m gives $3200",
    "D.W.I.N.B.L.E",
])
def test_dwinble_text_is_caught_by_the_seeded_rules(text):
    ss._c.words = [(1, "dwinble")]
    ss._c.domains = [(2, "dwinble.com")]
    assert ss.match_text(text) is not None


def test_dwinble_rules_do_not_touch_ordinary_chat():
    ss._c.words = [(1, "dwinble")]
    ss._c.domains = [(2, "dwinble.com")]
    for text in ["drake dropped a new album", "check https://example.com/dwin", "winnable game, bleh"]:
        assert ss.match_text(text) is None


def test_the_sweep_marker_is_per_bot():
    assert ss.backfill_marker(None) == ss.BACKFILL_KEY + ":main"
    assert ss.backfill_marker(12) == ss.BACKFILL_KEY + ":12"
    assert ss.backfill_marker(12) != ss.backfill_marker(None)


def test_seed_image_hashes_are_valid_and_new():
    old = [0x3734283e39391d31, 0x5ebe31a7a925a525, 0x1f2024a826232323, 0x2cb08ea3e363b070]
    for kind, pat in ss.SEED_RULES:
        if kind == "image":
            h = int(pat, 16)
            assert len(pat) == 16 and all(ss.hamming(h, o) > ss.IMAGE_MAX_DISTANCE for o in old)


# ── caught scams are copied to the image-hosting channel ─────────────────

def _att(name="pic.png", data=b"IMG", size=3):
    a = MagicMock()
    a.filename, a.size, a.content_type = name, size, "image/png"
    a.read = AsyncMock(return_value=data)
    return a


def test_caught_scam_is_archived_to_the_hosting_channel_before_the_delete(cog, monkeypatch):
    from discord_bot.cogs import scam_shield as cog_mod
    host = MagicMock(); host.send = AsyncMock()
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=host))
    order = []
    att = _att()
    att.read = AsyncMock(side_effect=lambda: order.append("read") or b"IMG")
    m = _msg("free $3500 at fatowin.com use GIFT", attachments=[att])
    m.delete = AsyncMock(side_effect=lambda: order.append("delete"))
    run(cog._inspect(m))
    assert order == ["read", "delete"]                      # evidence is read before Discord drops the file
    host.send.assert_awaited_once()
    kw = host.send.await_args.kwargs
    assert "fatowin" in kw["content"] and "555" in kw["content"] and "42" in kw["content"] and "deleted" in kw["content"]
    assert len(kw["files"]) == 1


def test_no_hosting_channel_means_no_download_and_the_delete_still_happens(cog, monkeypatch):
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=None))
    att = _att()
    m = _msg("free $3500 at fatowin.com use GIFT", attachments=[att])
    run(cog._inspect(m))
    att.read.assert_not_awaited()
    m.delete.assert_awaited_once()


def test_a_failing_archive_never_blocks_the_delete_or_the_log(cog, monkeypatch):
    host = MagicMock(); host.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=host))
    m = _msg("free $3500 at fatowin.com use GIFT")
    run(cog._inspect(m))
    m.delete.assert_awaited_once()
    cog.log.assert_awaited_once()


def test_oversized_attachments_are_skipped(cog, monkeypatch):
    from discord_bot.cogs import scam_shield as cog_mod
    host = MagicMock(); host.send = AsyncMock()
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=host))
    big = _att(size=cog_mod.EVIDENCE_MAX_BYTES + 1)
    run(cog._inspect(_msg("free $3500 at fatowin.com use GIFT", attachments=[big])))
    big.read.assert_not_awaited()
    assert host.send.await_args.kwargs["files"] == []


# ── one-time history sweep ───────────────────────────────────────────────

def _channel(messages, can_read=True):
    async def gen():
        for m in messages:
            yield m
    seen = {}

    def history(limit=None, after=None):
        seen["limit"], seen["after"] = limit, after
        return gen()
    perms = SimpleNamespace(view_channel=can_read, read_message_history=can_read)
    ch = SimpleNamespace(id=7, permissions_for=lambda me: perms, history=history, seen=seen)
    return ch


def _sweep_cog(cog, monkeypatch, channels, enabled=True, done=False, seeded=True, clone_id=None):
    from discord_bot.cogs import scam_shield as cog_mod
    guild = SimpleNamespace(id=555, me=object(), text_channels=channels)
    cog.bot.guilds = [guild]
    cog.bot.is_closed = lambda: False
    cog.bot.clone_id = clone_id
    monkeypatch.setattr(ss, "BACKFILL_CHANNEL_PAUSE", 0)
    monkeypatch.setattr(ss, "backfill_done", AsyncMock(return_value=done))
    monkeypatch.setattr(ss, "seed_done", AsyncMock(return_value=seeded))
    monkeypatch.setattr(ss, "mark_backfill_done", AsyncMock())
    monkeypatch.setattr(ss, "guild_settings", AsyncMock(return_value={"enabled": enabled, "allowed_domains": ()}))
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=None))
    return guild


def test_sweep_removes_old_scam_copies_and_then_marks_itself_done(cog, monkeypatch):
    bad = _msg("free $3500 at fatowin.com use GIFT")
    good = _msg("anyone up for a game tonight?")
    ch = _channel([bad, good])
    _sweep_cog(cog, monkeypatch, [ch])
    run(cog._sweep_history())
    bad.delete.assert_awaited_once()
    good.delete.assert_not_awaited()
    ss.mark_backfill_done.assert_awaited_once_with(None)
    assert ch.seen["limit"] == ss.BACKFILL_PER_CHANNEL and ch.seen["after"] is not None


def test_sweep_catches_a_known_scam_image_posted_before_the_rules_existed(cog, monkeypatch):
    ref = _img(size=(300, 200), seed=7)
    ss._c.images = [(9, ss.dhash(ref))]
    att = _att(name="pic.png", data=ref, size=len(ref))
    bad = _msg("", attachments=[att])
    _sweep_cog(cog, monkeypatch, [_channel([bad])])
    run(cog._sweep_history())
    bad.delete.assert_awaited_once()


def test_sweep_runs_only_once(cog, monkeypatch):
    ch = _channel([_msg("free $3500 at fatowin.com use GIFT")])
    _sweep_cog(cog, monkeypatch, [ch], done=True)
    run(cog._sweep_history())
    assert ch.seen == {}                                       # history was never even requested
    ss.mark_backfill_done.assert_not_awaited()


def test_sweep_waits_until_the_new_rules_are_stored(cog, monkeypatch):
    ch = _channel([_msg("free $3500 at fatowin.com use GIFT")])
    _sweep_cog(cog, monkeypatch, [ch], seeded=False)
    run(cog._sweep_history())
    assert ch.seen == {}
    ss.mark_backfill_done.assert_not_awaited()                 # so it tries again on the next start


def test_sweep_skips_servers_that_switched_scam_shield_off(cog, monkeypatch):
    bad = _msg("free $3500 at fatowin.com use GIFT")
    ch = _channel([bad])
    _sweep_cog(cog, monkeypatch, [ch], enabled=False)
    run(cog._sweep_history())
    assert ch.seen == {}
    bad.delete.assert_not_awaited()
    ss.mark_backfill_done.assert_awaited_once()                # nothing to do there still counts as finished


def test_sweep_skips_channels_the_bot_cannot_read(cog, monkeypatch):
    bad = _msg("free $3500 at fatowin.com use GIFT")
    ch = _channel([bad], can_read=False)
    _sweep_cog(cog, monkeypatch, [ch])
    run(cog._sweep_history())
    assert ch.seen == {}
    bad.delete.assert_not_awaited()


def test_sweep_never_touches_staff_messages(cog, monkeypatch):
    staff = _msg("free $3500 at fatowin.com use GIFT", staff=True)
    _sweep_cog(cog, monkeypatch, [_channel([staff])])
    run(cog._sweep_history())
    staff.delete.assert_not_awaited()


def test_a_channel_that_errors_does_not_stop_the_sweep(cog, monkeypatch):
    def boom(limit=None, after=None):
        raise discord.HTTPException(MagicMock(status=403), "forbidden")
    broken = _channel([])
    broken.history = boom
    bad = _msg("free $3500 at fatowin.com use GIFT")
    _sweep_cog(cog, monkeypatch, [broken, _channel([bad])])
    run(cog._sweep_history())
    bad.delete.assert_awaited_once()
    ss.mark_backfill_done.assert_awaited_once()


def test_sweep_stops_without_a_marker_when_the_bot_is_shutting_down(cog, monkeypatch):
    bad = _msg("free $3500 at fatowin.com use GIFT")
    _sweep_cog(cog, monkeypatch, [_channel([bad])])
    cog.bot.is_closed = lambda: True
    run(cog._sweep_history())
    bad.delete.assert_not_awaited()
    ss.mark_backfill_done.assert_not_awaited()


def test_each_clone_uses_its_own_marker(cog, monkeypatch):
    _sweep_cog(cog, monkeypatch, [_channel([])], clone_id=12)
    run(cog._sweep_history())
    ss.mark_backfill_done.assert_awaited_once_with(12)


def test_sweep_summary_is_posted_to_the_hosting_channel(cog, monkeypatch):
    host = MagicMock(); host.send = AsyncMock()
    _sweep_cog(cog, monkeypatch, [_channel([_msg("free $3500 at fatowin.com use GIFT")])])
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=host))
    run(cog._sweep_history())
    summary = [c.args[0] for c in host.send.await_args_list if c.args]
    assert any("history sweep finished" in t and "1 scam message" in t for t in summary)


# ── AI image scan (Gemini) + separate evidence channel ───────────────────

from modules import scam_vision as sv  # noqa: E402


@pytest.fixture()
def vision(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    ss._c.vision = True
    ss._c.evidence_channel_id = 0
    sv._s.cache.clear(); sv._s.learned.clear(); sv._s.calls.clear(); sv._s.user_calls.clear()
    sv._s.blocked_until = 0.0; sv._s.day_key = ""; sv._s.day_count = 0
    monkeypatch.setattr(sv.time, "monotonic", lambda: 10 ** 12)
    sv._s.guild_used.clear(); sv._s.premium.clear()
    monkeypatch.setattr(sv, "_is_premium", AsyncMock(return_value=False))
    yield


@pytest.mark.parametrize("text,expected", [
    ("SCAM", True), ("scam.", True), ("SAFE", False), ("Safe", False),
    ("**SCAM**", True), ("", None), ("maybe", None), ("This is not a scam, it is safe", None),
])
def test_parse_verdict(text, expected):
    assert sv.parse_verdict(text) is expected


def test_vision_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert not sv.available()


def test_vision_flags_scam_and_caches_the_answer(vision, monkeypatch):
    ask = AsyncMock(return_value=True)
    monkeypatch.setattr(sv, "_ask", ask)
    monkeypatch.setattr(sv, "prepare", lambda d: b"jpeg")
    hit = run(sv.is_scam_image(b"A" * 20000, 1, 2))
    assert hit and hit[0] == "vision"
    assert run(sv.is_scam_image(b"A" * 20000, 1, 3))            # same bytes: from cache
    ask.assert_awaited_once()


def test_vision_safe_and_unknown_never_flag(vision, monkeypatch):
    monkeypatch.setattr(sv, "prepare", lambda d: b"jpeg")
    monkeypatch.setattr(sv, "_ask", AsyncMock(return_value=False))
    assert run(sv.is_scam_image(b"B" * 20000, 1, 2)) is None
    monkeypatch.setattr(sv, "_ask", AsyncMock(return_value=None))
    assert run(sv.is_scam_image(b"C" * 20000, 1, 2)) is None
    assert hash(b"C" * 20000) is not None and len(sv._s.cache) == 1   # unknown answers are not cached


def test_vision_budgets_are_respected(vision, monkeypatch):
    ask = AsyncMock(return_value=False)
    monkeypatch.setattr(sv, "_ask", ask)
    monkeypatch.setattr(sv, "prepare", lambda d: b"jpeg")
    monkeypatch.setenv("SCAM_VISION_PER_USER", "2")
    for n in range(5):
        run(sv.is_scam_image(bytes([n]) * 20000, 1, 77))
    assert ask.await_count == 2                                  # same user: only 2 checks per 10 min
    monkeypatch.setenv("SCAM_VISION_PER_USER", "99")
    monkeypatch.setenv("SCAM_VISION_RPM", "3")
    run(sv.is_scam_image(b"x" * 20000, 1, 78))
    run(sv.is_scam_image(b"y" * 20000, 1, 79))
    run(sv.is_scam_image(b"z" * 20000, 1, 80))
    assert ask.await_count == 3                                  # per-minute cap hit


def test_vision_circuit_breaker_blocks_calls(vision, monkeypatch):
    ask = AsyncMock(return_value=True)
    monkeypatch.setattr(sv, "_ask", ask)
    sv._s.blocked_until = 10 ** 12 + 60
    assert run(sv.is_scam_image(b"D" * 20000, 1, 2)) is None
    ask.assert_not_awaited()


def test_worth_checking_skips_tiny_and_huge():
    assert not sv.worth_checking(2000)
    assert not sv.worth_checking(sv.MAX_DOWNLOAD_BYTES + 1)
    assert not sv.worth_checking(50000, 100, 100)
    assert sv.worth_checking(50000, 800, 600)
    assert sv.worth_checking(50000)


def _big_att(name="pic.jpg"):
    a = _att(name, data=b"IMG" * 10000, size=30000)
    a.content_type, a.width, a.height = "image/jpeg", 900, 700
    return a


def test_cog_deletes_an_image_gemini_calls_a_scam(cog, vision, monkeypatch):
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=None))
    monkeypatch.setattr(sv, "is_scam_image", AsyncMock(return_value=("vision", "AI scan: scam image", None)))
    m = _msg("look at this", attachments=[_big_att()])
    assert run(cog._inspect(m)) is True
    m.delete.assert_awaited_once()
    assert cog.log.await_args.args[3] == "vision"


def test_cog_leaves_safe_images_alone_and_skips_gifs(cog, vision, monkeypatch):
    check = AsyncMock(return_value=None)
    monkeypatch.setattr(sv, "is_scam_image", check)
    m = _msg("meme", attachments=[_big_att()])
    assert run(cog._inspect(m)) is False
    m.delete.assert_not_awaited()
    check.assert_awaited_once()
    gif = _big_att("x.gif"); gif.content_type = "image/gif"
    check.reset_mock()
    run(cog._inspect(_msg("lol", attachments=[gif])))
    check.assert_not_awaited()


def test_staff_images_are_never_sent_to_gemini(cog, vision, monkeypatch):
    check = AsyncMock(return_value=("vision", "x", None))
    monkeypatch.setattr(sv, "is_scam_image", check)
    m = _msg("hi", staff=True, attachments=[_big_att()])
    assert run(cog._inspect(m)) is False
    check.assert_not_awaited()


def test_each_attachment_is_downloaded_once_for_hash_and_vision(cog, vision, monkeypatch):
    ss._c.images = [(9, 0)]
    monkeypatch.setattr(sv, "is_scam_image", AsyncMock(return_value=None))
    att = _big_att(); att.size = 30000
    run(cog._inspect(_msg("pic", attachments=[att])))
    assert att.read.await_count == 1


def test_scam_evidence_goes_to_the_scam_channel_not_the_hosting_channel(cog, monkeypatch):
    from discord_bot.cogs import scam_shield as cog_mod
    ss._c.evidence_channel_id = 321
    scam_ch = MagicMock(spec=discord.TextChannel); scam_ch.send = AsyncMock()
    cog.bot.get_channel = lambda cid: scam_ch if cid == 321 else None
    host = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr("discord_bot.ad_images._host_channel", host)
    run(cog._inspect(_msg("free $3500 at fatowin.com use GIFT", attachments=[_att()])))
    scam_ch.send.assert_awaited_once()
    host.assert_not_awaited()
    ss._c.evidence_channel_id = 0


def test_evidence_falls_back_to_hosting_channel_when_scam_channel_unset(cog, monkeypatch):
    ss._c.evidence_channel_id = 0
    hostch = MagicMock(); hostch.send = AsyncMock()
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=hostch))
    run(cog._inspect(_msg("free $3500 at fatowin.com use GIFT", attachments=[_att()])))
    hostch.send.assert_awaited_once()


def test_scam_vision_prefers_its_own_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "shared")
    monkeypatch.delenv("SCAM_VISION_API_KEY", raising=False)
    assert sv.api_key() == "shared"
    monkeypatch.setenv("SCAM_VISION_API_KEY", "scam-only")
    assert sv.api_key() == "scam-only"


def test_status_text_and_self_test(vision, monkeypatch):
    sv._s.last_ok, sv._s.last_at = None, 0.0
    assert "no scan yet" in sv.status_text()
    sv._note(False, "key rejected (401)")
    assert "ERROR" in sv.status_text() and "key rejected" in sv.status_text()

    async def fake_ok(jpeg):
        sv._note(True, "ok")
        return True
    monkeypatch.setattr(sv, "_ask", fake_ok)
    sv._s.blocked_until = 10 ** 13
    ok, msg = run(sv.self_test())
    assert ok and "SCAM" in msg and sv._s.blocked_until == 0.0

    async def fake_bad(jpeg):
        sv._note(False, "key rejected (401)")
        return None
    monkeypatch.setattr(sv, "_ask", fake_bad)
    ok, msg = run(sv.self_test())
    assert not ok and "key rejected" in msg


def test_free_server_cap_then_premium_cap(vision, monkeypatch):
    ask = AsyncMock(return_value=False)
    monkeypatch.setattr(sv, "_ask", ask)
    monkeypatch.setattr(sv, "prepare", lambda d: b"jpeg")
    monkeypatch.setenv("SCAM_VISION_PER_USER", "999")
    monkeypatch.setenv("SCAM_VISION_RPM", "999")
    monkeypatch.setenv("SCAM_VISION_DAILY", "999")
    assert run(sv.guild_cap(1)) == (10, False)
    for n in range(15):
        run(sv.is_scam_image(n.to_bytes(2, "big") * 10000, 1, 100 + n))
    assert ask.await_count == 10 and sv.guild_used_today(1) == 10   # free server stops at 10
    monkeypatch.setattr(sv, "_is_premium", AsyncMock(return_value=True))
    assert run(sv.guild_cap(1)) == (60, True)
    run(sv.is_scam_image(b"\xff\xee" * 10000, 1, 500))
    assert ask.await_count == 11                                    # upgrading lifts the limit right away
    assert run(sv.guild_cap(2)) == (60, True) and sv.guild_used_today(2) == 0


def test_cached_and_known_copies_do_not_use_the_server_cap(vision, monkeypatch):
    monkeypatch.setattr(sv, "_ask", AsyncMock(return_value=True))
    monkeypatch.setattr(sv, "prepare", lambda d: b"jpeg")
    run(sv.is_scam_image(b"S" * 20000, 7, 1))
    assert sv.guild_used_today(7) == 1
    assert run(sv.is_scam_image(b"S" * 20000, 7, 2))               # cached: free
    assert sv.guild_used_today(7) == 1


def test_caps_can_be_changed_by_env(vision, monkeypatch):
    monkeypatch.setenv("SCAM_VISION_FREE_DAILY", "3")
    monkeypatch.setenv("SCAM_VISION_PREMIUM_DAILY", "100")
    assert run(sv.guild_cap(1)) == (3, False)
    monkeypatch.setattr(sv, "_is_premium", AsyncMock(return_value=True))
    assert run(sv.guild_cap(1)) == (100, True)


def test_scam_shield_loads_after_admin_cog():
    """It mounts /admin scamchannel in cog_load, so AdminCog must already be loaded (this crashed startup once)."""
    src = (ROOT / "discord_bot" / "bot.py").read_text()
    assert src.index('"discord_bot.cogs.admin")') < src.index('"discord_bot.cogs.scam_shield")')
