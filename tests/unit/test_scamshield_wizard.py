"""/scamshield wizard: strict mode (premium), deep report, free tools and the premium gating."""
import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from modules import scam_shield as ss
from discord_bot.cogs import _views_scamshield_wizard as wiz

GUILD = 7000


def run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ss._c.words, ss._c.domains, ss._c.images = [], [], []
    ss._c.strict = set()
    ss._c.enabled = True
    ss._c.loaded_at = 10 ** 12
    ss._last_hit.clear()
    monkeypatch.setattr(ss.time, "monotonic", lambda: 10 ** 12)
    yield


# ── strict heuristics ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "free nitro https://discord-gift.xyz/abc",
    "login https://discord.com.verify-login.xyz/x",
    "https://steamcommunity.com.trade-offer.ru",
    "claim your reward now http://random-site.io/go",
    "airdrop live! https://cool-airdrop.net",
    "https://discordnitro.club",
    "https://steam-claim.com/gift",
    "https://discord.com@evil.xyz/login",                  # the '@' disguise: the real host is evil.xyz
    "https://discord.gg@evil.xyz",
])
def test_strict_catches_lookalikes_fake_subdomains_and_link_bait(text):
    hit = ss.match_strict(text)
    assert hit and hit[0] == "strict" and hit[2] is None


@pytest.mark.parametrize("text", [
    "check https://discord.com/channels/1/2/3",
    "gift https://discord.gift/abc123",
    "join https://discord.gg/abc",
    "docs https://discord.js.org/docs and https://discordjs.guide",
    "list https://discordbotlist.com/bots",
    "never share your seed phrase with anyone",          # bait phrase but no link
    "free nitro would be nice lol",
    "see index.html and node.js",                          # bare words are never treated as links
    "watch https://youtube.com/watch?v=1",
    "https://store.steampowered.com/app/10",
    "https://cdn.discordapp.com/avatars/1/a.gif?size=512",
])
def test_strict_leaves_normal_links_alone(text):
    assert ss.match_strict(text) is None


def test_strict_respects_the_servers_allowed_domains():
    assert ss.match_strict("free nitro https://my-site.org") is not None
    assert ss.match_strict("free nitro https://my-site.org", allowed=("my-site.org",)) is None


def test_check_text_runs_normal_rules_first_and_strict_only_when_asked():
    ss._c.words = [(1, "fatawin")]
    assert ss.check_text("fatawin", strict=False)[0] == "word"
    assert ss.check_text("free nitro https://x-site.io", strict=False) is None
    assert ss.check_text("free nitro https://x-site.io", strict=True)[0] == "strict"


def test_strict_key_round_trips_and_bad_keys_are_ignored():
    assert ss._parse_strict_key(ss.strict_key(5, None)) == (5, None)
    assert ss._parse_strict_key(ss.strict_key(5, 12)) == (5, 12)
    assert ss._parse_strict_key("strict:nonsense") is None


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return Ctx()


def test_set_strict_stores_and_updates_memory(monkeypatch):
    conn = MagicMock(); conn.execute = AsyncMock()
    monkeypatch.setattr(ss, "_pool", AsyncMock(return_value=FakePool(conn)))
    run(ss.set_strict(GUILD, None, True))
    assert ss.strict_on(GUILD, None) and "INSERT" in conn.execute.await_args.args[0]
    assert conn.execute.await_args.args[1] == ss.strict_key(GUILD, None)
    run(ss.set_strict(GUILD, None, False))
    assert not ss.strict_on(GUILD, None) and "DELETE" in conn.execute.await_args.args[0]


def test_strict_is_per_server_and_per_clone():
    ss._c.strict = {(GUILD, None)}
    assert ss.strict_on(GUILD, None) and not ss.strict_on(GUILD, 3) and not ss.strict_on(GUILD + 1, None)


# ── reports ──────────────────────────────────────────────────────────────

def test_deep_report_returns_empty_when_the_database_fails(monkeypatch):
    monkeypatch.setattr(ss, "_pool", AsyncMock(side_effect=RuntimeError("db down")))
    assert run(ss.deep_report(GUILD)) == {}
    assert run(ss.recent_guild_hits(GUILD)) == []


def test_deep_report_shapes_the_rows(monkeypatch):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"total": 3, "deleted": 2, "users": 2, "last": now})

    async def fetch(sql, *a):
        if "GROUP BY kind" in sql:
            return [{"kind": "domain", "n": 2}, {"kind": "image", "n": 1}]
        if "GROUP BY matched" in sql:
            return [{"matched": "dwinble.com", "n": 2}]
        if "GROUP BY channel_id" in sql:
            return [{"channel_id": 9, "n": 3}]
        if "GROUP BY user_id" in sql:
            return [{"user_id": 42, "n": 2}]
        return [{"d": date(2026, 10, 9), "n": 3}]
    conn.fetch = fetch
    monkeypatch.setattr(ss, "_pool", AsyncMock(return_value=FakePool(conn)))
    rep = run(ss.deep_report(GUILD, None, 30))
    assert rep["total"] == 3 and rep["deleted"] == 2 and rep["users"] == 2
    assert rep["kinds"] == [("domain", 2), ("image", 1)] and rep["channels"] == [(9, 3)]
    assert rep["daily"] == [(date(2026, 10, 9), 3)]
    text = wiz.deep_text(rep)
    assert "3** caught" in text and "dwinble.com" in text and "<#9>" in text and "<@42>" in text


def test_deep_text_handles_empty_and_failed_reports():
    assert "couldn't build" in wiz.deep_text({})
    assert "No scam messages" in wiz.deep_text({"days": 30, "total": 0})


def test_trend_fills_gaps_and_spark_scales():
    t = wiz.trend([(date(2026, 10, 10), 4), (date(2026, 10, 8), 2)], days=4, today=date(2026, 10, 10))
    assert t == [0, 2, 0, 4]
    assert wiz.spark(t)[-1] == "█" and wiz.spark([0, 0]) == "▁▁"


def test_recent_text():
    assert "Nothing has been caught" in wiz.recent_text([])
    row = {"user_id": 42, "kind": "domain", "matched": "dwinble.com", "deleted": True,
           "created_at": datetime(2026, 10, 10, tzinfo=timezone.utc)}
    out = wiz.recent_text([row])
    assert "<@42>" in out and "dwinble.com" in out and "deleted" in out


# ── panel ────────────────────────────────────────────────────────────────

def _labels(view):
    out = []

    def walk(item):
        if isinstance(item, discord.ui.DynamicItem):
            item = item.item                      # persistent buttons wrap the real Button
        if isinstance(item, discord.ui.Button):
            out.append((item.label, item.custom_id))
        for child in getattr(item, "children", []) or []:
            walk(child)
        if isinstance(item, discord.ui.TextDisplay):
            out.append((item.content, None))
    for c in view.children:
        walk(c)
    return out


def _guild(manage_messages=True):
    perms = SimpleNamespace(manage_messages=manage_messages)
    return SimpleNamespace(id=GUILD, me=SimpleNamespace(guild_permissions=perms), owner_id=1, get_member=lambda uid: None)


def _state(premium, strict=False, enabled=True, caught=3):
    return {"scam": {"cfg": {"enabled": enabled, "allowed_domains": ["a.com"]}, "caught": caught, "global_on": True},
            "premium": premium, "strict": strict}


def test_free_panel_shows_the_premium_pitch_and_locked_buttons():
    items = _labels(wiz.build_panel(_guild(), None, _state(False)))
    texts = " ".join(t for t, cid in items if cid is None)
    labels = [t for t, cid in items if cid]
    assert "Upgrade to Premium" in texts and "Strict Scam Shield" in texts and "Deep reports" in texts
    assert "Strict mode 🔒" in labels and "Deep report 🔒" in labels
    assert "Upgrade to Premium" in labels and "Turn off here" in labels
    assert {"Allow domains", "Check a link", "Recent catches", "How it works"} <= set(labels)


def test_premium_panel_unlocks_both_and_drops_the_pitch():
    items = _labels(wiz.build_panel(_guild(), None, _state(True, strict=True)))
    texts = " ".join(t for t, cid in items if cid is None)
    labels = [t for t, cid in items if cid]
    assert "Upgrade to Premium to unlock" not in texts and "Strict Scam Shield: 🟢" in texts
    assert "Deep report" in labels and "Strict: on — turn off" in labels and "Upgrade to Premium" not in labels


def test_panel_shows_a_missing_permission_and_the_off_state():
    items = _labels(wiz.build_panel(_guild(manage_messages=False), None, _state(False, enabled=False)))
    texts = " ".join(t for t, cid in items if cid is None)
    labels = [t for t, cid in items if cid]
    assert "Manage Messages" in texts and "🔴 **off**" in texts and "Turn on here" in labels


def test_every_custom_id_is_unique_and_matches_the_template_and_stays_short():
    for st in (_state(False), _state(True)):
        ids = [cid for _, cid in _labels(wiz.build_panel(_guild(), 12, st)) if cid]
        assert len(ids) == len(set(ids))
        for cid in ids:
            m = re.match(wiz._PAT, cid)
            assert m and m.group("guild") == str(GUILD) and m.group("clone") == "12" and len(cid) <= 100


# ── buttons ──────────────────────────────────────────────────────────────

def _interaction(manage=True, uid=42):
    perms = SimpleNamespace(manage_guild=manage)
    member = SimpleNamespace(id=uid, guild_permissions=perms)
    guild = SimpleNamespace(id=GUILD, owner_id=1, get_member=lambda u: member if u == uid else None)
    i = MagicMock()
    i.client.get_guild = lambda gid: guild if gid == GUILD else None
    i.user = SimpleNamespace(id=uid)
    state = {"done": False}
    i.response.is_done = lambda: state["done"]

    async def mark(*a, **k):
        state["done"] = True
    i.response.defer = AsyncMock(side_effect=mark)
    i.response.send_message = AsyncMock(side_effect=mark)
    i.response.send_modal = AsyncMock(side_effect=mark)
    i.followup.send = AsyncMock()
    i.edit_original_response = AsyncMock()
    return i


@pytest.fixture()
def env(monkeypatch):
    e = SimpleNamespace(premium=True)
    monkeypatch.setattr(wiz.pro, "is_premium", AsyncMock(side_effect=lambda g, c: e.premium))
    monkeypatch.setattr(wiz, "render", AsyncMock(return_value="VIEW"))
    pitch = AsyncMock()
    monkeypatch.setattr("discord_bot.cogs._views_premium.send_premium_pitch", pitch)
    e.pitch = pitch
    return e


def test_non_staff_cannot_press_anything(env):
    i = _interaction(manage=False)
    run(wiz.WizButton("deep", GUILD, None).callback(i))
    i.response.send_message.assert_awaited_once()
    assert "Manage Server" in i.response.send_message.await_args.args[0]
    env.pitch.assert_not_awaited()


@pytest.mark.parametrize("kind,what", [("strict", "Strict Scam Shield"), ("deep", "Deep reports"),
                                       ("upgrade", "Strict Scam Shield and Deep reports")])
def test_free_servers_get_the_premium_pitch_on_premium_buttons(env, monkeypatch, kind, what):
    env.premium = False
    set_strict = AsyncMock(); monkeypatch.setattr(ss, "set_strict", set_strict)
    deep = AsyncMock(); monkeypatch.setattr(ss, "deep_report", deep)
    i = _interaction()
    run(wiz.WizButton(kind, GUILD, None, premium=False).callback(i))
    env.pitch.assert_awaited_once()
    assert what in i.followup.send.await_args.args[0] and "premium" in i.followup.send.await_args.args[0]
    set_strict.assert_not_awaited(); deep.assert_not_awaited()


def test_premium_strict_button_turns_strict_on_then_off(env, monkeypatch):
    calls = []

    async def fake_set(g, c, on):
        calls.append(on)
        (ss._c.strict.add if on else ss._c.strict.discard)((g, c))
    monkeypatch.setattr(ss, "set_strict", fake_set)
    monkeypatch.setattr("modules.server_panel.record_change", AsyncMock())
    run(wiz.WizButton("strict", GUILD, None).callback(_interaction()))
    run(wiz.WizButton("strict", GUILD, None).callback(_interaction()))
    assert calls == [True, False]


def test_premium_deep_button_sends_the_report(env, monkeypatch):
    monkeypatch.setattr(ss, "deep_report", AsyncMock(return_value={"days": 30, "total": 0}))
    i = _interaction()
    run(wiz.WizButton("deep", GUILD, None).callback(i))
    assert "Deep report" in i.followup.send.await_args.args[0]
    env.pitch.assert_not_awaited()


def test_toggle_flips_the_servers_switch_and_redraws(env, monkeypatch):
    monkeypatch.setattr(wiz.spp, "scam_state", AsyncMock(return_value={"cfg": {"enabled": True}}))
    setter = AsyncMock(); monkeypatch.setattr(wiz.spp, "set_scam_enabled", setter)
    i = _interaction()
    run(wiz.WizButton("toggle", GUILD, None).callback(i))
    assert setter.await_args.args == (GUILD, None, 42, False)
    i.edit_original_response.assert_awaited_once()


def test_allow_and_check_open_modals_and_recent_lists_catches(env, monkeypatch):
    i = _interaction()
    run(wiz.WizButton("allow", GUILD, None).callback(i))
    assert isinstance(i.response.send_modal.await_args.args[0], wiz.AllowModal)
    i = _interaction()
    run(wiz.WizButton("check", GUILD, None).callback(i))
    assert isinstance(i.response.send_modal.await_args.args[0], wiz.CheckModal)
    monkeypatch.setattr(ss, "recent_guild_hits", AsyncMock(return_value=[]))
    i = _interaction()
    run(wiz.WizButton("recent", GUILD, None).callback(i))
    assert "Nothing has been caught" in i.followup.send.await_args.args[0]


def test_a_crashing_button_tells_the_user_instead_of_failing_silently(env, monkeypatch):
    monkeypatch.setattr(ss, "recent_guild_hits", AsyncMock(side_effect=RuntimeError("boom")))
    i = _interaction()
    run(wiz.WizButton("recent", GUILD, None).callback(i))
    assert "went wrong" in i.followup.send.await_args.args[0]


def _check(text, env, monkeypatch, allowed=()):
    from database import db
    monkeypatch.setattr(db, "get_scam_shield_guild", AsyncMock(return_value={"allowed_domains": list(allowed)}), raising=False)
    modal = wiz.CheckModal(GUILD, None)
    modal.text = SimpleNamespace(value=text)
    i = _interaction()
    run(modal.on_submit(i))
    return i.followup.send.await_args.args[0]


def test_check_a_link_reports_both_outcomes(env, monkeypatch):
    ss._c.domains = [(1, "dwinble.com")]
    assert "Would be caught" in _check("go to https://dwinble.com/promo", env, monkeypatch)
    assert "Not flagged" in _check("https://example.com/page", env, monkeypatch)


def test_check_a_link_uses_strict_only_for_premium_servers_with_it_on(env, monkeypatch):
    text = "free nitro https://x-site.io"
    assert "Not flagged" in _check(text, env, monkeypatch)                  # strict not switched on
    ss._c.strict = {(GUILD, None)}
    assert "Would be caught" in _check(text, env, monkeypatch)
    env.premium = False
    assert "Not flagged" in _check(text, env, monkeypatch)                  # lapsed premium: paused


# ── the live listener ────────────────────────────────────────────────────

def _msg(content):
    from discord_bot.cogs import scam_shield as cog_mod
    guild = SimpleNamespace(id=GUILD, owner_id=1, get_channel=lambda cid: None)
    author = MagicMock(spec=discord.Member)
    author.id = 42
    author.guild_permissions = SimpleNamespace(administrator=False, manage_guild=False, manage_messages=False)
    m = MagicMock()
    m.guild, m.author, m.content, m.embeds, m.attachments = guild, author, content, [], []
    m.type = discord.MessageType.default
    m.channel = SimpleNamespace(id=7, mention="#c")
    m.delete = AsyncMock()
    return m


@pytest.fixture()
def cog(monkeypatch):
    from discord_bot.cogs import scam_shield as cog_mod
    bot = MagicMock(); bot.user = SimpleNamespace(id=999); bot.clone_id = None
    c = cog_mod.ScamShieldCog.__new__(cog_mod.ScamShieldCog)
    c.bot = bot
    monkeypatch.setattr(cog_mod.ss, "log_hit", AsyncMock())
    monkeypatch.setattr(cog_mod.ss, "load", AsyncMock())
    monkeypatch.setattr(cog_mod.ss, "guild_settings",
                        AsyncMock(return_value={"enabled": True, "allowed_domains": ()}))
    monkeypatch.setattr(c, "_flag", AsyncMock(), raising=False)
    monkeypatch.setattr("discord_bot.ad_images._host_channel", AsyncMock(return_value=None))
    return c


BAIT = "free nitro https://discord-gift.xyz/abc"


def test_strict_deletes_for_premium_servers_that_switched_it_on(cog, monkeypatch):
    ss._c.strict = {(GUILD, None)}
    monkeypatch.setattr("modules.antiraid_pro.is_premium", AsyncMock(return_value=True))
    m = _msg(BAIT)
    assert run(cog._inspect(m)) is True
    m.delete.assert_awaited_once()


def test_strict_does_nothing_when_it_is_not_switched_on(cog, monkeypatch):
    premium = AsyncMock(return_value=True)
    monkeypatch.setattr("modules.antiraid_pro.is_premium", premium)
    m = _msg(BAIT)
    assert run(cog._inspect(m)) is False
    m.delete.assert_not_awaited(); premium.assert_not_awaited()            # not even a premium lookup


def test_strict_pauses_when_premium_lapses(cog, monkeypatch):
    ss._c.strict = {(GUILD, None)}
    monkeypatch.setattr("modules.antiraid_pro.is_premium", AsyncMock(return_value=False))
    m = _msg(BAIT)
    assert run(cog._inspect(m)) is False
    m.delete.assert_not_awaited()


def test_strict_never_touches_clean_messages_and_skips_premium_lookup(cog, monkeypatch):
    ss._c.strict = {(GUILD, None)}
    premium = AsyncMock(return_value=True)
    monkeypatch.setattr("modules.antiraid_pro.is_premium", premium)
    m = _msg("anyone up for a game? https://youtube.com/watch?v=1")
    assert run(cog._inspect(m)) is False
    m.delete.assert_not_awaited(); premium.assert_not_awaited()


def test_strict_honours_the_servers_allowed_domains(cog, monkeypatch):
    ss._c.strict = {(GUILD, None)}
    monkeypatch.setattr("modules.antiraid_pro.is_premium", AsyncMock(return_value=True))
    monkeypatch.setattr("discord_bot.cogs.scam_shield.ss.guild_settings",
                        AsyncMock(return_value={"enabled": True, "allowed_domains": ("discord-gift.xyz",)}))
    m = _msg(BAIT)
    assert run(cog._inspect(m)) is False
    m.delete.assert_not_awaited()


def test_the_command_is_registered_and_the_buttons_are_exported():
    from discord_bot.cogs import scam_shield as cog_mod
    cmd = cog_mod.ScamShieldCog.scamshield_cmd
    assert cmd.name == "scamshield" and "premium" in cmd.description
    assert wiz.DYNAMIC_ITEMS == (wiz.WizButton,)
