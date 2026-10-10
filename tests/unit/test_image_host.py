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


# ---------- moving saved backgrounds into the shared channel ----------
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20
JPG = b"\xff\xd8\xff" + b"x" * 20


class Store:
    """Stand-in for the welcome-config table (one row per server of this bot) plus the global settings."""
    def __init__(self, rows):
        self.rows = {r["guild_id"]: dict(r) for r in rows}
        self.settings = {}
        self.moves = []

    async def welcome_custom_bg_rows(self, clone_id, limit=1000):
        return [dict(r) for r in self.rows.values()]

    async def welcome_custom_bg_move(self, gid, clone_id, old_msg, old_url, ch, msg, url):
        r = self.rows[gid]
        if r.get("custom_bg_message_id") != old_msg or r.get("custom_background_url") != old_url:
            return False                                                                   # an admin changed it meanwhile
        r.update(custom_bg_channel_id=ch, custom_bg_message_id=msg, custom_background_url=url)
        self.moves.append(gid)
        return True

    async def get_global_setting(self, key):
        return self.settings.get(key)

    async def set_global_setting(self, key, value):
        self.settings[key] = value


@pytest.fixture
def mover(env, monkeypatch):
    import database
    st = Store([])
    for n in ("welcome_custom_bg_rows", "welcome_custom_bg_move", "get_global_setting", "set_global_setting"):
        monkeypatch.setattr(database.db, n, getattr(st, n), raising=False)
    served = {}                                                                            # url -> bytes (what Discord/the web serves)

    async def fetch(url):
        if url in served:
            return served[url], None
        return None, "couldn't fetch that URL (HTTP 404)"

    async def refresh(bot, row):
        return row.get("_live")
    monkeypatch.setattr(ih, "_fetch_bytes", fetch)
    from discord_bot.cogs import welcome
    monkeypatch.setattr(welcome, "_refresh_custom_bg_url", refresh)
    st.served = served
    return st


def host_bot(clone_id=None):
    ch = Chan(DEFAULT)
    posted = []

    async def send(content=None, file=None):
        m = MagicMock()
        m.id = 9000 + len(posted)
        m.attachments = [types.SimpleNamespace(url=f"https://cdn.example/new{m.id}.png")]
        posted.append((content, file.filename))
        return m
    ch.send = send
    bot = Bot({DEFAULT: ch}, clone_id=clone_id)
    guilds = {}
    bot.get_guild = lambda gid: guilds.get(int(gid))
    bot.guild_map = guilds
    bot.posted = posted
    return bot


def row(gid, ch=None, msg=None, url=None, live=None):
    return {"guild_id": gid, "custom_bg_channel_id": ch, "custom_bg_message_id": msg, "custom_background_url": url, "_live": live}


def test_a_pasted_link_background_is_moved_into_the_hosting_channel(mover, monkeypatch):
    mover.rows = {1: row(1, url="https://img.example/bg.png")}
    mover.served["https://img.example/bg.png"] = PNG
    bot = host_bot()
    res = run(ih.move_backgrounds(bot, pause=0))
    assert (res.moved, res.failed, res.already_ok) == (1, [], 0) and mover.moves == [1]
    r = mover.rows[1]
    assert r["custom_bg_channel_id"] == DEFAULT and r["custom_bg_message_id"] == 9000 and r["custom_background_url"].startswith("https://cdn.example/new")
    assert bot.posted[0][1] == "background.png" and "guild `1`" in bot.posted[0][0]


def test_a_background_in_an_old_unreachable_channel_is_recovered_through_its_message_or_link(mover):
    mover.rows = {2: row(2, ch=555, msg=44, url="https://cdn.example/old.png", live="https://cdn.example/live.png")}
    mover.served["https://cdn.example/live.png"] = JPG
    bot = host_bot(clone_id=7)
    res = run(ih.move_backgrounds(bot, pause=0))
    assert res.moved == 1 and mover.rows[2]["custom_bg_channel_id"] == DEFAULT and bot.posted[0][1] == "background.jpg"


def test_servers_already_in_the_hosting_channel_are_left_alone(mover):
    mover.rows = {3: row(3, ch=DEFAULT, msg=77, url="https://cdn.example/a.png")}
    bot = host_bot()
    res = run(ih.move_backgrounds(bot, pause=0))
    assert (res.checked, res.already_ok, res.moved) == (1, 1, 0) and bot.posted == []


def test_an_unreadable_background_is_reported_and_its_row_is_untouched(mover):
    mover.rows = {4: row(4, ch=555, msg=44, url="https://cdn.example/expired.png")}
    before = dict(mover.rows[4])
    bot = host_bot()
    res = run(ih.move_backgrounds(bot, pause=0))
    assert res.moved == 0 and res.failed and res.failed[0][0] == 4 and "hosting message is gone" in res.failed[0][1] and mover.rows[4] == before
    text = ih.format_move_report(res, "main bot")
    assert "`4`" in text and "/welcome custombg" in text


def test_an_image_that_is_not_a_valid_background_is_not_moved(mover, monkeypatch):
    mover.rows = {5: row(5, url="https://img.example/huge.png")}

    async def fetch(url):
        return None, "that image is over the 8MB limit"
    monkeypatch.setattr(ih, "_fetch_bytes", fetch)
    res = run(ih.move_backgrounds(host_bot(), pause=0))
    assert res.failed == [(5, "that image is over the 8MB limit")]


def test_an_admin_who_saved_a_new_background_meanwhile_is_not_overwritten(mover, monkeypatch):
    mover.rows = {6: row(6, url="https://img.example/old.png")}
    mover.served["https://img.example/old.png"] = PNG
    orig = ih._saved_bytes

    async def saved(bot, r):
        out = await orig(bot, r)
        mover.rows[6].update(custom_bg_message_id=1234, custom_background_url="https://cdn.example/brandnew.png")     # admin re-uploads
        return out
    monkeypatch.setattr(ih, "_saved_bytes", saved)
    res = run(ih.move_backgrounds(host_bot(), pause=0))
    assert res.moved == 0 and mover.rows[6]["custom_bg_message_id"] == 1234 and mover.rows[6]["custom_background_url"] == "https://cdn.example/brandnew.png"


def test_per_run_limit_leaves_the_rest_for_the_next_run(mover):
    mover.rows = {i: row(i, url=f"https://img.example/{i}.png") for i in range(1, 6)}
    for i in range(1, 6):
        mover.served[f"https://img.example/{i}.png"] = PNG
    bot = host_bot()
    res = run(ih.move_backgrounds(bot, limit=2, pause=0))
    assert (res.moved, res.waiting) == (2, 3)
    res2 = run(ih.move_backgrounds(bot, limit=10, pause=0))
    assert res2.moved == 3 and res2.already_ok == 2 and res2.waiting == 0


def test_no_reachable_channel_means_nothing_is_touched(mover):
    mover.rows = {1: row(1, url="https://img.example/bg.png")}
    res = run(ih.move_backgrounds(Bot(), pause=0))
    assert res.error and mover.moves == [] and "Nothing was moved" in ih.format_move_report(res)


def test_one_server_crashing_does_not_stop_the_others(mover, monkeypatch):
    mover.rows = {1: row(1, url="https://img.example/1.png"), 2: row(2, url="https://img.example/2.png")}
    mover.served.update({"https://img.example/1.png": PNG, "https://img.example/2.png": PNG})
    orig = ih._saved_bytes

    async def saved(bot, r):
        if r["guild_id"] == 1:
            raise ValueError("bad")
        return await orig(bot, r)
    monkeypatch.setattr(ih, "_saved_bytes", saved)
    res = run(ih.move_backgrounds(host_bot(), pause=0))
    assert res.moved == 1 and res.failed == [(1, "unexpected error (ValueError)")]


def test_auto_move_tells_the_bot_owners_and_each_server_owner_once_a_week(mover):
    mover.rows = {8: row(8, ch=555, msg=44, url="https://cdn.example/expired.png")}
    bot = host_bot()
    owner = MagicMock()
    owner.send = AsyncMock()
    guild = types.SimpleNamespace(id=8, name="Cool Server", owner_id=99, owner=owner)
    bot.guild_map[8] = guild
    res = run(ih.auto_move(bot))
    assert res.failed and len(bot.dms) == 2 and "Could not move" in bot.dms[0][1]                    # both bot owners
    assert owner.send.await_count == 1 and "Cool Server" in owner.send.await_args.args[0] and "/welcome custombg" in owner.send.await_args.args[0]
    ih._alert_sent.clear()
    run(ih.auto_move(bot))
    assert owner.send.await_count == 1                                                               # not again within a week


def test_server_owner_notices_can_be_switched_off_and_are_capped(mover, monkeypatch):
    mover.rows = {i: row(i, ch=555, msg=44, url="https://cdn.example/expired.png") for i in range(1, 15)}
    bot = host_bot()
    sent = []
    for i in range(1, 15):
        o = MagicMock()
        o.send = AsyncMock(side_effect=lambda t, i=i: sent.append(i))
        bot.guild_map[i] = types.SimpleNamespace(id=i, name=f"G{i}", owner_id=100 + i, owner=o)
    monkeypatch.setattr(ih, "NOTIFY_SERVER_OWNERS", False)
    run(ih.auto_move(bot))
    assert sent == []
    monkeypatch.setattr(ih, "NOTIFY_SERVER_OWNERS", True)
    ih._alert_sent.clear()
    run(ih.auto_move(bot))
    assert len(sent) == ih.MAX_SERVER_NOTICES_PER_RUN


def test_auto_move_alerts_when_the_hosting_channel_is_unreachable(mover):
    mover.rows = {1: row(1, url="https://img.example/bg.png")}
    bot = Bot(clone_id=5)
    run(ih.auto_move(bot))
    assert len(bot.dms) == 2 and "Nothing was moved" in bot.dms[0][1] and "clone #5" in bot.dms[0][1]


def test_background_jobs_start_once_and_run_the_startup_check_then_the_mover(env, monkeypatch):
    calls = []

    async def startup(bot, delay=0):
        calls.append("startup")

    async def move(bot):
        calls.append("move")
        raise asyncio.CancelledError
    monkeypatch.setattr(ih, "startup_check", startup)
    monkeypatch.setattr(ih, "auto_move", move)
    monkeypatch.setattr(ih, "_jobs_started", False)
    with pytest.raises(asyncio.CancelledError):
        run(ih.background_jobs(Bot(), delay=0))
    assert calls == ["startup", "move"]
    run(ih.background_jobs(Bot(), delay=0))                                                          # second call returns at once
    assert calls == ["startup", "move"]


def test_sql_for_listing_and_moving_is_scoped_and_compare_and_set():
    import inspect
    import database
    rows_sql = inspect.getsource(database.Database.welcome_custom_bg_rows)
    assert "clone_id IS NOT DISTINCT FROM $1" in rows_sql and "ultra_pack_unlocked = TRUE" in rows_sql
    move_sql = inspect.getsource(database.Database.welcome_custom_bg_move)
    assert "guild_id = $1" in move_sql and "clone_id IS NOT DISTINCT FROM $2" in move_sql
    assert "custom_bg_message_id IS NOT DISTINCT FROM $3" in move_sql and "custom_background_url IS NOT DISTINCT FROM $4" in move_sql


def test_pasted_links_are_rehosted_when_saved_and_the_wizard_falls_back_to_the_link(env, monkeypatch):
    import inspect
    from discord_bot.cogs import _views_card_customize as wiz
    src = inspect.getsource(wiz)
    assert "_host_bytes(" in src and "hosted[2] or url" in src and "custom_background_url=url, custom_bg_channel_id=None" in src


def test_host_bytes_posts_through_the_shared_helper(env, monkeypatch):
    from discord_bot.cogs import welcome
    got = {}

    async def post(bot, data, label):
        got.update(data=data, label=label)
        return (1, 2, "u")
    monkeypatch.setattr(ih, "post_to_host", post)
    out = run(welcome._host_bytes(Bot(), PNG, types.SimpleNamespace(name="My Guild"), 12))
    assert out == (1, 2, "u") and "guild `12`" in got["label"] and "My Guild" in got["label"]


def test_changing_the_hosting_channel_starts_a_move(env):
    import inspect
    from discord_bot.cogs import welcome
    src = inspect.getsource(welcome.WelcomeCog.hostingchannel)
    assert "image_host.auto_move(self.bot)" in src
