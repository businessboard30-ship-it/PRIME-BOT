"""Clone health board: classification, quiet change detection, V2 limits, and the cog's behaviour with a fake Discord."""
import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import clone_health as cog_mod
from modules import clone_health as ch

run = asyncio.run
NOW = 1_800_000_000.0


def dt(age_s):
    return datetime.fromtimestamp(NOW - age_s, tz=timezone.utc) if age_s is not None else None


def row(cid, age=60, status="active", servers=2, members=100, created_age=10 ** 6, name=None):
    return {"clone_id": cid, "bot_username": name or f"bot{cid}", "status": status, "last_heartbeat": dt(age),
            "created_at": dt(created_age), "servers": servers, "members": members}


# ── classification ────────────────────────────────────────────────────────────
def test_states_follow_the_heartbeat_age():
    c = lambda status, age, created=10 ** 6: ch.classify(status, None if age is None else NOW - age, NOW - created, NOW)
    assert c("active", 60) == ch.ONLINE and c("active", 8 * 60) == ch.ONLINE
    assert c("active", 9 * 60) == ch.DELAYED and c("active", 20 * 60) == ch.DELAYED
    assert c("active", 21 * 60) == ch.OFFLINE
    assert c("inactive", 60) == ch.STOPPED
    assert c("active", None, created=60) == ch.STARTING          # brand new, not beaten yet
    assert c("active", None, created=3600) == ch.OFFLINE         # never connected


def test_problems_sort_first_and_names_are_clean():
    es = ch.build_entries([row(1), row(2, age=3000), row(3, age=700), row(4, status="inactive"),
                           row(5, name="@" + "x" * 80)], NOW)
    assert [e.state for e in es] == [ch.OFFLINE, ch.DELAYED, ch.ONLINE, ch.ONLINE, ch.STOPPED]
    assert next(e for e in es if e.clone_id == 5).name == "x" * ch.NAME_MAX


# ── noise control ─────────────────────────────────────────────────────────────
def test_fingerprint_ignores_time_and_member_counts_but_sees_real_changes():
    a = ch.build_entries([row(1, age=10, members=100), row(2)], NOW)
    b = ch.build_entries([row(1, age=400, members=999), row(2)], NOW)       # older beat, more members: same state
    assert ch.fingerprint(a, 3) == ch.fingerprint(b, 3)
    assert ch.fingerprint(a, 3) != ch.fingerprint(a, 4)                      # main bot joined a server
    c = ch.build_entries([row(1, servers=3), row(2)], NOW)
    assert ch.fingerprint(a, 3) != ch.fingerprint(c, 3)
    d = ch.build_entries([row(1, age=3000), row(2)], NOW)
    assert ch.fingerprint(a, 3) != ch.fingerprint(d, 3)


def test_only_the_offline_boundary_is_an_event_and_a_restart_is_silent():
    on = ch.build_entries([row(1), row(2)], NOW)
    assert ch.diff_states(None, on) == ([], [])                              # first look after boot = baseline
    prev = {1: ch.ONLINE, 2: ch.ONLINE}
    yellow = ch.build_entries([row(1, age=700), row(2)], NOW)
    assert ch.diff_states(prev, yellow) == ([], [])                          # delayed is not an alert
    red = ch.build_entries([row(1, age=3000), row(2)], NOW)
    down, up = ch.diff_states(prev, red)
    assert [e.clone_id for e in down] == [1] and up == []
    back, rec = ch.diff_states({1: ch.OFFLINE, 2: ch.ONLINE}, on)
    assert back == [] and [e.clone_id for e in rec] == [1]
    assert ch.diff_states({}, red) == ([], [])                               # a brand-new clone is not a "transition"


# ── the V2 pages ──────────────────────────────────────────────────────────────
def _walk(v):
    return list(v.walk_children())


def _text(v):
    return "\n".join(n.content for n in _walk(v) if isinstance(n, discord.ui.TextDisplay))


MAIN = ch.MainInfo(12, 3400, int(NOW) - 5000)


def test_pages_are_valid_v2_with_no_timeout_and_no_pings():
    pages = ch.build_pages(ch.build_entries([row(1), row(2, age=3000)], NOW), MAIN, [(int(NOW), "🔴 #2 bot2 went offline")])
    assert len(pages) == 1 and pages[0].timeout is None and pages[0].to_components()
    t = _text(pages[0])
    assert "Clone health" in t and "Main bot" in t and "#2" in t and "<t:" in t and "went offline" in t
    assert "<@" not in t                                                     # names are plain text, never mentions


def test_a_huge_fleet_stays_inside_discords_limits_with_problems_first():
    rows = [row(i, name="n" * 40, servers=10 ** 6, members=10 ** 9) for i in range(1, 301)]
    rows[299] = row(300, age=99999, name="broken")                            # the only problem is the LAST id
    pages = ch.build_pages(ch.build_entries(rows, NOW), MAIN, [(int(NOW), "x" * 40)] * 6)
    assert 1 < len(pages) <= ch.MAX_PAGES
    for p in pages:
        assert p.to_components() and len(_walk(p)) <= 40 and len(_text(p)) < 4000
    assert "#300" in _text(pages[0])                                          # surfaced despite being last
    assert "more not listed" in _text(pages[-1])


def test_empty_and_stopped_only_fleets_render():
    assert "No clones" in _text(ch.build_pages([], MAIN, [])[0])
    p = ch.build_pages(ch.build_entries([row(1, status="inactive")], NOW), MAIN, [])[0]
    assert "stopped clone" in _text(p)


# ── the cog, with a fake Discord ──────────────────────────────────────────────
class Msg:
    def __init__(self, author_id=1):
        self.author = SimpleNamespace(id=author_id)
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class Channel:
    def __init__(self, existing=()):
        self.id = 500
        self.name = cog_mod.CHANNEL_NAME
        self.existing = list(existing)
        self.sent = []

        async def send(**kw):
            m = Msg(); m.kw = kw; self.sent.append(m); return m
        self.send = send

    def history(self, limit=100, oldest_first=False):
        items = list(self.existing)

        async def gen():
            for m in items:
                yield m
        return gen()


def make(monkeypatch, rows, channel=None, create_ok=True):
    monkeypatch.setattr(cog_mod.config, "DISCORD_SUPPORT_SERVER_ID", 42, raising=False)
    state = {"rows": rows}
    monkeypatch.setattr(cog_mod.db, "get_clone_health_rows", AsyncMock(side_effect=lambda: state["rows"]), raising=False)
    guild = MagicMock()
    guild.id = 42; guild.member_count = 10
    guild.text_channels = [channel] if channel else []
    created = Channel()
    known = {c.id: c for c in (channel, created) if c is not None}
    # a real guild returns the channels it knows about, including one it just created
    guild.get_channel = lambda cid: known.get(cid) if cid in known and (cid != created.id or guild.create_text_channel.await_count) else (channel if channel and cid == channel.id else None)
    if create_ok:
        guild.create_text_channel = AsyncMock(return_value=created)
    else:
        guild.create_text_channel = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403, reason="x"), "no"))
    bot = SimpleNamespace(guilds=[SimpleNamespace(member_count=50)], user=SimpleNamespace(id=1), clone_id=None,
                          get_guild=lambda gid: guild if gid == 42 else None)
    cog = cog_mod.CloneHealthCog(bot)
    clock = {"t": NOW}
    monkeypatch.setattr(cog_mod.time, "time", lambda: clock["t"])
    return cog, state, guild, created, clock


def test_first_look_creates_a_private_channel_and_posts_one_board(monkeypatch):
    cog, state, guild, created, clock = make(monkeypatch, [row(1), row(2)])
    assert run(cog.refresh()) is True
    ow = guild.create_text_channel.await_args.kwargs["overwrites"]
    assert ow[guild.default_role].view_channel is False and ow[guild.me].view_channel is True
    assert len(created.sent) == 1
    kw = created.sent[0].kw
    assert isinstance(kw["view"], discord.ui.LayoutView) and kw["allowed_mentions"].users is False


def test_nothing_changed_means_zero_discord_calls(monkeypatch):
    cog, state, guild, created, clock = make(monkeypatch, [row(1), row(2)])
    run(cog.refresh())
    sent_before = len(created.sent)
    msg = cog._messages[0]
    for _ in range(5):
        clock["t"] += 60
        state["rows"] = [row(1, age=60 + 60), row(2)]                          # time passing only
        assert run(cog.refresh()) is False
    assert len(created.sent) == sent_before and msg.edit.await_count == 0
    assert guild.create_text_channel.await_count == 1


def test_a_state_change_edits_the_same_message_once(monkeypatch):
    cog, state, guild, created, clock = make(monkeypatch, [row(1), row(2)])
    run(cog.refresh())
    msg = cog._messages[0]
    clock["t"] += 120
    state["rows"] = [row(1, age=3000), row(2)]
    assert run(cog.refresh()) is True
    assert msg.edit.await_count == 1 and len(created.sent) == 1
    assert msg.edit.await_args.kwargs["allowed_mentions"].roles is False


def test_edits_are_debounced_then_retried(monkeypatch):
    cog, state, guild, created, clock = make(monkeypatch, [row(1)])
    run(cog.refresh())
    msg = cog._messages[0]
    clock["t"] += 5                                                            # too soon
    state["rows"] = [row(1, servers=9)]
    assert run(cog.refresh()) is False and msg.edit.await_count == 0
    clock["t"] += 60
    assert run(cog.refresh()) is True and msg.edit.await_count == 1


def test_a_deleted_board_message_is_replaced_not_crashed_on(monkeypatch):
    cog, state, guild, created, clock = make(monkeypatch, [row(1)])
    run(cog.refresh())
    cog._messages[0].edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "gone"))
    clock["t"] += 120
    state["rows"] = [row(1, servers=5)]
    assert run(cog.refresh()) is True and len(created.sent) == 2


def test_after_a_restart_the_existing_board_is_reused(monkeypatch):
    old = Msg(author_id=1); stray = Msg(author_id=999)
    ch_existing = Channel(existing=[stray, old])
    cog, state, guild, created, clock = make(monkeypatch, [row(1)], channel=ch_existing)
    assert run(cog.refresh()) is True
    assert old.edit.await_count == 1 and stray.edit.await_count == 0 and ch_existing.sent == []
    assert guild.create_text_channel.await_count == 0


def test_logging_is_quiet_baseline_then_one_line_per_real_event(monkeypatch, caplog):
    cog, state, guild, created, clock = make(monkeypatch, [row(1, age=3000), row(2)])      # #1 is ALREADY offline
    with caplog.at_level(logging.INFO, logger="discord_bot.cogs.clone_health"):
        run(cog.refresh())
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []           # restart = baseline, silent
        clock["t"] += 120
        state["rows"] = [row(1, age=3000), row(2, age=3000)]
        run(cog.refresh())
        warn = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn) == 1 and "clone #2" in warn[0].getMessage()
        clock["t"] += 120
        state["rows"] = [row(1), row(2, age=3000)]
        run(cog.refresh())
        assert any("clone #1" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1       # recovery is not a warning


def test_a_mass_outage_is_one_summary_line_not_a_storm(monkeypatch, caplog):
    cog, state, guild, created, clock = make(monkeypatch, [row(i) for i in range(1, 21)])
    run(cog.refresh())
    clock["t"] += 120
    state["rows"] = [row(i, age=9999) for i in range(1, 21)]
    with caplog.at_level(logging.INFO, logger="discord_bot.cogs.clone_health"):
        run(cog.refresh())
    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn) == 1 and "20 clones went offline at once" in warn[0].getMessage()


def test_a_missing_permission_is_logged_once_not_every_minute(monkeypatch, caplog):
    cog, state, guild, created, clock = make(monkeypatch, [row(1)], create_ok=False)
    with caplog.at_level(logging.WARNING, logger="discord_bot.cogs.clone_health"):
        for _ in range(6):
            clock["t"] += 60
            assert run(cog.refresh()) is False
    assert len([r for r in caplog.records if "couldn't create" in r.getMessage()]) == 1


def test_a_failing_tick_never_raises_and_logs_sparingly(monkeypatch, caplog):
    cog, state, guild, created, clock = make(monkeypatch, [row(1)])
    monkeypatch.setattr(cog_mod.db, "get_clone_health_rows", AsyncMock(side_effect=RuntimeError("db down")), raising=False)
    with caplog.at_level(logging.WARNING, logger="discord_bot.cogs.clone_health"):
        for _ in range(10):
            run(cog.tick.coro(cog))
    assert len([r for r in caplog.records if "update failed" in r.getMessage()]) == 1


def test_clones_never_load_the_cog():
    bot = MagicMock(); bot.clone_id = 7; bot.add_cog = AsyncMock()
    run(cog_mod.setup(bot))
    bot.add_cog.assert_not_awaited()
    bot2 = MagicMock(); bot2.clone_id = None; bot2.add_cog = AsyncMock()
    run(cog_mod.setup(bot2))
    bot2.add_cog.assert_awaited_once()
