"""Spawn message now carries Adopt + My cats + Market + Help; Help is private and its numbers come from the real rules."""
import asyncio
import re
from types import SimpleNamespace

from discord_bot.cogs import _views_cat_adopt as v
from discord_bot.cogs import _views_cat_market as market
from modules import cat_adopt, cat_help, cat_market, cat_rank, cat_wallet, cat_wishlist


def run(coro):
    return asyncio.run(coro)


def test_spawn_has_adopt_my_cats_market_and_help():
    ids = [c.custom_id for c in v.spawn_view(7).children]
    assert ids == ["cat_adopt:7", "cat_mine", "cat_market", "cat_help"]


def test_adopted_spawn_keeps_market_and_help_and_disables_only_adopt():
    kids = v.spawn_view(7, adopted=True).children
    assert [c.custom_id for c in kids] == ["cat_adopt:7", "cat_mine", "cat_market", "cat_help"]
    inner = [c.item for c in kids]
    assert inner[0].disabled is True and not any(b.disabled for b in inner[1:])


def test_new_buttons_survive_a_restart():
    """Persistent buttons only work after a restart if their class is registered in DYNAMIC_ITEMS."""
    assert v.HelpButton in v.DYNAMIC_ITEMS and market.MarketButton in v.DYNAMIC_ITEMS
    for cid in ("cat_adopt:7", "cat_mine", "cat_market", "cat_help", "cat_board"):
        hits = [i for i in v.DYNAMIC_ITEMS if i.__discord_ui_compiled_template__.match(cid)]
        assert len(hits) == 1, f"{cid} must be claimed by exactly one dynamic item, got {hits}"


def test_help_button_round_trips_and_replies_privately():
    m = re.match(v.HelpButton.__discord_ui_compiled_template__.pattern, "cat_help")
    obj = run(v.HelpButton.from_custom_id(None, None, m))
    assert obj.item.custom_id == "cat_help"
    sent = []

    async def send_message(*a, **kw):
        sent.append(kw)
    inter = SimpleNamespace(response=SimpleNamespace(send_message=send_message))
    run(obj.callback(inter))
    assert sent[0]["ephemeral"] is True
    e = sent[0]["embed"]
    assert "Adopt me" in e.description and "Market" in e.description and "no real money" in e.footer.text


def test_help_text_uses_the_real_numbers():
    text = cat_help.build_text()
    assert f"{cat_market.MIN_PRICE:,}" in text and f"{cat_market.MAX_PRICE:,}" in text
    assert str(cat_market.MAX_OPEN_PER_SELLER) in text and f"{cat_wallet.DIAMOND_COINS:,}" in text
    assert str(cat_adopt.MAX_CATS) in text and str(cat_rank.MAX_RANK) in text and str(cat_wishlist.MAX_OPEN) in text
    assert len(text) < 4000                                  # fits an embed description with room to spare


def test_every_help_section_fits_discord_limits():
    for title, body in cat_help.sections():
        assert 0 < len(title) <= 256 and 0 < len(body) <= 1024
