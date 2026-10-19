"""Phase 4: the leaderboard and the sell wizard are Components V2 cards (visuals only; public listing post unchanged)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

discord = pytest.importorskip("discord")
from discord_bot.cogs import _views_cat_market as vm
from modules import cat_adopt as ca
from modules import cat_board
from modules import cat_market as cm


def run(c):
    return asyncio.run(c)


def _count(payload) -> int:
    """Components in a message payload, nested ones included (what Discord's 40 limit counts)."""
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(x) for x in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(x) for x in payload)
    return 0


def _text(view):
    return "\n".join(c.content for c in view.walk_children() if isinstance(c, discord.ui.TextDisplay))


def _buttons(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


class Box:                                   # text input stand-in
    def __init__(self, t): self.t = t
    def __str__(self): return self.t


def mkcat(rank=3, rarity="rare"):
    return ca.Cat(id=42, owner_id=1, clone_id=None, pool_id=9, name="Mochi", rarity=rarity, trait="Sleepy", cat_rank=rank)


class FI:
    def __init__(self, uid=1, guild=True):
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>")
        self.client = SimpleNamespace(clone_id=None)
        self.guild = SimpleNamespace(id=5) if guild else None
        self.channel = SimpleNamespace(id=7) if guild else None
        self.edits, self.sent, self.modals, self.orig = [], [], [], []
        self.response = SimpleNamespace(edit_message=self._edit, send_message=self._send, send_modal=self._modal,
                                        defer=AsyncMock())

    async def _edit(self, **k): self.edits.append(k)
    async def _send(self, content=None, **k): self.sent.append((content, k))
    async def _modal(self, m): self.modals.append(m)
    async def edit_original_response(self, **k): self.orig.append(k)


# ── leaderboard ──────────────────────────────────────────────────────────────────────────────────────────────────

ROWS = [{"owner_id": i, "cats": 30 - i, "score": 500 - i, "legendary": i % 2} for i in range(1, 11)]


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setattr(cat_board, "top_collectors", AsyncMock(return_value=(ROWS, 14)))
    monkeypatch.setattr(cat_board, "top_richest", AsyncMock(return_value=(ROWS, None)))


def test_board_card_budget_and_content(board):
    view = vm.BoardView(SimpleNamespace(id=1))
    run(view.draw())
    assert isinstance(view, discord.ui.LayoutView)
    assert 6 <= _count(view.to_components()) <= 40 and len(_text(view)) < 4000
    t = _text(view)
    assert "Top cat collectors" in t and "<@1>" in t and "pts" in t and "points: common 1" in t and "**You:** #14" in t
    assert [b.label for b in _buttons(view)] == ["Top collectors", "Richest", "Post to channel"]


def test_board_switch_edits_in_place_with_the_richest_tab(board):
    view = vm.BoardView(SimpleNamespace(id=1))
    run(view.draw())
    i = FI()
    run(view._switch(i, "richest"))
    assert i.edits[0]["view"] is view and "embed" not in i.edits[0] and "content" not in i.edits[0]
    t = _text(view)
    assert "Richest cat owners" in t and "coins + diamonds" in t and "coins" in t
    assert "Post to channel" not in [b.label for b in _buttons(view)]          # wallets are never posted publicly
    assert [b.disabled for b in _buttons(view)] == [False, True]


def test_board_failure_shows_a_polite_line(monkeypatch):
    monkeypatch.setattr(cat_board, "top_collectors", AsyncMock(side_effect=RuntimeError("db")))
    view = vm.BoardView(SimpleNamespace(id=1))
    run(view.draw())
    assert "isn't available right now" in _text(view)


def test_board_is_opened_from_the_persistent_button_as_a_v2_message(board):
    sent = []

    async def send_message(*a, **kw): sent.append(kw)
    i = FI()
    i.guild_id = None                       # private reply path
    i.response = SimpleNamespace(send_message=send_message, is_done=lambda: False)
    run(vm.LeaderboardButton().callback(i))
    assert "embed" not in sent[0] and isinstance(sent[0]["view"], discord.ui.LayoutView) and "Top cat collectors" in _text(sent[0]["view"])


def test_board_is_owner_only(board):
    view = vm.BoardView(SimpleNamespace(id=1))
    other = FI(uid=2)
    assert run(view.interaction_check(other)) is False and other.sent


# ── sell wizard ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_wizard_cards_budget_range_and_steps():
    c = mkcat(rank=5)
    low, high = cm.price_range("rare", 5)
    wiz = vm.SellWizard(c, 1)
    step1 = wiz.price_view()
    assert isinstance(step1, discord.ui.LayoutView) and 7 <= _count(step1.to_components()) <= 40
    t1 = _text(step1)
    assert "step 1 of 2" in t1 and f"{low:,}" in t1 and f"{high:,}" in t1 and "Play coins only" in t1
    sel = next(x for x in step1.walk_children() if isinstance(x, discord.ui.Select))
    assert 1 <= len(sel.options) <= 25 and all(low <= int(o.value) <= high for o in sel.options)
    assert [b.label for b in _buttons(step1)] == ["Type a price", "Cancel"]
    wiz.price = low
    step2 = wiz.confirm_view()
    assert 5 <= _count(step2.to_components()) <= 40 and "step 2 of 2" in _text(step2) and f"{low:,}" in _text(step2)
    assert [b.label for b in _buttons(step2)] == ["List it", "Change price"]


def test_wizard_opens_as_a_private_v2_message():
    i = FI()
    run(vm.open_sell_wizard(i, mkcat()))
    kw = i.sent[0][1]
    assert kw["ephemeral"] is True and "embed" not in kw and isinstance(kw["view"], discord.ui.LayoutView)


def test_picking_a_price_moves_to_confirm_and_change_price_goes_back():
    wiz = vm.SellWizard(mkcat(), 1)
    low, _ = cm.price_range("rare", 3)
    step1 = wiz.price_view()
    sel = next(x for x in step1.walk_children() if isinstance(x, discord.ui.Select))
    sel._values = [str(low)]
    i = FI()
    run(sel.callback(i))
    confirm = i.edits[0]["view"]
    assert wiz.price == low and "step 2 of 2" in _text(confirm) and "embed" not in i.edits[0]
    back = next(b for b in _buttons(confirm) if b.label == "Change price")
    j = FI()
    run(back.callback(j))
    assert "step 1 of 2" in _text(j.edits[0]["view"])


def test_typing_a_good_price_moves_the_same_message_to_confirm():
    wiz = vm.SellWizard(mkcat(), 1)
    low, high = cm.price_range("rare", 3)
    modal = vm.SellModal(wiz.cat, wiz)
    modal.price = Box(str(low))
    i = FI()
    run(modal.on_submit(i))
    assert wiz.price == low and "step 2 of 2" in _text(i.edits[0]["view"]) and "embed" not in i.edits[0]
    modal.price = Box("1")                     # out of range: private note, nothing moves
    j = FI()
    run(modal.on_submit(j))
    assert not j.edits and f"{low:,} to {high:,}" in j.sent[0][0]


def test_cancel_and_list_it_end_with_a_button_free_card(monkeypatch):
    wiz = vm.SellWizard(mkcat(), 1)
    wiz.price = cm.price_range("rare", 3)[0]
    cancel = next(b for b in _buttons(wiz.price_view()) if b.label == "Cancel")
    i = FI()
    run(cancel.callback(i))
    assert "nothing was listed" in _text(i.edits[0]["view"]) and not _buttons(i.edits[0]["view"])
    monkeypatch.setattr(vm, "publish_listing", AsyncMock(return_value="🏷️ **Mochi** is for sale for **500 coins**!"))
    ok = next(b for b in _buttons(wiz.confirm_view()) if b.label == "List it")
    j = FI()
    run(ok.callback(j))
    done = j.orig[0]
    assert "embed" not in done and "content" not in done and "is for sale" in _text(done["view"]) and not _buttons(done["view"])
    vm.publish_listing.assert_awaited_once()
    d = FI(guild=False)
    run(ok.callback(d))
    assert "not in DMs" in _text(d.edits[0]["view"])


def test_wizard_is_owner_only():
    view = vm.SellWizard(mkcat(), 1).price_view()
    other = FI(uid=2)
    assert run(view.interaction_check(other)) is False and "someone else's sale" in other.sent[0][0]


def test_the_public_listing_post_is_still_a_classic_embed_message():
    import inspect
    src = inspect.getsource(vm.publish_listing)
    assert "embed=await listing_embed" in src and "view=listing_view(" in src
    assert type(vm.listing_view(1, 500)) is discord.ui.View
