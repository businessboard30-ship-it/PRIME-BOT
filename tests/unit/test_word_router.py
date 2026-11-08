"""One-word router: only real triggers fire, panels list only what the user may run, and a button press re-checks everything."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from discord_bot.cogs import word_router as cog
from modules import word_router as wr

BOT = 777
MANAGE_GUILD = discord.Permissions(manage_guild=True).value
ADMIN = discord.Permissions(administrator=True).value


def run(c):
    return asyncio.run(c)


# ── parsing ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,dm,expect", [
    (f"<@{BOT}> raid", False, "raid"), (f"<@!{BOT}>   RAID ", False, "raid"), ("!raid", False, "raid"), ("!Raid", True, "raid"),
    ("raid", True, "raid"),
    ("raid", False, None),                       # bare word in a server channel is normal chat
    (f"<@{BOT}> two words", False, None), (f"<@{BOT}>", False, None), ("!two words", False, None), ("!", False, None),
    ("", True, None), (None, True, None), ("<@123> raid", False, None), (f"<@{BOT}> r", False, None),
    (f"<@{BOT}> raid; drop", False, None), ("hello there", True, None),
])
def test_parse_trigger(text, dm, expect):
    assert wr.parse_trigger(text, BOT, dm) == expect


def test_real_prefix_command_is_left_alone():
    assert wr.parse_trigger("!help", BOT, False, lambda w: w == "help") is None


# ── ranking + permissions ────────────────────────────────────────────────
E = [wr.Entry("antiraid status", "Show raid protection", MANAGE_GUILD, True), wr.Entry("antiraid setup", "Configure", MANAGE_GUILD, True),
     wr.Entry("raid", "Alias", 0, False), wr.Entry("cat menu", "Open the cat menu", 0, False),
     wr.Entry("ping", "Latency; fast raid check", 0, False), wr.Entry("dmonly", "x", 0, False)]


def names(found):
    return [e.qname for e in found]


def test_exact_word_beats_prefix_beats_description():
    got = names(wr.rank("raid", E, ADMIN, True))
    assert got[0] == "raid" and got.index("antiraid status") < got.index("ping")


def test_users_without_permission_never_see_admin_commands():
    got = names(wr.rank("raid", E, 0, True))
    assert "antiraid status" not in got and "antiraid setup" not in got and "raid" in got


def test_guild_only_hidden_in_dm_and_perm_commands_hidden_in_dm():
    got = names(wr.rank("raid", E, None, False))
    assert got == ["raid", "ping"] or set(got) == {"raid", "ping"}


def test_administrator_passes_any_perm():
    assert wr.allowed(wr.Entry("x", "", MANAGE_GUILD | discord.Permissions(ban_members=True).value, True), ADMIN, True)


def test_requires_all_bits_not_any():
    both = MANAGE_GUILD | discord.Permissions(ban_members=True).value
    assert not wr.allowed(wr.Entry("x", "", both, True), MANAGE_GUILD, True)


def test_rank_is_capped_and_no_match_is_empty():
    many = [wr.Entry(f"raid{i}", "", 0, False) for i in range(40)]
    assert len(wr.rank("raid", many, 0, True)) == wr.MAX_BUTTONS
    assert wr.rank("zzzz", E, ADMIN, True) == []


def test_top_level_names_unique_sorted_and_filtered():
    assert wr.top_level_names(E, 0, True) == ["cat", "dmonly", "ping", "raid"]


# ── entries from a real command tree ─────────────────────────────────────
class Demo(commands.Cog):
    g = app_commands.default_permissions(manage_guild=True)(app_commands.guild_only()(app_commands.Group(name="demo", description="d")))
    ran = []
    explode = False

    @g.command(name="open", description="open it")
    async def open(self, i):
        if Demo.explode:
            raise RuntimeError("boom")
        self.ran.append("open")

    @g.command(name="needs", description="needs arg")
    async def needs(self, i, who: str):
        self.ran.append("needs")

    @app_commands.command(name="plain", description="plain one")
    async def plain(self, i, n: int = 1):
        self.ran.append("plain")


def make_bot():
    bot = MagicMock()
    cogobj = Demo()
    cmds = list(cogobj.walk_app_commands())
    bot.tree.walk_commands = lambda: iter(cmds)
    return bot, cogobj


def test_entries_skip_groups_and_commands_with_required_args_and_inherit_group_perms():
    bot, _ = make_bot()
    ents = {e.qname: e for e in wr.entries_from([c for c in bot.tree.walk_commands()])}
    assert set(ents) == {"demo open", "plain"}
    assert ents["demo open"].perms == MANAGE_GUILD and ents["demo open"].guild_only
    assert ents["plain"].perms == 0 and not ents["plain"].guild_only


# ── view + button press ──────────────────────────────────────────────────
def view_for(bot, perms, in_guild=True, word="demo"):
    return cog.RouterView(bot, 5, word, perms, in_guild)


def buttons(view):
    out = []
    for item in view.walk_children():
        if isinstance(item, discord.ui.Button):
            out.append(item)
    return out


def inter(user_id=5, perms=ADMIN, guild=True):
    i = SimpleNamespace(user=SimpleNamespace(id=user_id), guild=object() if guild else None,
                        permissions=discord.Permissions(perms) if guild else SimpleNamespace(value=0))
    i.response = SimpleNamespace(is_done=lambda: False, send_message=AsyncMock(), edit_message=AsyncMock())
    i.followup = SimpleNamespace(send=AsyncMock())
    return i


def test_panel_shows_only_buttons_the_user_may_use():
    bot, _ = make_bot()
    assert [b.label for b in buttons(view_for(bot, ADMIN))] == ["demo open"]
    assert buttons(view_for(bot, 0)) == []                 # no match left -> the fallback select, no buttons


def test_press_runs_the_command_for_the_owner():
    bot, c = make_bot()
    Demo.ran.clear()
    v = view_for(bot, ADMIN)
    run(buttons(v)[0].callback(inter()))
    assert Demo.ran == ["open"]


def test_press_is_rechecked_and_refused_if_permission_was_lost():
    bot, _ = make_bot()
    Demo.ran.clear()
    v = view_for(bot, ADMIN)
    i = inter(perms=0)
    run(buttons(v)[0].callback(i))
    assert Demo.ran == [] and i.response.send_message.await_count == 1


def test_only_the_person_who_typed_the_word_can_press():
    bot, _ = make_bot()
    v = view_for(bot, ADMIN)
    i = inter(user_id=999)
    assert run(v.interaction_check(i)) is False and i.response.send_message.await_count == 1
    assert run(v.interaction_check(inter(user_id=5))) is True


def test_command_that_raises_is_reported_not_crashed():
    bot, _ = make_bot()
    v = view_for(bot, ADMIN)
    Demo.explode = True
    try:
        i = inter()
        run(buttons(v)[0].callback(i))
    finally:
        Demo.explode = False
    assert i.response.send_message.await_count == 1


# ── listener ─────────────────────────────────────────────────────────────
def message(content, guild=True, bot_author=False, uid=5):
    m = SimpleNamespace(content=content, author=SimpleNamespace(id=uid, bot=bot_author), guild=object() if guild else None,
                        channel=SimpleNamespace(permissions_for=lambda u: discord.Permissions(ADMIN)))
    m.reply = AsyncMock(return_value=SimpleNamespace())
    return m


def make_cog():
    bot, _ = make_bot()
    bot.user = SimpleNamespace(id=BOT)
    bot.get_command = lambda w: None
    return cog.WordRouterCog(bot)


def test_listener_replies_once_with_a_view_and_no_pings():
    c = make_cog()
    m = message(f"<@{BOT}> demo")
    run(c.on_message(m))
    m.reply.assert_awaited_once()
    kw = m.reply.await_args.kwargs
    assert kw["view"] is not None and kw["allowed_mentions"].everyone is False and kw["mention_author"] is False


@pytest.mark.parametrize("m", [message("demo"), message("hello world", guild=False), message(f"<@{BOT}> demo", bot_author=True)])
def test_listener_ignores_non_triggers_and_bots(m):
    c = make_cog()
    run(c.on_message(m))
    m.reply.assert_not_awaited()


def test_listener_cooldown_blocks_spam():
    c = make_cog()
    a, b = message("!demo"), message("!demo")
    run(c.on_message(a)); run(c.on_message(b))
    a.reply.assert_awaited_once(); b.reply.assert_not_awaited()


def test_dm_bare_word_gets_a_panel_only_for_commands_that_work_in_a_dm():
    c = make_cog()
    ok = message("plain", guild=False)
    run(c.on_message(ok))
    ok.reply.assert_awaited_once()
    guild_only = message("demo", guild=False)          # only guild-only, permissioned commands match -> nothing to show
    assert c.claims(guild_only) is False
    run(c.on_message(guild_only))
    guild_only.reply.assert_not_awaited()


# ── one answer, never two: the router claims its messages so AI chat stays silent ─────────
def test_claims_a_mention_with_a_matching_word_but_not_an_unknown_one():
    c = make_cog()
    assert c.claims(message(f"<@{BOT}> demo")) is True
    assert c.claims(message(f"<@{BOT}> hello")) is False         # no match -> normal chat answers it
    assert c.claims(message(f"<@{BOT}> demo please")) is False   # two words are never a trigger
    assert c.claims(message("demo")) is False                    # a bare word in a server is just chat


def test_bang_word_is_always_claimed_even_without_a_match():
    c = make_cog()
    m = message("!zzzz")
    assert c.claims(m) is True
    run(c.on_message(m))
    m.reply.assert_awaited_once()


def test_claims_never_raises():
    c = make_cog()
    assert c.claims(SimpleNamespace(content="!x", author=None, guild=None)) is False


def test_ai_chat_stays_silent_when_the_router_claims_the_message():
    from discord_bot.cogs import ai_tools
    router = SimpleNamespace(claims=lambda m: True)
    boom = MagicMock(side_effect=AssertionError("chat must not run"))
    me = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda n: router if n == "WordRouterCog" else None, user=SimpleNamespace(id=BOT)),
                         _dm_context=boom, _reply_chain=boom, _run_reply_turn=boom)
    m = SimpleNamespace(author=SimpleNamespace(bot=False), content=f"<@{BOT}> demo", attachments=[], guild=object(),
                        reference=None, mentions=[SimpleNamespace(id=BOT)])
    run(ai_tools.AIToolsCog.on_bot_chat(me, m))
    boom.assert_not_called()
