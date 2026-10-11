"""Cat game ON but not working: the permission / channel check, the live setup-panel warning, the owner alert."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from discord_bot.cogs import _views_cat_setup as wiz
from discord_bot.cogs import cat
from modules import cat_perms as cp
from modules import cat_setup as cs
from tests.unit.test_cat_setup import _cog, _guild  # noqa: F401  (shared spawner fixtures)


def run(c):
    return asyncio.run(c)


def P(view=True, send=True, embed=True):
    return SimpleNamespace(view_channel=view, send_messages=send, embed_links=embed)


# ── pure helpers ──────────────────────────────────────────────────────────

def test_missing_names():
    assert cp.missing_names(P()) == []
    assert cp.missing_names(P(embed=False)) == ["Embed Links"]
    assert cp.missing_names(P(False, False, False)) == ["View Channel", "Send Messages", "Embed Links"]
    assert cp.missing_names(SimpleNamespace()) == ["View Channel", "Send Messages", "Embed Links"]


def test_diagnose_reports_gone_wrong_type_and_missing_permissions():
    g, chans = _guild([1, 2, 3, 4])
    chans[2].permissions_for.return_value = P(embed=False)
    voice = MagicMock(spec=discord.VoiceChannel); voice.id = 3
    g.get_channel = lambda cid: {1: chans[1], 2: chans[2], 3: voice}.get(cid)        # 4 was deleted
    found = {p.channel_id: p for p in cp.diagnose(g, [1, 2, 3, 4], discord.TextChannel)}
    assert 1 not in found
    assert found[2].kind == "perms" and found[2].missing == ("Embed Links",)
    assert found[3].kind == "not_text" and found[4].kind == "gone"


def test_panel_text_is_empty_when_fine_and_explains_the_fix_when_not():
    assert cp.panel_text([]) == ""
    text = cp.panel_text([cp.Problem(1, "perms", ("Send Messages", "Embed Links")), cp.Problem(2, "gone")])
    assert "<#1>" in text and "**Send Messages** and **Embed Links**" in text and "<#2>" in text
    assert "ON but I can't post" in text and "Edit Channel" in text


def test_owner_message_cannot_inject_markdown_from_the_server_name():
    msg = cp.owner_message("**evil** `x` " + "n" * 200, [cp.Problem(1, "perms", ("Embed Links",))])
    assert "**evil**" not in msg and "`x`" not in msg and "<#1>" in msg and "/catsetup" in msg


def test_alert_needs_three_failed_turns_in_a_row_and_then_waits_a_week():
    t = cp.AlertTracker()
    assert [t.failed(1, None, now=n) for n in (0, 1)] == [False, False]
    assert t.failed(1, None, now=2) is True                       # third failed turn
    assert t.failed(1, None, now=3) is False                      # already told
    assert t.failed(1, None, now=cp.ALERT_COOLDOWN_SECONDS - 1) is False
    assert t.failed(1, None, now=cp.ALERT_COOLDOWN_SECONDS + 10) is True


def test_a_good_turn_resets_the_count_and_servers_are_tracked_separately():
    t = cp.AlertTracker()
    t.failed(1, None, now=0); t.failed(1, None, now=1)
    t.ok(1, None)
    assert t.failed(1, None, now=2) is False                      # counting from zero again
    t.failed(2, None, now=0); t.failed(2, None, now=1)
    assert t.failed(2, 7, now=2) is False                         # a clone's server is a different key
    assert t.failed(2, None, now=2) is True


# ── spawner: ON but not posting ───────────────────────────────────────────

def _broken(monkeypatch, perms, premium=False, fetch_user=None):
    cat._alerts = cp.AlertTracker()
    g, chans = _guild([1])
    g.name, g.owner_id = "Cat Club", 555
    chans[1].permissions_for.return_value = perms
    cfg = cs.CatConfig(guild_id=77, enabled=True, channel_ids=(1,))
    c, _ = _cog(monkeypatch, g, cfg, premium=premium)
    owner = SimpleNamespace(bot=False, send=AsyncMock())
    c.bot.fetch_user = fetch_user or AsyncMock(return_value=owner)
    return c, owner, g, chans


def test_owner_is_told_once_after_three_failed_turns(monkeypatch):
    c, owner, *_ = _broken(monkeypatch, P(embed=False))
    for _ in range(2):
        assert run(c.spawn_due()) == 0
    owner.send.assert_not_called()
    run(c.spawn_due())
    owner.send.assert_awaited_once()
    text = owner.send.call_args.args[0]
    assert "<#1>" in text and "Embed Links" in text and "Cat Club" in text
    for _ in range(5):
        run(c.spawn_due())
    owner.send.assert_awaited_once()                              # not again


def test_working_server_is_never_messaged_and_recovery_resets(monkeypatch):
    c, owner, g, chans = _broken(monkeypatch, P(embed=False))
    run(c.spawn_due()); run(c.spawn_due())
    chans[1].permissions_for.return_value = P()                   # the admin fixed it
    assert run(c.spawn_due()) == 1
    chans[1].permissions_for.return_value = P(embed=False)
    run(c.spawn_due()); run(c.spawn_due())
    owner.send.assert_not_called()                                # count restarted after the good turn


def test_one_good_channel_means_no_permission_alert(monkeypatch):
    cat._alerts = cp.AlertTracker()
    g, chans = _guild([1, 2])
    g.name, g.owner_id = "Cat Club", 555
    chans[1].permissions_for.return_value = P(embed=False)
    chans[2].send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "boom"))   # fails for another reason
    cfg = cs.CatConfig(guild_id=77, enabled=True, channel_ids=(1, 2))
    c, _ = _cog(monkeypatch, g, cfg, premium=True)
    owner = SimpleNamespace(bot=False, send=AsyncMock())
    c.bot.fetch_user = AsyncMock(return_value=owner)
    for _ in range(6):
        run(c.spawn_due())
    owner.send.assert_not_called()                                # channel 2 is fine permission-wise: not our problem to report


def test_closed_dms_or_a_failing_lookup_never_break_the_spawner(monkeypatch):
    closed = SimpleNamespace(bot=False, send=AsyncMock(side_effect=discord.HTTPException(MagicMock(status=403, reason="x"), "closed")))
    c, *_ = _broken(monkeypatch, P(send=False), fetch_user=AsyncMock(return_value=closed))
    for _ in range(4):
        assert run(c.spawn_due()) == 0
    c2, *_ = _broken(monkeypatch, P(send=False), fetch_user=AsyncMock(side_effect=RuntimeError("x")))
    for _ in range(4):
        assert run(c2.spawn_due()) == 0


def test_a_bot_owner_account_is_never_messaged(monkeypatch):
    owner = SimpleNamespace(bot=True, send=AsyncMock())
    c, *_ = _broken(monkeypatch, P(send=False), fetch_user=AsyncMock(return_value=owner))
    for _ in range(4):
        run(c.spawn_due())
    owner.send.assert_not_called()


# ── setup panel ───────────────────────────────────────────────────────────

def _view(enabled=True, perms=None, channels=(1,)):
    g, chans = _guild(list(channels) or [1])
    for ch in chans.values():
        ch.permissions_for.return_value = perms or P()
    cfg = cs.CatConfig(guild_id=77, enabled=enabled, channel_ids=tuple(channels))
    return wiz.CatSetupView(MagicMock(), g, None, 9, cfg, False), g


def test_panel_warns_live_when_on_and_a_permission_is_missing():
    view, _ = _view(perms=P(embed=False))
    text = view._problems()
    assert "Embed Links" in text and "<#1>" in text
    assert text in wiz.render_embed(view.cfg, False, problems=text).description


def test_panel_is_quiet_when_all_is_fine_or_the_game_is_off():
    assert _view()[0]._problems() == ""
    assert _view(enabled=False, perms=P(embed=False))[0]._problems() == ""
    assert _view(channels=())[0]._problems() == ""


def test_render_embed_without_problems_is_unchanged():
    view, _ = _view()
    assert "can't post" not in wiz.render_embed(view.cfg, False).description


def test_check_button_exists_and_reports_both_outcomes():
    view, _ = _view(perms=P(send=False))
    assert any(getattr(c, "label", "") == "Check permissions" for c in view.children)
    i = MagicMock(); i.response.send_message = AsyncMock()
    run(view._on_check(i))
    msg = i.response.send_message.call_args.args[0]
    assert "Send Messages" in msg and "ON but" not in msg and i.response.send_message.call_args.kwargs["ephemeral"] is True
    ok_view, _ = _view()
    i = MagicMock(); i.response.send_message = AsyncMock()
    run(ok_view._on_check(i))
    assert "✅" in i.response.send_message.call_args.args[0]


def test_check_button_is_disabled_until_a_channel_is_picked():
    assert _view(channels=())[0].check.disabled is True
    assert _view()[0].check.disabled is False


def test_post_cat_and_setup_share_the_same_permission_rule():
    import inspect
    assert "cat_perms.missing_names" in inspect.getsource(cat.post_cat)
    assert "cat_perms.missing_names" in inspect.getsource(cat.CatSetupCog._can_post)
