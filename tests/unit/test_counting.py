"""Counting game (rules, More features button, listener) + total followers on the anime spawn card."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_anime_feed as vaf
from discord_bot.cogs import _views_join_dm as jd
from discord_bot.cogs import counting as cc
from modules import anime_feed as af
from modules import anime_feed_browse as ab
from modules import anime_feed_spawn as spawn
from modules import counting as cnt

run = asyncio.run


# ── parsing ──────────────────────────────────────────────────────────────

def test_plain_numbers_only_unless_math_is_on():
    assert cnt.parse_number("42") == 42 and cnt.parse_number("  7 ") == 7
    for chat in ("hello", "5 lol", "", None, "1,000", "12.5", "-3", "9999999999999"):
        assert cnt.parse_number(chat) is None
    assert cnt.parse_number("2+3") is None                       # maths is a Premium extra


def test_math_is_safe_and_exact():
    p = lambda t: cnt.parse_number(t, allow_math=True)
    assert p("2*3+1") == 7 and p("(4+6)/2") == 5 and p("10-3") == 7
    assert p("-5") is None and p("(5)") is None                  # not an attempt: no binary operator
    assert p("7/2") is None and p("1/0") is None                 # only exact whole-number division
    assert p("2**9") is None and p("__import__('os')") is None and p("1+a") is None
    assert p("9" * 30 + "*" + "9" * 30) is None                 # absurd sizes are just chat
    assert p("1+" * 30 + "1") is None                           # too long


def test_milestone_validation():
    assert cnt.valid_milestone("0") == 0 and cnt.valid_milestone("100") == 100
    for bad in ("5", "10001", "abc", "", "-10"):
        assert cnt.valid_milestone(bad) is None


# ── the rules ────────────────────────────────────────────────────────────

def _s(**kw):
    base = {"current": 0, "high_score": 0, "last_user_id": None, "saves": 1, "total_correct": 0, "total_ruined": 0}
    base.update(kw)
    return base


def test_counting_up_takes_turns_and_tracks_best():
    s, o = cnt.step(_s(), 1, 1)
    assert o.kind == "ok" and o.new_record and s["current"] == 1 and s["last_user_id"] == 1
    s, o = cnt.step(s, 2, 2)
    assert s["current"] == 2 and s["high_score"] == 2 and s["total_correct"] == 2


def test_wrong_number_or_same_person_ruins_the_run_but_keeps_the_best():
    s = _s(current=9, high_score=20, last_user_id=1)
    new, o = cnt.step(s, 2, 12)
    assert o.kind == "ruined" and o.reason == "wrong" and o.lost == 9
    assert new["current"] == 0 and new["last_user_id"] is None and new["high_score"] == 20 and new["total_ruined"] == 1
    new, o = cnt.step(s, 1, 10)                                  # right number, same person
    assert o.kind == "ruined" and o.reason == "same_user"


def test_a_wrong_first_number_is_only_a_hint():
    new, o = cnt.step(_s(), 1, 5)
    assert o.kind == "hint" and new["current"] == 0 and new["total_ruined"] == 0


def test_saves_spend_instead_of_ruining_and_are_earned_back():
    s = _s(current=40, high_score=40, last_user_id=1, saves=1)
    new, o = cnt.step(s, 2, 99, saves_on=True)
    assert o.kind == "saved" and new["current"] == 40 and new["saves"] == 0 and new["last_user_id"] == 1
    _, o = cnt.step(new, 2, 99, saves_on=True)                   # none left: now it ruins
    assert o.kind == "ruined"
    s = _s(current=49, last_user_id=1, saves=0, high_score=49)
    new, o = cnt.step(s, 2, 50, saves_on=True)
    assert o.earned_save and new["saves"] == 1
    new, o = cnt.step(_s(current=49, last_user_id=1, saves=cnt.SAVES_MAX), 2, 50, saves_on=True)
    assert not o.earned_save and new["saves"] == cnt.SAVES_MAX    # capped
    new, o = cnt.step(_s(current=49, last_user_id=1, saves=0), 2, 50, saves_on=False)
    assert not o.earned_save                                      # saves off: nothing earned


def test_milestones():
    _, o = cnt.step(_s(current=99, last_user_id=1, high_score=99), 2, 100, milestone_every=100)
    assert o.milestone
    _, o = cnt.step(_s(current=99, last_user_id=1, high_score=99), 2, 100, milestone_every=0)
    assert not o.milestone


def test_texts():
    t = cnt.outcome_text("Kofi", cnt.Outcome("ruined", 0, "wrong", lost=12), 12, 30)
    assert "Kofi" in t and "13" in t and "12" in t and "30" in t
    assert "twice" in cnt.outcome_text("Ama", cnt.Outcome("ruined", 0, "same_user", lost=3), 3, 3)
    assert cnt.outcome_text("x", cnt.Outcome("ok", 1), 0, 1) is None
    free = cnt.stats_text({"current": 4, "high_score": 9}, [(i, 10 - i, 1) for i in range(1, 9)], premium=False)
    prem = cnt.stats_text({"current": 4, "high_score": 9, "saves_on": True}, [(i, 10 - i, 1) for i in range(1, 9)], premium=True)
    assert free.count("<@") == cnt.FREE_TOP and prem.count("<@") == 8 and "Saves" in prem and "Premium" in free


# ── More features button ─────────────────────────────────────────────────

def test_more_features_lists_counting_with_a_routable_persistent_button():
    for clone in (None, 7):
        view = jd.build_join_dm_more_view(123, clone)
        kids = list(view.walk_children())
        assert len(kids) <= 40 and view.timeout is None and view.to_components()
        text = "\n".join(k.content for k in kids if isinstance(k, discord.ui.TextDisplay))
        assert "Counting" in text and "Free" in text and "Premium" in text
        cid = f"join_dm_counting:123:{'-' if clone is None else clone}"
        ids = [getattr(k, "custom_id", "") for k in kids]
        assert cid in ids and all(len(i) <= 100 for i in ids if i)
    assert jd._CountingButton in jd.DYNAMIC_ITEMS
    m = jd._CountingButton.__discord_ui_compiled_template__.match("join_dm_counting:123:7")
    item = run(jd._CountingButton.from_custom_id(None, None, m))
    assert (item.guild_id, item.clone_id) == (123, 7)


def _guild():
    ch = MagicMock(id=555)
    perms = SimpleNamespace(view_channel=True, send_messages=True, add_reactions=True, read_message_history=True)
    ch.permissions_for = lambda me: perms
    ch.mention = "<#555>"
    g = MagicMock(id=1)
    g.me = object()
    g.get_channel = lambda cid: ch if cid == 555 else None
    return g


def test_setup_panel_builds_for_free_and_premium_and_items_are_persistent():
    g = _guild()
    cfg = {"guild_id": 1, "channel_id": 555, "enabled": True, "current": 3, "high_score": 8, "milestone_every": 100,
           "math_on": True, "saves_on": False}
    for premium in (False, True):
        view = cc.build_setup_view(g, cfg, premium, note="hi")
        kids = list(view.walk_children())
        assert view.timeout is None and view.to_components() and len(kids) <= 40
        ids = {getattr(k, "custom_id", "") for k in kids}
        assert {"count:channel", "count:toggle", "count:stats", "count:reset", "count:math", "count:saves",
                "count:milestone"} <= ids
    free_text = "\n".join(k.content for k in cc.build_setup_view(g, cfg, False).walk_children() if isinstance(k, discord.ui.TextDisplay))
    assert "Premium perks" in free_text and "Maths counting" in free_text
    for cls in cc.DYNAMIC_ITEMS:
        assert cls.__discord_ui_compiled_template__.match(next(
            i for i in ids if cls.__discord_ui_compiled_template__.match(i)))


# ── the channel listener ─────────────────────────────────────────────────

def _cog(cfg_extra=None, premium=False):
    cfg = {"guild_id": 1, "clone_id": None, "enabled": True, "channel_id": 555, "math_on": False, "saves_on": False,
           "milestone_every": 0, "current": 0, "high_score": 0, "last_user_id": None, "saves": 1,
           "total_correct": 0, "total_ruined": 0}
    cfg.update(cfg_extra or {})
    cog = cc.CountingCog(SimpleNamespace(clone_id=None))
    cog._cfg[555] = cfg
    cog._premium = AsyncMock(return_value=premium)
    return cog, cfg


def _msg(text, uid):
    m = MagicMock()
    m.guild = SimpleNamespace(id=1)
    m.channel = MagicMock(id=555)
    m.channel.send = AsyncMock()
    m.author = SimpleNamespace(id=uid, bot=False, display_name=f"User{uid}")
    m.content = text
    m.add_reaction = AsyncMock()
    return m


def test_listener_counts_reacts_and_stores(monkeypatch):
    rec = AsyncMock()
    monkeypatch.setattr(cc.store, "record", rec)
    cog, cfg = _cog()
    m1, m2, chat = _msg("1", 10), _msg("2", 11), _msg("hello there", 12)
    for m in (m1, m2, chat):
        run(cog.on_message(m))
    m1.add_reaction.assert_awaited_with("✅")
    m2.add_reaction.assert_awaited_with("✅")
    chat.add_reaction.assert_not_awaited()                        # chat is ignored
    assert cfg["current"] == 2 and cfg["last_user_id"] == 11 and rec.await_count == 2


def test_listener_ruin_posts_a_message_and_resets(monkeypatch):
    monkeypatch.setattr(cc.store, "record", AsyncMock())
    cog, cfg = _cog({"current": 5, "high_score": 5, "last_user_id": 10})
    bad = _msg("9", 11)
    run(cog.on_message(bad))
    bad.add_reaction.assert_awaited_with("❌")
    assert "ruined it at **5**" in bad.channel.send.await_args.args[0]
    assert cfg["current"] == 0 and cfg["high_score"] == 5


def test_listener_other_channels_and_bots_are_ignored(monkeypatch):
    rec = AsyncMock()
    monkeypatch.setattr(cc.store, "record", rec)
    cog, cfg = _cog()
    other = _msg("1", 10)
    other.channel = MagicMock(id=999)
    bot = _msg("1", 10)
    bot.author.bot = True
    for m in (other, bot):
        run(cog.on_message(m))
    assert cfg["current"] == 0 and rec.await_count == 0


def test_premium_extras_only_apply_while_premium(monkeypatch):
    monkeypatch.setattr(cc.store, "record", AsyncMock())
    cog, cfg = _cog({"math_on": True, "saves_on": True, "milestone_every": 10, "current": 5, "last_user_id": 1,
                     "high_score": 5}, premium=False)
    run(cog.on_message(_msg("3+3", 2)))
    assert cfg["current"] == 5                                    # lapsed Premium: maths is just chat
    cog, cfg = _cog({"math_on": True, "saves_on": True, "milestone_every": 10, "current": 5, "last_user_id": 1,
                     "high_score": 5}, premium=True)
    m = _msg("3+3", 2)
    run(cog.on_message(m))
    assert cfg["current"] == 6
    wrong = _msg("20", 3)
    run(cog.on_message(wrong))
    assert cfg["current"] == 6 and cfg["saves"] == 0              # a save was spent instead of ruining the run
    assert "save kept the run alive" in wrong.channel.send.await_args.args[0]


def test_apply_config_keeps_the_live_run_and_moves_channels():
    cog, cfg = _cog({"current": 7, "high_score": 9})
    fresh = {**cfg, "current": 0, "high_score": 0, "channel_id": 777}
    out = cog.apply_config(fresh)
    assert out["current"] == 7 and out["high_score"] == 9         # the in-memory run wins over an older row
    assert 777 in cog._cfg and 555 not in cog._cfg
    off = cog.apply_config({**fresh, "enabled": False})
    assert not cog._cfg and off["guild_id"] == 1


# ── anime spawn: total followers ─────────────────────────────────────────

def test_followers_label():
    assert af.followers_label(None) == "" and af.followers_label(0) == "" and af.followers_label("x") == ""
    assert af.followers_label(950) == "950" and af.followers_label(1000) == "1K"
    assert af.followers_label(84_500) == "84.5K" and af.followers_label(1_234_567) == "1.2M"


def test_provider_followers_reach_the_spawn_card():
    payload = {"data": {"Page": {"media": [{"id": 1, "title": {"english": "Show"}, "popularity": 321_456, "episodes": 12},
                                            {"id": 2, "title": {"english": "Quiet"}}]}}}
    a, b = ab.parse_entries(payload)
    assert a.followers == 321_456 and b.followers is None
    assert "popularity" in ab._MEDIA
    card = vaf.card_text(spawn.to_preview(a))
    assert "👥 321.5K following" in card and "12 episodes" in card
    assert "following" not in vaf.card_text(spawn.to_preview(b))  # unknown total: no line, no crash


def test_real_episode_posts_are_unchanged():
    p = af.Preview(1, "Show", 3, 2_000_000_000, "s", None, "https://anilist.co/anime/1", followers=5000)
    assert "following" not in vaf.card_text(p)


# ── quick-start / join DM guide entry ────────────────────────────────────

def test_counting_is_in_the_join_dm_guide_and_opens_its_panel(monkeypatch):
    label, emoji, handler, opts, blurb = jd.FEATURE_TOGGLES["counting"]
    assert label == "Counting" and opts is None and "Free" in blurb
    keys = list(jd.FEATURE_TOGGLES)
    assert keys.index("counting") == keys.index("birthdays") + 1
    # the guide page that holds it still builds within Discord's limits
    page = (keys.index("counting")) // jd.FEATURES_PER_PAGE
    view = jd.JoinDMLayoutView(1, None, keys, page, "intro")
    assert view.to_components() and len(list(view.walk_children())) <= 40

    guild = MagicMock(id=1)
    sent = MagicMock()
    sent.send = AsyncMock()
    ch = MagicMock(mention="<#9>")
    ch.send = sent.send
    monkeypatch.setattr(jd, "_default_text_channel", lambda g: ch)
    monkeypatch.setattr(cc.store, "get_config", AsyncMock(return_value={
        "guild_id": 1, "channel_id": None, "enabled": False, "current": 0, "high_score": 0}))
    monkeypatch.setattr("discord_bot.cogs.birthdays._is_premium", AsyncMock(return_value=False))
    guild.get_channel = lambda cid: None
    i = MagicMock()
    i.followup.send = AsyncMock()
    out = run(handler(i, guild, None))
    assert out == (None, None)                                    # never flips to "On:"
    ch.send.assert_awaited_once()
    i.followup.send.assert_awaited_once()
