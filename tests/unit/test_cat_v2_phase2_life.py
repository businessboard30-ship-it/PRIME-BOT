"""Phase 2: Cat Life screens (hub, walks, heists, lucky games) are Components V2 cards; the heist reveal still works."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip("discord")
from discord_bot.cogs import _views_cat_life as v
from modules import cat_life as cl


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _count(payload) -> int:
    """Components in a message payload, nested ones included (what Discord's 40 limit counts)."""
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(x) for x in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(x) for x in payload)
    return 0


def _text(view):
    out = []
    def walk(items):
        for it in items:
            if hasattr(it, "content"):
                out.append(it.content)
            walk(getattr(it, "children", []) or [])
    walk(view.children)
    return "\n".join(out)


def _ov(cats=2, out=0, ready=0):
    return SimpleNamespace(cats=cats, outings_out=out, outings_ready=ready, rent_per_day=50, home_on=True,
                           home_until=__import__('datetime').datetime(2030, 1, 1), pending=10, estimate_per_day=5, heists_left=3, loss_left=100)


def _inter(uid=10, done=False):
    i = MagicMock()
    i.user = SimpleNamespace(id=uid, mention=f"<@{uid}>")
    i.guild_id, i.client = 5, SimpleNamespace(clone_id=None)
    i.response.is_done = lambda: done
    i.response.edit_message, i.response.send_message = AsyncMock(), AsyncMock()
    i.edit_original_response = AsyncMock()
    i.followup.send = AsyncMock()
    return i


ROWS = [{"id": n, "name": f"Cat{n}", "rarity": "rare", "cat_rank": 2} for n in range(1, 26)]


def test_every_cat_life_screen_is_v2_and_within_budget():
    screens = {"hub": v.HubView(_ov(), "## hub"), "walk": v.OutingView("walk", ROWS),
               "heist": v.OutingView("heist", ROWS), "games": v.GamesView(500, "note")}
    for name, view in screens.items():
        assert isinstance(view, discord.ui.LayoutView), name
        n = _count(view.to_components())
        assert 8 <= n <= 40, (name, n)                      # real lower bound: container, text, rows, controls, footer
        assert len(_text(view)) < 4000, name


def test_hub_buttons_disable_without_cats_and_keep_labels():
    view = v.HubView(_ov(cats=0), "x")
    buttons = {b.label: b for b in view.walk_children() if isinstance(b, discord.ui.Button)}
    assert set(buttons) == {"Collect", "Claim", "Pay rent (50/day)", "Take a walk", "Heist", "Lucky games"}
    assert all(buttons[k].disabled for k in ("Collect", "Claim", "Take a walk", "Heist"))
    assert not buttons["Lucky games"].disabled


def test_hub_payload_returns_a_v2_view_with_title(monkeypatch):
    monkeypatch.setattr(v.cat_wallet, "safe_wallet", AsyncMock(return_value=SimpleNamespace(coins=5, diamonds=1)))
    monkeypatch.setattr(v.cat_wallet, "wallet_text", lambda w: "wallet")
    monkeypatch.setattr(v.cat_life, "overview", AsyncMock(return_value=_ov()))
    view = run(v.hub_payload(10, None, "a note"))
    assert "Cat Life" in _text(view) and "a note" in _text(view) and "wallet" in _text(view)


def test_outing_view_enables_go_only_after_place_and_cat(monkeypatch):
    view = v.OutingView("walk", ROWS)
    assert view.go.disabled and len(view.cat.options) == 25
    monkeypatch.setattr(type(view.place), "values", property(lambda self: ["park"]), raising=False)
    view.route = "park"
    i = _inter()
    view.cat_id = 3
    view._ready()
    assert not view.go.disabled


def test_games_bet_change_keeps_the_last_result_on_screen():
    view = v.GamesView(500, "🪙 You won!")
    i = _inter()
    i.data = {"values": [str(cl.BETS[1])]}
    run(view._bet(i))
    new = i.response.edit_message.await_args.kwargs["view"]
    assert "You won!" in _text(new) and new.bet == cl.BETS[1] and "embed" not in i.response.edit_message.await_args.kwargs


def test_gamble_edits_the_same_message_with_the_result(monkeypatch):
    monkeypatch.setattr(v.cat_life, "gamble", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(v, "gamble_note", lambda res: "🎡 Spin result!")
    view = v.GamesView(500)
    play = [b for b in view.walk_children() if isinstance(b, discord.ui.Button) and b.label == "Lucky spin"][0]
    i = _inter()
    run(play.callback(i))
    assert "Spin result" in _text(i.response.edit_message.await_args.kwargs["view"])


def test_heist_reveal_edits_the_same_message_twice_with_v2_views(monkeypatch):
    """sneak screen via edit_message, wait, then the result via edit_original_response (same message)."""
    sleeps = []
    async def fake_sleep(sec): sleeps.append(sec)
    monkeypatch.setattr(v.asyncio, "sleep", fake_sleep)
    heist = next(iter(cl.HEISTS.values()))
    res = SimpleNamespace(cat_name="Mochi", chance=0.5, ends_at=None, cost=100, coins=900)
    monkeypatch.setattr(v, "_ts", lambda dt: 1700000000)
    monkeypatch.setattr(v.cat_life, "claim_outings", AsyncMock(return_value=["done"]))
    monkeypatch.setattr(v, "claim_note", lambda items: "🎁 You won big!")
    monkeypatch.setattr(v, "hub_payload", AsyncMock(return_value=v.HubView(_ov(), "## Cat Life\n🎁 You won big!")))
    i = _inter()
    state = {"done": False}
    i.response.is_done = lambda: state["done"]
    async def edit_message(**kw):
        state["done"] = True
    i.response.edit_message = AsyncMock(side_effect=edit_message)
    run(v._heist_reveal(i, res, heist))
    first = i.response.edit_message.await_args.kwargs
    assert "Sneaking into" in _text(first["view"]) and "embed" not in first and "content" not in first
    assert not [c for c in first["view"].walk_children() if isinstance(c, discord.ui.Button)]     # buttons gone while sneaking
    assert sleeps and sleeps[0] == heist.seconds + 1
    second = i.edit_original_response.await_args.kwargs
    assert "You won big" in _text(second["view"]) and "embed" not in second


def test_heist_reveal_says_still_going_when_nothing_to_claim(monkeypatch):
    async def fake_sleep(sec): pass
    monkeypatch.setattr(v.asyncio, "sleep", fake_sleep)
    heist = next(iter(cl.HEISTS.values()))
    res = SimpleNamespace(cat_name="Mochi", chance=0.5, ends_at=None, cost=100, coins=900)
    monkeypatch.setattr(v, "_ts", lambda dt: 1)
    monkeypatch.setattr(v.cat_life, "claim_outings", AsyncMock(return_value=[]))
    seen = {}
    async def hub(uid, clone, note=None):
        seen["note"] = note
        return v.HubView(_ov(), "x")
    monkeypatch.setattr(v, "hub_payload", hub)
    i = _inter()
    state = {"done": False}
    i.response.is_done = lambda: state["done"]
    async def edit_message(**kw): state["done"] = True
    i.response.edit_message = AsyncMock(side_effect=edit_message)
    run(v._heist_reveal(i, res, heist))
    assert "still going" in seen["note"] and v.cat_life.claim_outings.await_count == 4


def test_redraws_stay_locked_to_the_owner():
    view = v.HubView(_ov(), "x")
    i = _inter(uid=10)
    run(v._edit(i, view))
    other = _inter(uid=99)
    assert run(view.interaction_check(other)) is False and run(view.interaction_check(i)) is True
