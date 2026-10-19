"""Phase 3: the adoption / My cats / trade / filter / help screens are Components V2 cards (visuals only)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip("discord")
from discord_bot.cogs import _views_cat_adopt as v
from modules import cat_adopt as ca
from modules import cat_help, cat_pool
from modules import cat_rank as cr
from modules import cat_wallet as cw

PIC = "https://cdn.discordapp.com/a/b/c.jpg"


def run(c):
    return asyncio.run(c)


def _count(payload) -> int:
    """Components in a message payload, nested ones included (what Discord's 40 limit counts)."""
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(x) for x in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(x) for x in payload)
    return 0


def _size(view):
    return _count(view.to_components())


def _chars(view) -> int:
    return len(v.view_text(view))


def mkcat(i=1, owner=10, rarity="common", name=None, rank=0):
    return ca.Cat(id=i, owner_id=owner, clone_id=None, pool_id=100 + i, name=name or f"Mochi{i}", rarity=rarity,
                  trait="Sleepy", cat_rank=rank)


class FI:
    def __init__(self, uid=10):
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=f"u{uid}", bot=False)
        self.client = SimpleNamespace(clone_id=None)
        self.guild_id, self.guild = 5, SimpleNamespace(id=5, name="Srv")
        self.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=9)))
        self.sent, self.edits = [], []
        self.response = SimpleNamespace(send_message=self._send, edit_message=self._edit, send_modal=AsyncMock())

    async def _send(self, content=None, **k): self.sent.append((content, k))
    async def _edit(self, **k): self.edits.append(k)


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value=PIC))
    monkeypatch.setattr(v.cat_ids, "safe_get", AsyncMock(return_value="142574D"))
    monkeypatch.setattr(cw, "safe_wallet", AsyncMock(return_value=cw.Wallet(500, 2)))


def _collection(n=3):
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(i) for i in range(1, n + 1)])
    run(view.refresh_wallet())
    run(view.draw())
    return view


def _buttons(view):
    return [c for c in view.walk_children() if isinstance(c, discord.ui.Button)]


# ── budgets (40 components and 4000 characters per message) ─────────────────────────────────────────────────────────

def test_my_cats_card_budget_with_one_few_and_many_cats():
    for n in (1, 3, 60):
        view = _collection(n)
        assert isinstance(view, discord.ui.LayoutView)
        assert 14 <= _size(view) <= 40, (n, _size(view))
        assert _chars(view) < 4000


def test_my_cats_card_has_the_picture_the_facts_the_purse_and_the_trade_id():
    view = _collection(3)
    gallery = [c for c in view.walk_children() if isinstance(c, discord.ui.MediaGallery)]
    assert len(gallery) == 1 and gallery[0].items[0].media.url == PIC
    text = v.view_text(view)
    assert "Mochi1" in text and "**Rarity:**" in text and "#1" in text
    assert "🪙 500 coins · 💎 2 diamonds" in text and "`142574D`" in text and "Cat 1 of 3" in text


def test_my_cats_card_still_works_when_the_picture_is_missing(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(side_effect=RuntimeError("no cdn")))
    view = _collection(2)
    assert not [c for c in view.walk_children() if isinstance(c, discord.ui.MediaGallery)]
    assert "Mochi1" in v.view_text(view)


def test_jump_menu_never_exceeds_25_options_and_pages_through_big_collections():
    view = _collection(60)
    menu = next(c for c in view.walk_children() if isinstance(c, discord.ui.Select))
    assert len(menu.options) == v.JUMP_PAGE == 25 and "1-25 of 60" in menu.placeholder
    view.index = 30
    run(view.draw())
    menu = next(c for c in view.walk_children() if isinstance(c, discord.ui.Select))
    assert "26-50 of 60" in menu.placeholder and sum(1 for o in menu.options if o.default) == 1


def test_all_button_labels_and_order_are_kept():
    labels = [b.label for b in _buttons(_collection(3))]
    assert labels[:5] == ["◀", "▶", "Rename", "Trade / gift", "Release"]
    assert "Filter / search" in labels and any(l.startswith("⭐ Rank up") for l in labels)
    assert [l for l in labels if "Convert" in l] == ["💎 Convert 1", "💎 Convert all (2)"]
    assert labels[-3:] == ["Feed · 80", "Water · 30", "Bed · 120"]


def test_help_card_budget_for_every_topic():
    for key in [None] + [k for k, *_ in cat_help.topics()]:
        view = v.HelpView(key)
        assert isinstance(view, discord.ui.LayoutView)
        assert 6 <= _size(view) <= 40 and _chars(view) < 4000, key


def test_filter_card_budget_and_selects():
    view = v.FilterView(_collection(4))
    assert 12 <= _size(view) <= 40 and _chars(view) < 4000
    selects = [c for c in view.walk_children() if isinstance(c, discord.ui.Select)]
    assert len(selects) == 3 and all(len(s.options) <= 25 for s in selects)
    assert "Filter your cats" in v.view_text(view)


def test_trade_screens_budget():
    parent = _collection(3)
    text = "🤝 pick\n\n" + v._trade_card_text(parent.current)
    target_view = v.TradeTargetView(parent, text=text, image=PIC)
    assert 9 <= _size(target_view) <= 40 and any(isinstance(c, discord.ui.UserSelect) for c in target_view.walk_children())
    theirs = [mkcat(100 + i, owner=20) for i in range(60)]
    ask = v.TradeAskView(parent, SimpleNamespace(id=20, display_name="bob", mention="<@20>"), theirs, text=text, image=PIC)
    assert 9 <= _size(ask) <= 40 and _chars(ask) < 4000
    pick = next(c for c in ask.walk_children() if isinstance(c, discord.ui.Select))
    assert len(pick.options) == v.ASK_PAGE + 1 <= 25                       # their cats + "just gift mine"
    ask.page = 1
    ask._build()
    pick = next(c for c in ask.walk_children() if isinstance(c, discord.ui.Select))
    assert "24-46 of 60" in pick.placeholder


def test_trade_offer_card_budget_and_buttons():
    giver = SimpleNamespace(id=10, mention="<@10>")
    taker = SimpleNamespace(id=20, mention="<@20>")
    offer = v.TradeOfferView(SimpleNamespace(clone_id=None), giver, taker, mkcat(1), mkcat(2, owner=20), where=" from **Srv**")
    assert 5 <= _size(offer) <= 40 and _chars(offer) < 4000
    assert [b.label for b in _buttons(offer)] == ["Accept", "Decline", "Cancel offer"]
    assert "<@20>" in v.view_text(offer) and "from **Srv**" in v.view_text(offer)


# ── behaviour that moved from `content=` / `embed=` into the card ─────────────────────────────────────────────────────

def test_a_result_note_sits_on_top_of_the_card_and_clears_on_the_next_tap(monkeypatch):
    monkeypatch.setattr(v.cat_care, "water", AsyncMock(return_value=SimpleNamespace(
        status="ok", coins=470, cost=30, until=None)))
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1), mkcat(2)]))
    view = _collection(2)
    i = FI()
    run(view._water(i))
    first = v.view_text(i.edits[0]["view"])
    assert first.startswith(v.cat_care.DROP) and "fresh water" in first
    assert "content" not in i.edits[0] and "embed" not in i.edits[0]
    j = FI()
    run(view._next(j))
    assert "fresh water" not in v.view_text(j.edits[0]["view"])


def test_rank_up_still_needs_two_taps_and_only_then_spends(monkeypatch):
    spend = AsyncMock(return_value=SimpleNamespace(status="ok", rank=1, cost=300, coins=200))
    monkeypatch.setattr(cr, "rank_up", spend)
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1, rank=1), mkcat(2)]))
    monkeypatch.setattr(v.cat_announce, "announce", AsyncMock())
    monkeypatch.setattr(v.cat_quests, "on_rank", AsyncMock())
    view = _collection(2)
    run(view._rank_up(FI()))
    spend.assert_not_awaited()
    assert any(b.label.startswith("Tap again: spend") for b in _buttons(view))
    i = FI()
    run(view._rank_up(i))
    spend.assert_awaited_once()
    assert "is now" in v.view_text(i.edits[0]["view"])


def test_rename_refreshes_the_card_in_place(monkeypatch):
    monkeypatch.setattr(ca, "rename", AsyncMock(return_value=True))
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(1, name="Biscuit"), mkcat(2)]))
    view = _collection(2)
    modal = v.RenameModal(view)
    modal.new_name = SimpleNamespace(__str__=lambda s: "Biscuit")
    i = FI()
    run(modal.on_submit(i))
    assert i.edits[0]["view"] is view and "Biscuit" in v.view_text(view) and "embed" not in i.edits[0]


def test_trade_screen_keeps_the_cat_picture_and_facts():
    view = _collection(2)
    i = FI()
    run(view._trade(i))
    screen = i.edits[0]["view"]
    assert isinstance(screen, v.TradeTargetView) and screen.image == PIC
    text = v.view_text(screen)
    assert "cat #1" in text and "This is the cat you are trading or selling" in text and "**Rank:**" in text
    assert [c for c in screen.walk_children() if isinstance(c, discord.ui.MediaGallery)]


def test_trade_picker_carries_the_cat_card_into_the_swap_step(monkeypatch):
    parent = _collection(2)
    screen = v.TradeTargetView(parent, text="x\n\n" + v._trade_card_text(parent.current), image=PIC)
    screen.picker._values = [SimpleNamespace(id=20, bot=False, display_name="bob", mention="<@20>")]
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(5, owner=20, name="Theirs")]))
    i = FI()
    run(screen._picked(i))
    ask = i.edits[0]["view"]
    assert isinstance(ask, v.TradeAskView) and ask.image == PIC and "swapped for" in v.view_text(ask)


def test_an_expired_offer_loses_its_buttons_and_says_so():
    giver = SimpleNamespace(id=10, mention="<@10>", send=AsyncMock())
    taker = SimpleNamespace(id=20, mention="<@20>")
    offer = v.TradeOfferView(SimpleNamespace(clone_id=None), giver, taker, mkcat(1), None)
    offer.message = SimpleNamespace(edit=AsyncMock())
    run(offer.on_timeout())
    sent = offer.message.edit.await_args.kwargs
    assert "embed" not in sent and "content" not in sent
    assert "expired" in v.view_text(sent["view"]) and not _buttons(sent["view"])


def test_the_adoption_result_for_the_adopter_is_a_private_v2_card(monkeypatch):
    cat = mkcat(1, rarity="rare")
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("ok", cat, cw.Reward("coins", 420))))
    monkeypatch.setattr(v.cat_quests, "on_adopt", AsyncMock(return_value=["First Steps"]))
    monkeypatch.setattr(v.cat_announce, "announce_badges", AsyncMock())
    follow = []

    async def follow_send(*a, **kw): follow.append((a, kw))
    i = FI()
    i.message = SimpleNamespace(embeds=[discord.Embed(title="x")])
    i.followup = SimpleNamespace(send=follow_send)
    run(v.AdoptButton(5).callback(i))
    (args, kw), = follow
    assert not args and kw["ephemeral"] is True and "embed" not in kw
    text = v.view_text(kw["view"])
    assert "You adopted **Mochi1**" in text and "420 coins" in text and "Badge earned" in text and "First Steps" in text
    assert "embed" in i.edits[0]                      # the public spawn post is still a classic embed message


# ── what must NOT change: the public spawn post and its persistent buttons ───────────────────────────────────────────

def test_spawn_post_stays_classic_with_the_same_persistent_buttons():
    view = v.spawn_view(7)
    assert type(view) is discord.ui.View and not isinstance(view, discord.ui.LayoutView)
    ids = [c.custom_id for c in view.children]
    assert ids[0] == "cat_adopt:7" and {"cat_mine", "cat_help", "cat_menu"} <= set(ids)
    assert {c.__name__ for c in v.DYNAMIC_ITEMS} >= {"AdoptButton", "MyCatsButton", "HelpButton", "MenuButton"}


def test_only_the_two_public_spawn_edits_still_pass_embeds():
    import inspect
    src = inspect.getsource(v)
    lines = [l.strip() for l in src.splitlines() if "embed=" in l and not l.strip().startswith("#")]
    assert len(lines) == 2, lines          # the spawn's "ran away" edit and its "adopted" edit, nothing else
    assert all("spawn_view" in l for l in lines)
