"""Image hosting channel: shared default for main + clones, fallback when unreachable, the owner-panel test, and the alerts."""
import asyncio
import io
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from modules import image_host as ih

DEFAULT = 1541141079913660446


def run(c):
    return asyncio.run(c)


def http(exc, status):
    return exc(types.SimpleNamespace(status=status, reason="x"), "boom")


class Chan(discord.TextChannel):
    """A minimal text channel: isinstance(..., discord.TextChannel) is True without a real connection."""
    def __init__(self, cid, perms=None, send_exc=None):
        self.id, self.name = cid, f"chan{cid}"
        self.guild = MagicMock()
        self.guild.name = "Support"
        self._perms = perms if perms is not None else discord.Permissions(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        self._send_exc = send_exc
        self.sent = []

    def permissions_for(self, member):
        return self._perms

    async def send(self, content=None, file=None):
        if self._send_exc:
            raise self._send_exc
        m = MagicMock()
        m.id = 777
        m.delete = AsyncMock()
        self.sent.append((content, file))
        self._msg = m
        return m

    async def fetch_message(self, mid):
        m = MagicMock()
        m.attachments = [types.SimpleNamespace(url="https://cdn.example/x.png")]
        return m


class Bot:
    def __init__(self, chans=None, clone_id=None, fetch_exc=None):
        self.chans = chans or {}
        self.clone_id = clone_id
        self.fetch_exc = fetch_exc or {}
        self.user = types.SimpleNamespace(id=42)
        self.dms = []

    def get_channel(self, cid):
        return self.chans.get(cid)

    async def fetch_channel(self, cid):
        if cid in self.fetch_exc:
            raise self.fetch_exc[cid]
        raise http(discord.NotFound, 404)

    def get_user(self, uid):
        u = MagicMock()
        u.send = AsyncMock(side_effect=lambda t: self.dms.append((uid, t)))
        return u


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "IMAGE_HOST_CHANNEL_ID", DEFAULT)
    monkeypatch.setattr(config, "DISCORD_OWNER_BROADCAST_IDS", {1, 2})
    import database
    monkeypatch.setattr(database.db, "get_global_setting", AsyncMock(return_value=None), raising=False)
    ih._alert_sent.clear()
    monkeypatch.setattr(ih, "_startup_done", False)
    monkeypatch.setattr(ih, "_test_png", lambda: b"\x89PNG-test")
    return types.SimpleNamespace(db=database.db)


def set_owner_channel(env, cid):
    env.db.get_global_setting = AsyncMock(return_value=str(cid))


def test_config_default_is_the_shared_support_server_channel():
    import importlib
    assert config.IMAGE_HOST_CHANNEL_ID == DEFAULT
    import inspect
    assert str(DEFAULT) in inspect.getsource(importlib.import_module("config"))


# ---------- finding the channel ----------
def test_candidates_order_dedupe_and_junk(env):
    assert run(ih.candidates()) == [(DEFAULT, "default (IMAGE_HOST_CHANNEL_ID)")]
    set_owner_channel(env, 555)
    assert [c for c, _ in run(ih.candidates())] == [555, DEFAULT]
    set_owner_channel(env, DEFAULT)
    assert [c for c, _ in run(ih.candidates())] == [DEFAULT]
    for junk in ("abc", "0", "-5", ""):
        set_owner_channel(env, junk)
        assert [c for c, _ in run(ih.candidates())] == [DEFAULT]


def test_a_clone_that_cannot_reach_the_owners_channel_falls_back_to_the_default(env):
    set_owner_channel(env, 555)
    clone = Bot({DEFAULT: Chan(DEFAULT)}, clone_id=7)                      # clone is only in the support server
    assert run(ih.resolve_channel(clone)).id == DEFAULT
    main = Bot({555: Chan(555), DEFAULT: Chan(DEFAULT)})
    assert run(ih.resolve_channel(main)).id == 555                          # the owner's choice still wins when reachable


def test_resolve_returns_none_when_nothing_is_reachable(env):
    assert run(ih.resolve_channel(Bot())) is None


@pytest.mark.parametrize("exc,words", [(http(discord.NotFound, 404), "not in its server"), (http(discord.Forbidden, 403), "no access"),
                                       (http(discord.HTTPException, 500), "HTTP 500")])
def test_open_channel_gives_a_plain_reason(exc, words):
    ch, why = run(ih.open_channel(Bot(fetch_exc={9: exc}), 9))
    assert ch is None and words in why


def test_a_voice_or_other_channel_is_refused():
    bot = Bot({9: MagicMock(spec=discord.VoiceChannel)})
    ch, why = run(ih.open_channel(bot, 9))
    assert ch is None and "text channel" in why


# ---------- the test the owner presses ----------
@pytest.fixture
def good(monkeypatch):
    async def fetch(url):
        return b"PNGDATA", None

    async def render(bg):
        return b"CARDPNG"
    monkeypatch.setattr(ih, "_fetch_bytes", fetch)
    monkeypatch.setattr(ih, "_render_card", render)


def names(checks):
    return {c.name: c for c in checks}


def test_everything_works(env, good):
    ch = Chan(DEFAULT)
    checks, card = run(ih.diagnose(Bot({DEFAULT: ch})))
    assert not ih.problems(checks) and card == b"CARDPNG"
    assert [n for n in names(checks)] == ["Channel id", "Channel reachable", "Permissions", "Upload", "Read back", "Welcome card"]
    assert ch.sent and ch._msg.delete.await_count == 1                        # the test message is cleaned up
    text = ih.format_report(checks, "main bot")
    assert text.startswith("✅") and "❌" not in text and "Fix:" not in text


def test_nothing_configured(env, monkeypatch):
    monkeypatch.setattr(config, "IMAGE_HOST_CHANNEL_ID", 0)
    checks, card = run(ih.diagnose(Bot()))
    assert card is None and ih.problems(checks)[0].name == "Channel id"


def test_unreachable_channel_names_the_bot_and_the_fix(env):
    checks, card = run(ih.diagnose(Bot(clone_id=3)))
    bad = ih.problems(checks)
    assert bad and "not in its server" in bad[0].detail and "invite this bot" in bad[0].fix and card is None
    text = ih.format_report(checks, ih.bot_label(Bot(clone_id=3)))
    assert text.startswith("❌") and "clone #3" in text and "Fix:" in text


def test_missing_permissions_are_listed_and_nothing_is_posted(env):
    ch = Chan(DEFAULT, perms=discord.Permissions(view_channel=True, send_messages=True))
    checks, _ = run(ih.diagnose(Bot({DEFAULT: ch})))
    bad = ih.problems(checks)[0]
    assert bad.name == "Permissions" and "Attach Files" in bad.detail and "Read Message History" in bad.detail and ch.sent == []


@pytest.mark.parametrize("exc,words", [(http(discord.Forbidden, 403), "Forbidden"), (http(discord.HTTPException, 413), "HTTP 413")])
def test_upload_refusal_is_reported(env, exc, words):
    checks, card = run(ih.diagnose(Bot({DEFAULT: Chan(DEFAULT, send_exc=exc)})))
    bad = ih.problems(checks)[0]
    assert bad.name == "Upload" and words in bad.detail and card is None


def test_read_back_failure_is_reported_and_card_is_skipped(env, monkeypatch):
    async def fetch(url):
        return None, "couldn't fetch that URL (HTTP 403)"
    monkeypatch.setattr(ih, "_fetch_bytes", fetch)
    checks, card = run(ih.diagnose(Bot({DEFAULT: Chan(DEFAULT)})))
    assert names(checks)["Read back"].ok is False and "HTTP 403" in names(checks)["Read back"].detail
    assert "Welcome card" not in names(checks) and card is None


def test_card_drawing_failure_is_reported(env, monkeypatch, good):
    async def boom(bg):
        raise ValueError("pillow")
    monkeypatch.setattr(ih, "_render_card", boom)
    checks, card = run(ih.diagnose(Bot({DEFAULT: Chan(DEFAULT)})))
    assert names(checks)["Welcome card"].ok is False and "ValueError" in names(checks)["Welcome card"].detail and card is None


def test_a_cleanup_failure_is_harmless_info(env, good):
    ch = Chan(DEFAULT)
    orig = ch.send

    async def send(*a, **k):
        m = await orig(*a, **k)
        m.delete = AsyncMock(side_effect=http(discord.Forbidden, 403))
        return m
    ch.send = send
    checks, _ = run(ih.diagnose(Bot({DEFAULT: ch})))
    assert names(checks)["Cleanup"].ok is None and not ih.problems(checks)


def test_a_fallback_channel_is_called_out(env, good):
    set_owner_channel(env, 555)
    checks, _ = run(ih.diagnose(Bot({DEFAULT: Chan(DEFAULT)}, clone_id=2)))
    reach = [c for c in checks if c.name == "Channel reachable"]
    assert [c.ok for c in reach] == [False, True] and "a fallback was used" in reach[1].detail and not any(c.ok is False for c in checks[-4:])


def test_report_is_short_enough_for_discord():
    many = [ih.Check(f"c{i}", False, "x" * 300, "y" * 300) for i in range(20)]
    assert len(ih.format_report(many)) <= 1900


# ---------- alerts ----------
def test_owner_alert_goes_to_every_owner_once_per_cooldown(env):
    bot = Bot(clone_id=4)
    assert run(ih.notify_owners(bot, "k", "problem")) is True
    assert sorted(u for u, _ in bot.dms) == [1, 2] and "clone #4" in bot.dms[0][1]
    assert run(ih.notify_owners(bot, "k", "problem")) is False and len(bot.dms) == 2
    assert run(ih.notify_owners(bot, "other", "problem")) is True
    assert run(ih.notify_owners(Bot(clone_id=5), "k", "problem")) is True               # per bot


def test_one_owner_with_closed_dms_does_not_stop_the_other(env):
    bot = Bot()
    real = bot.get_user

    def get_user(uid):
        u = real(uid)
        if uid == 1:
            u.send = AsyncMock(side_effect=http(discord.Forbidden, 403))
        return u
    bot.get_user = get_user
    assert run(ih.notify_owners(bot, "k", "problem")) is True and [u for u, _ in bot.dms] == [2]


def test_startup_check_is_quiet_when_fine_and_alerts_once_when_not(env, good, monkeypatch):
    bot = Bot({DEFAULT: Chan(DEFAULT)})
    run(ih.startup_check(bot, delay=0))
    assert bot.dms == []
    monkeypatch.setattr(ih, "_startup_done", False)
    broken = Bot(clone_id=9)
    run(ih.startup_check(broken, delay=0))
    assert len(broken.dms) == 2 and "will not be sent" in broken.dms[0][1]
    run(ih.startup_check(broken, delay=0))                                                 # once per process
    assert len(broken.dms) == 2


def test_startup_check_never_posts_a_test_image(env, good):
    ch = Chan(DEFAULT)
    run(ih.startup_check(Bot({DEFAULT: ch}), delay=0))
    assert ch.sent == []


def test_render_problem_alert_is_rate_limited_per_server(env):
    bot = Bot()
    run(ih.report_render_problem(bot, 123, "the saved image could not be loaded"))
    run(ih.report_render_problem(bot, 123, "again"))
    assert len(bot.dms) == 2                                                               # 2 owners, once
    run(ih.report_render_problem(bot, 456, "x"))
    assert len(bot.dms) == 4


# ---------- the welcome and ads paths use the shared resolver, and say so when it fails ----------
def test_welcome_and_ads_use_the_shared_resolver(env, monkeypatch):
    from discord_bot import ad_images
    from discord_bot.cogs import welcome
    seen = []

    async def fake(bot):
        seen.append(bot)
        return "CHAN"
    monkeypatch.setattr(ih, "resolve_channel", fake)
    assert run(welcome._get_image_host_channel("b1")) == "CHAN" and run(ad_images._host_channel("b2")) == "CHAN" and seen == ["b1", "b2"]


def test_upload_failure_tells_the_admin_and_the_owner(env, monkeypatch):
    from discord_bot.cogs import welcome

    async def none(bot):
        return None
    monkeypatch.setattr(ih, "resolve_channel", none)
    told = []

    async def notify(bot, key, text, cooldown=0):
        told.append((key, text))
    monkeypatch.setattr(ih, "notify_owners", notify)
    att = types.SimpleNamespace(content_type="image/png", size=10)
    guild = types.SimpleNamespace(id=5, name="G")

    async def go():
        out = await welcome._upload_custom_bg(Bot(), att, guild)
        await asyncio.sleep(0)
        return out
    out = run(go())
    assert out[0] is None and "bot owner has been told" in out[3] and "/admin hostingchannel" not in out[3]
    assert told and told[0][0] == "upload" and "Test image hosting" in told[0][1]


def test_render_time_failures_alert_the_owner_only_when_a_background_was_saved(env, monkeypatch):
    from discord_bot.cogs import welcome
    alerts = []

    async def report(bot, gid, reason):
        alerts.append((gid, reason))
    monkeypatch.setattr(ih, "report_render_problem", report)

    async def refresh(bot, row):
        return None
    monkeypatch.setattr(welcome, "_refresh_custom_bg_url", refresh)

    async def go(row):
        out = await welcome._custom_bg_bytes_for_render(None, row, Bot())
        await asyncio.sleep(0)
        return out
    assert run(go({"guild_id": 1, "ultra_pack_unlocked": True})) is None and alerts == []                       # never set one: normal
    assert run(go({"guild_id": 2, "ultra_pack_unlocked": True, "custom_bg_message_id": 9})) is None
    assert alerts and alerts[0][0] == 2 and "could not be found again" in alerts[0][1]

    async def bad_fetch(session, url):
        return None, "couldn't fetch that URL (HTTP 403)"
    monkeypatch.setattr(welcome, "_fetch_custom_bg_bytes", bad_fetch)
    alerts.clear()
    assert run(go({"guild_id": 3, "ultra_pack_unlocked": True, "custom_background_url": "https://x/y.png"})) is None
    assert alerts and "HTTP 403" in alerts[0][1]
