"""Daily XP + invite leaderboard autoposts: crash-proof loops, one shared post path
(used by both the loop and the Post-now button), honest failure reasons."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

LB = "discord_bot.cogs._views_leveling_leaderboard"
WZ = "discord_bot.cogs._views_leveling_wizard"
INV = "discord_bot.cogs._views_invites"
INVC = "discord_bot.cogs.invites"
LVC = "discord_bot.cogs.leveling"
GUILD, CHAN = 5000, 6000


class FakeDB:
    def __init__(self):
        self.lvl_cfg = {"guild_id": GUILD, "clone_id": None, "announce_channel_id": CHAN,
                        "leaderboard_autopost_channel_id": None}
        self.inv_cfg = {"guild_id": GUILD, "clone_id": None, "enabled": True, "channel_id": CHAN,
                        "leaderboard_autopost_channel_id": None, "leaderboard_last_posted_at": None}
        self.rows = [(11, 5, 4), (12, 3, 3)]
        self.boom = False
        self.marked_lvl, self.marked_inv, self.set_calls = [], [], []
        self.due_lvl, self.due_inv = [], []

    def _chk(self):
        if self.boom:
            raise RuntimeError("db down")

    async def get_leveling_config(self, g, clone_id=None): self._chk(); return dict(self.lvl_cfg)
    async def get_invite_tracker_config(self, g, clone_id=None): self._chk(); return dict(self.inv_cfg)
    async def get_invite_leaderboard(self, g, clone_id=None, limit=10): self._chk(); return list(self.rows)
    async def mark_leaderboard_posted(self, g, c): self._chk(); self.marked_lvl.append(g)
    async def mark_invite_leaderboard_posted(self, g, c): self._chk(); self.marked_inv.append(g)
    async def get_due_leaderboard_autoposts(self, clone_id, limit=10): self._chk(); return list(self.due_lvl)
    async def get_due_invite_leaderboard_autoposts(self, clone_id, limit=10): self._chk(); return list(self.due_inv)

    async def set_invite_leaderboard_autopost_channel(self, g, c, ch):
        self._chk()
        self.set_calls.append(ch)
        self.inv_cfg["leaderboard_autopost_channel_id"] = ch


@pytest.fixture()
def env(monkeypatch):
    fake = FakeDB()
    dbm = types.ModuleType("database"); dbm.db = fake; dbm.get_pool = AsyncMock()
    monkeypatch.setitem(sys.modules, "database", dbm)
    cfg = types.ModuleType("config")
    for n, v in (("DISCORD_CLONE_ADMIN_IDS", {1}), ("PREMIUM_FEE_USD", 5.0)):
        setattr(cfg, n, v)
    mods = {}
    for name in (LB, WZ, INV, INVC, LVC):
        sys.modules.pop(name, None)
    try:
        for key, name in (("lb", LB), ("wz", WZ), ("inv", INV), ("invc", INVC), ("lvc", LVC)):
            mods[key] = importlib.import_module(name)
    except Exception as e:                      # heavy imports unavailable in this sandbox
        pytest.skip(f"module import needs more of the repo: {e!r}")
    mods["db"] = fake
    yield types.SimpleNamespace(**mods)
    for name in (LB, WZ, INV, INVC, LVC):
        sys.modules.pop(name, None)


def run(c): return asyncio.run(c)


def perms(view=True, send=True):
    p = MagicMock(); p.view_channel, p.send_messages = view, send
    return p


def make_guild(channel="ok", view=True, send=True):
    g = MagicMock(); g.id = GUILD; g.name = "My *Guild*"
    ch = None
    if channel == "ok":
        ch = MagicMock(); ch.mention = f"<#{CHAN}>"; ch.send = AsyncMock()
        ch.permissions_for = MagicMock(return_value=perms(view, send))
    elif channel == "novoice":
        ch = MagicMock(spec=["mention", "permissions_for"]); ch.mention = "#cat"
    g.get_channel = MagicMock(return_value=ch)
    return g, ch


def forbidden():
    return discord.Forbidden(MagicMock(status=403), "no")


# ── XP: shared post path ─────────────────────────────────────────────────

def lb_post(env, monkeypatch, guild, view="VIEW", build_exc=None):
    async def fake_build(bot, g, clone_id, mode="local", page=0, stats_for_user_id=None):
        if build_exc: raise build_exc
        return view
    monkeypatch.setattr(env.lb, "build_leaderboard_view", fake_build)
    return run(env.lb.post_leaderboard_to_channel(MagicMock(), guild, None, CHAN))


def test_xp_post_success_sends_view_without_pings(env, monkeypatch):
    g, ch = make_guild()
    ok, reason = lb_post(env, monkeypatch, g)
    assert ok and reason == ""
    kw = ch.send.await_args.kwargs
    assert kw["view"] == "VIEW" and kw["allowed_mentions"].users is False and kw["allowed_mentions"].everyone is False


@pytest.mark.parametrize("kind,needle", [("none", "can't find"), ("novoice", "isn't a text channel")])
def test_xp_post_missing_or_non_text_channel(env, monkeypatch, kind, needle):
    g, ch = make_guild(kind)
    ok, reason = lb_post(env, monkeypatch, g)
    assert not ok and needle in reason


def test_xp_post_missing_permissions_names_them(env, monkeypatch):
    g, ch = make_guild(send=False)
    ok, reason = lb_post(env, monkeypatch, g)
    assert not ok and "Send Messages" in reason and "View Channel" not in reason
    ch.send.assert_not_awaited()


def test_xp_post_nobody_has_xp(env, monkeypatch):
    g, ch = make_guild()
    ok, reason = lb_post(env, monkeypatch, g, view=None)
    assert not ok and "XP" in reason
    ch.send.assert_not_awaited()


def test_xp_post_discord_forbidden_and_other_errors_never_raise(env, monkeypatch):
    g, ch = make_guild(); ch.send.side_effect = forbidden()
    assert "refused" in lb_post(env, monkeypatch, g)[1]
    g, ch = make_guild(); ch.send.side_effect = RuntimeError("x")
    assert lb_post(env, monkeypatch, g)[0] is False
    g, ch = make_guild()
    ok, reason = lb_post(env, monkeypatch, g, build_exc=RuntimeError("db"))
    assert not ok and "database" in reason


# ── XP: the loop can't die ───────────────────────────────────────────────

def cog_for(env, guilds):
    bot = MagicMock(); bot.clone_id = None
    bot.get_guild = MagicMock(side_effect=lambda gid: guilds.get(gid))
    cog = env.lvc.LevelingCog.__new__(env.lvc.LevelingCog)
    cog.bot = bot
    return cog


def test_loop_body_survives_a_database_error(env):
    env.db.boom = True
    cog = cog_for(env, {})
    run(env.lvc.LevelingCog._leaderboard_autopost_loop.coro(cog))        # must not raise


def test_one_bad_guild_does_not_stop_the_others_and_all_get_marked(env, monkeypatch):
    g1, _ = make_guild(); g2, ch2 = make_guild()
    g1.id, g2.id = 1, 2
    env.db.due_lvl = [{"guild_id": 1, "clone_id": None, "post_channel_id": CHAN},
                      {"guild_id": 2, "clone_id": None, "post_channel_id": CHAN}]
    calls = []
    async def fake_post(bot, guild, clone_id, channel_id):
        calls.append(guild.id)
        if guild.id == 1:
            raise RuntimeError("kaboom")
        return True, ""
    monkeypatch.setattr(env.lb, "post_leaderboard_to_channel", fake_post)
    cog = cog_for(env, {1: g1, 2: g2})
    run(env.lvc.LevelingCog._leaderboard_autopost_loop.coro(cog))
    assert calls == [1, 2] and env.db.marked_lvl == [1, 2]


def test_guild_owned_by_another_process_is_skipped_not_marked(env, monkeypatch):
    env.db.due_lvl = [{"guild_id": 9, "clone_id": None, "post_channel_id": CHAN}]
    cog = cog_for(env, {})
    run(env.lvc.LevelingCog._leaderboard_autopost_loop.coro(cog))
    assert env.db.marked_lvl == []


def test_error_handler_defers_restart_instead_of_a_no_op_start(env):
    cog = cog_for(env, {})
    restarted = []
    cog._restart_leaderboard_autopost = lambda: restarted.append(1)
    async def go():
        await env.lvc.LevelingCog._leaderboard_autopost_loop_error(cog, RuntimeError("x"))
        await asyncio.sleep(0)
        return list(restarted)
    assert run(go()) == []          # not called inline (the task is still 'running' at that moment)


# ── XP: Post-now button ──────────────────────────────────────────────────

def interaction(guild):
    i = MagicMock(); i.user.id = 77
    i.client = MagicMock(); i.client.get_guild = MagicMock(return_value=guild)
    i.guild = guild
    i.response.defer = AsyncMock(); i.followup.send = AsyncMock()
    return i


def xp_button(env, monkeypatch, access=True):
    monkeypatch.setattr(env.wz, "_check_access", AsyncMock(return_value=access))
    return env.wz.LevelingLeaderboardPostNowButton(GUILD, None, 77)


def sent(i): return i.followup.send.await_args.args[0]


def test_xp_post_now_success_marks_posted(env, monkeypatch):
    g, ch = make_guild()
    monkeypatch.setattr(env.lb, "post_leaderboard_to_channel", AsyncMock(return_value=(True, "")))
    i = interaction(g)
    run(xp_button(env, monkeypatch).callback(i))
    assert env.db.marked_lvl == [GUILD] and "Posted" in sent(i)


def test_xp_post_now_reports_the_reason_and_does_not_mark(env, monkeypatch):
    g, ch = make_guild()
    monkeypatch.setattr(env.lb, "post_leaderboard_to_channel", AsyncMock(return_value=(False, "I'm missing **Send Messages**")))
    i = interaction(g)
    run(xp_button(env, monkeypatch).callback(i))
    assert env.db.marked_lvl == [] and "Send Messages" in sent(i)


def test_xp_post_now_when_off_or_no_channel(env, monkeypatch):
    g, _ = make_guild()
    env.db.lvl_cfg["leaderboard_autopost_channel_id"] = -1
    i = interaction(g); run(xp_button(env, monkeypatch).callback(i))
    assert "turned off" in sent(i)
    env.db.lvl_cfg.update(leaderboard_autopost_channel_id=None, announce_channel_id=None)
    i = interaction(g); run(xp_button(env, monkeypatch).callback(i))
    assert "no channel" in sent(i).lower()


def test_xp_post_now_denied_does_nothing_and_db_error_is_handled(env, monkeypatch):
    g, _ = make_guild()
    post = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(env.lb, "post_leaderboard_to_channel", post)
    i = interaction(g); run(xp_button(env, monkeypatch, access=False).callback(i))
    post.assert_not_awaited()
    env.db.boom = True
    i = interaction(g); run(xp_button(env, monkeypatch).callback(i))
    assert "Nothing was posted" in sent(i) and post.await_count == 0


def test_xp_wizard_has_the_button_within_discord_limits(env):
    cfg = {"leaderboard_autopost_channel_id": None, "announce_channel_id": CHAN, "xp_rate": "default", "card_style": "card"}
    view = env.wz.build_wizard_view(GUILD, None, None, cfg, [])
    labels = {getattr(c, "label", None) for c in view.walk_children() if isinstance(getattr(c, "item", c), discord.ui.Button)}
    labels |= {getattr(getattr(c, "item", c), "label", None) for c in view.walk_children()}
    assert "Post it now (test)" in labels
    assert len(list(view.walk_children())) <= 40
    assert env.wz.LevelingLeaderboardPostNowButton in env.wz.DYNAMIC_ITEMS
    view.to_components()


# ── invites ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,expected", [
    ({"leaderboard_autopost_channel_id": -1, "channel_id": 5, "enabled": True}, None),
    ({"leaderboard_autopost_channel_id": 9, "channel_id": 5, "enabled": False}, 9),
    ({"leaderboard_autopost_channel_id": None, "channel_id": 5, "enabled": True}, 5),
    ({"leaderboard_autopost_channel_id": None, "channel_id": 5, "enabled": False}, None),
    ({"leaderboard_autopost_channel_id": None, "channel_id": None, "enabled": True}, None),
])
def test_invite_channel_resolution(env, cfg, expected):
    assert env.inv.resolve_leaderboard_channel(cfg) == expected


def inv_post(env, guild, rows=None):
    if rows is not None:
        env.db.rows = rows
    return run(env.inv.post_invite_leaderboard(MagicMock(), guild, None, CHAN))


def test_invite_post_success_no_pings_and_valid_components(env):
    g, ch = make_guild()
    ok, _ = inv_post(env, g)
    assert ok
    kw = ch.send.await_args.kwargs
    assert kw["allowed_mentions"].users is False
    view = kw["view"]
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "<@11>" in text and "**4** invites, 1 left" in text
    assert "<@12>" in text and "🥈" in text
    view.to_components()


def test_invite_view_shows_left_counts_and_escapes_guild_name(env):
    view = env.inv.build_invite_leaderboard_view("a_b*c", [(1, 5, 3)])
    text = "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert "2 left" in text and "a\\_b\\*c" in text


def test_invite_post_failure_reasons(env):
    g, ch = make_guild("none")
    assert "can't find" in inv_post(env, g)[1]
    g, ch = make_guild(send=False)
    assert "Send Messages" in inv_post(env, g)[1]
    g, ch = make_guild()
    assert "trackable invite" in inv_post(env, g, rows=[])[1]
    ch.send.assert_not_awaited()
    g, ch = make_guild(); ch.send.side_effect = forbidden()
    assert "refused" in inv_post(env, g, rows=[(1, 1, 1)])[1]
    env.db.boom = True
    g, ch = make_guild()
    ok, reason = inv_post(env, g)
    assert not ok and "database" in reason


def inv_cog(env, guilds):
    bot = MagicMock(); bot.clone_id = None
    bot.get_guild = MagicMock(side_effect=lambda gid: guilds.get(gid))
    cog = env.invc.InvitesCog.__new__(env.invc.InvitesCog)
    cog.bot = bot
    return cog


def test_invite_loop_runs_leaderboards_even_when_the_wizard_pass_fails(env, monkeypatch):
    g, _ = make_guild(); g.id = 1
    env.db.due_inv = [{"guild_id": 1, "clone_id": None, "post_channel_id": CHAN}]
    monkeypatch.setattr(env.db, "get_due_invite_wizard_guilds", AsyncMock(side_effect=RuntimeError("boom")), raising=False)
    post = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(env.inv, "post_invite_leaderboard", post)
    cog = inv_cog(env, {1: g})
    run(env.invc.InvitesCog._scheduler_loop.coro(cog))            # must not raise
    post.assert_awaited_once()
    assert env.db.marked_inv == [1]


def test_invite_loop_survives_db_error_and_marks_failures_once(env, monkeypatch):
    env.db.boom = True
    run(env.invc.InvitesCog._scheduler_loop.coro(inv_cog(env, {})))      # no raise
    env.db.boom = False
    g, _ = make_guild(); g.id = 1
    env.db.due_inv = [{"guild_id": 1, "clone_id": None, "post_channel_id": CHAN}]
    monkeypatch.setattr(env.db, "get_due_invite_wizard_guilds", AsyncMock(return_value=[]), raising=False)
    monkeypatch.setattr(env.inv, "post_invite_leaderboard", AsyncMock(return_value=(False, "no perms")))
    run(env.invc.InvitesCog._scheduler_loop.coro(inv_cog(env, {1: g})))
    assert env.db.marked_inv == [1]


def test_invite_wizard_has_new_buttons_and_stays_within_limits(env):
    cfg = {"enabled": True, "channel_id": CHAN, "leaderboard_autopost_channel_id": None}
    view = env.inv.build_wizard_view(GUILD, None, 77, cfg)
    labels = {getattr(getattr(c, "item", c), "label", None) for c in view.walk_children()}
    assert {"Post it now (test)", "Turn daily post off"} <= labels
    assert len(list(view.walk_children())) <= 40
    for row in (c for c in view.walk_children() if isinstance(c, discord.ui.ActionRow)):
        assert len(row.children) <= 5
    view.to_components()
    off = env.inv.build_wizard_view(GUILD, None, 77, dict(cfg, leaderboard_autopost_channel_id=-1))
    assert "Turn daily post on" in {getattr(getattr(c, "item", c), "label", None) for c in off.walk_children()}
    assert {env.inv.InviteDailyToggleButton, env.inv.InvitePostNowButton} <= set(env.inv.DYNAMIC_ITEMS)


def inv_button(env, monkeypatch, cls, *extra, access=True):
    monkeypatch.setattr(env.inv, "_check_access", AsyncMock(return_value=access))
    monkeypatch.setattr(env.inv, "_rerender", AsyncMock())
    return cls(GUILD, None, 77, *extra)


def test_invite_daily_toggle_flips_off_and_back_on(env, monkeypatch):
    g, _ = make_guild(); i = interaction(g)
    run(inv_button(env, monkeypatch, env.inv.InviteDailyToggleButton, {}).callback(i))
    assert env.db.set_calls == [-1]
    run(inv_button(env, monkeypatch, env.inv.InviteDailyToggleButton, {}).callback(interaction(g)))
    assert env.db.set_calls == [-1, None]


def test_invite_daily_toggle_denied_or_db_error_changes_nothing(env, monkeypatch):
    g, _ = make_guild()
    run(inv_button(env, monkeypatch, env.inv.InviteDailyToggleButton, {}, access=False).callback(interaction(g)))
    assert env.db.set_calls == []
    env.db.boom = True
    i = interaction(g)
    run(inv_button(env, monkeypatch, env.inv.InviteDailyToggleButton, {}).callback(i))
    assert env.db.set_calls == [] and "Nothing was changed" in sent(i)


def test_invite_post_now_paths(env, monkeypatch):
    g, _ = make_guild()
    post = AsyncMock(return_value=(True, ""))
    monkeypatch.setattr(env.inv, "post_invite_leaderboard", post)
    i = interaction(g); run(inv_button(env, monkeypatch, env.inv.InvitePostNowButton).callback(i))
    assert env.db.marked_inv == [GUILD] and "Posted" in sent(i)
    post.return_value = (False, "I'm missing **View Channel**")
    env.db.marked_inv.clear()
    i = interaction(g); run(inv_button(env, monkeypatch, env.inv.InvitePostNowButton).callback(i))
    assert env.db.marked_inv == [] and "View Channel" in sent(i)
    env.db.inv_cfg["leaderboard_autopost_channel_id"] = -1
    i = interaction(g); run(inv_button(env, monkeypatch, env.inv.InvitePostNowButton).callback(i))
    assert "turned off" in sent(i)
    env.db.inv_cfg.update(leaderboard_autopost_channel_id=None, channel_id=None)
    i = interaction(g); run(inv_button(env, monkeypatch, env.inv.InvitePostNowButton).callback(i))
    assert "no channel" in sent(i).lower()
    i = interaction(g); run(inv_button(env, monkeypatch, env.inv.InvitePostNowButton, access=False).callback(i))
    i.followup.send.assert_not_awaited()


def test_schema_bumped_and_columns_added_idempotently():
    from pathlib import Path
    import re
    src = (Path(__file__).resolve().parents[2] / "database.py").read_text()
    assert int(re.search(r'^SCHEMA_VERSION = "(\d+)"', src, re.M).group(1)) >= 41
    for col in ("leaderboard_autopost_channel_id BIGINT", "leaderboard_last_posted_at TIMESTAMPTZ"):
        assert f"ALTER TABLE discord_invite_tracker_config ADD COLUMN IF NOT EXISTS {col}" in src


def test_xp_due_query_lets_the_default_null_setting_through():
    """Regression: the query used COALESCE(col, -1) != -1, which turns the default
    NULL into -1 and excludes it, so only servers that explicitly picked a channel
    ever got the daily post. NULL means 'use the announce channel' and must pass."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "database.py").read_text()
    a = src.index("async def get_due_leaderboard_autoposts")
    body = src[a:src.index("async def mark_leaderboard_posted")]
    assert "COALESCE(leaderboard_autopost_channel_id, -1) != -1" not in body
    assert "COALESCE(leaderboard_autopost_channel_id, 0) != -1" in body
