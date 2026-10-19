"""Phase 1: the cat features screens (care, cosmetics, quests, badges, wishlist) are Components V2 cards."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

discord = pytest.importorskip("discord")
from discord_bot.cogs import _views_cat_features as vf
from modules import cat_features, cat_quests, cat_wishlist


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _count(payload) -> int:
    """Components in a message payload, nested ones included (what Discord's 40 limit counts)."""
    if isinstance(payload, dict):
        return (1 if isinstance(payload.get("type"), int) else 0) + sum(_count(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return sum(_count(v) for v in payload)
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


def _inter(uid=10):
    i = MagicMock()
    i.user = SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name="Sam")
    i.guild_id, i.guild, i.client = 5, None, SimpleNamespace(clone_id=None)
    i.response.is_done = lambda: False
    i.response.send_message, i.response.edit_message = AsyncMock(), AsyncMock()
    i.followup.send = AsyncMock()
    return i


class _Want:
    def __init__(self, n): self.owner_id, self.id, self.rarity, self.trait = n, n, "rare", "Playful"
    def text(self): return "a rare Playful cat"


def test_wishlist_board_is_v2_and_a_full_page_fits():
    wants = [_Want(i) for i in range(1, cat_wishlist.PAGE_SIZE + 1)]
    v = vf.WishlistBoardView(0, 40, None, wants)
    assert isinstance(v, discord.ui.LayoutView)
    n = _count(v.to_components())
    assert 10 <= n <= 40, n
    t = _text(v)
    assert "Cat wishlist" in t and "Page 1/5" in t and t.count("is looking for") == cat_wishlist.PAGE_SIZE


def test_wishlist_empty_board_message_and_nav_state():
    v = vf.WishlistBoardView(0, 0, None, [])
    assert "Be the first to post a want" in _text(v)
    buttons = [c for c in v.walk_children() if isinstance(c, discord.ui.Button)]
    assert [b.label for b in buttons[:2]] == ["◀", "▶"] and all(b.disabled for b in buttons[:2])
    assert {b.label for b in buttons} >= {"Post a want", "My wants"}


def test_wishlist_turn_edits_with_a_new_v2_view(monkeypatch):
    monkeypatch.setattr(vf.cat_wishlist, "board", AsyncMock(return_value=([_Want(1)], 20)))
    v = vf.WishlistBoardView(0, 20, None, [_Want(1)])
    nxt = [c for c in v.walk_children() if isinstance(c, discord.ui.Button) and c.label == "▶"][0]
    assert not nxt.disabled
    i = _inter()
    run(nxt.callback(i))
    sent = i.response.edit_message.await_args.kwargs
    assert "embed" not in sent and "content" not in sent and "Page 2/3" in _text(sent["view"])


def test_wishlist_post_view_is_v2_with_rows_and_enables_continue():
    v = vf.WishlistPostView()
    assert isinstance(v, discord.ui.LayoutView) and 8 <= _count(v.to_components()) <= 40
    assert v.go.disabled
    i = _inter()
    i.data = {"values": ["rare"]}
    run(v._rarity(i))
    assert v.rarity == "rare" and not v.go.disabled


def test_cosmetics_screen_is_v2_and_fits(monkeypatch):
    items = [cat_features.Cosmetic(n, f"s{n}", "hat", f"Hat {n}", "🎩", 100 * n, 0) for n in range(1, 26)]
    monkeypatch.setattr(vf.cat_features, "cosmetics", AsyncMock(return_value=items))
    monkeypatch.setattr(vf.cat_features, "wardrobe", AsyncMock(return_value=(items[:3], {1})))
    i = _inter()
    run(vf.CosmeticsButton().callback(i))
    view = i.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, discord.ui.LayoutView) and "embed" not in i.response.send_message.await_args.kwargs
    t = _text(view)
    assert "Cat cosmetics" in t and "(worn)" in t and "Hat 25" in t and len(t) < 4000
    assert 6 <= _count(view.to_components()) <= 40       # container + text + two select rows


def test_quests_screen_and_claim_notice_are_v2(monkeypatch):
    q = lambda k, ready: SimpleNamespace(key=k, title=f"Quest {k}", progress=3, target=3, reward=1, claimed=False, ready=ready)
    monkeypatch.setattr(vf.cat_quests, "quests", AsyncMock(return_value=[q("a", True), q("b", False)]))
    i = _inter()
    run(vf.QuestsButton().callback(i))
    view = i.response.send_message.await_args.kwargs["view"]
    assert "Daily cat quests" in _text(view) and "Quest a" in _text(view)
    assert any(isinstance(c, discord.ui.Select) for c in view.walk_children())
    assert 4 <= _count(view.to_components()) <= 40       # container + text + row + select


def test_badges_screen_lists_every_badge_and_fits(monkeypatch):
    monkeypatch.setattr(vf.cat_quests, "award_badges", AsyncMock(return_value=[]))
    monkeypatch.setattr(vf.cat_quests, "owned_badges", AsyncMock(return_value={next(iter(cat_quests.BADGES))}))
    monkeypatch.setattr(vf.cat_announce, "announce_badges", AsyncMock())
    i = _inter()
    run(vf.BadgesButton().callback(i))
    view = i.response.send_message.await_args.kwargs["view"]
    t = _text(view)
    assert "Collection badges" in t and t.count("✅") == 1 and t.count("🔒") == len(cat_quests.BADGES) - 1
    assert any(isinstance(c, vf.ShareButton) for c in view.walk_children())
    assert 4 <= _count(view.to_components()) <= 40 and len(t) < 4000


def test_care_result_is_a_v2_card_with_a_share_button(monkeypatch):
    res = SimpleNamespace(status="ok", streak=3, coins=20, diamonds=0)
    monkeypatch.setattr(vf.cat_features, "care", AsyncMock(return_value=res))
    i = _inter()
    run(vf.CareButton().callback(i))
    view = i.response.send_message.await_args.kwargs["view"]
    assert "Care complete" in _text(view) and "3 day" in _text(view)
    assert any(isinstance(c, vf.ShareButton) for c in view.walk_children())


def test_public_feature_cards_are_locked_to_their_opener(monkeypatch):
    monkeypatch.setattr(vf.cat_wishlist, "board", AsyncMock(return_value=([], 0)))
    i = _inter(uid=10)
    run(vf.WishlistButton().callback(i))
    view = i.response.send_message.await_args.kwargs["view"]
    other = _inter(uid=99)
    assert run(view.interaction_check(other)) is False and other.response.send_message.await_args.kwargs["ephemeral"] is True
    assert run(view.interaction_check(i)) is True
