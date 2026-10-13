"""Cat adoption: Adopt button on spawns, collection browser, trades (virtual only)."""
import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from discord_bot.cogs import _views_cat_adopt as v
from discord_bot.cogs import cat
from modules import cat_adopt as ca
from modules import cat_pool


def run(coro):
    return asyncio.run(coro)


def mkcat(i=1, owner=10, rarity="common", name="Mochi"):
    return ca.Cat(id=i, owner_id=owner, clone_id=None, pool_id=100 + i, name=name, rarity=rarity, trait="Sleepy")


class FI:
    """Minimal interaction."""
    def __init__(self, uid=10, message=None, guild=True):
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=f"u{uid}", bot=False)
        self.client = SimpleNamespace(clone_id=None)
        self.guild_id = 5 if guild else None
        self.guild = object() if guild else None
        self.channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=999)))
        self.message = message
        self.sent, self.edits, self.followups, self.modals = [], [], [], []
        self.response = SimpleNamespace(send_message=self._send, edit_message=self._edit, send_modal=self._modal)
        self.followup = SimpleNamespace(send=self._follow)

    async def _send(self, content=None, **k): self.sent.append((content, k))
    async def _edit(self, **k): self.edits.append(k)
    async def _modal(self, m): self.modals.append(m)
    async def _follow(self, content=None, **k): self.followups.append((content, k))


@pytest.fixture(autouse=True)
def no_images(monkeypatch):
    monkeypatch.setattr(cat_pool, "url_for", AsyncMock(return_value="https://cdn.discordapp.com/a/b/c.jpg"))


# ── rolls + names ─────────────────────────────────────────────────────────

def test_rarity_odds_are_roughly_70_20_8_2():
    rng = random.Random(7)
    n = 20000
    counts = {}
    for _ in range(n):
        r = ca.roll_rarity(rng)
        counts[r] = counts.get(r, 0) + 1
    assert 0.66 < counts["common"] / n < 0.74 and 0.17 < counts["uncommon"] / n < 0.23
    assert 0.06 < counts["rare"] / n < 0.10 and 0.01 < counts["legendary"] / n < 0.035


@pytest.mark.parametrize("raw,expected", [("Mochi", "Mochi"), ("  Sir   Fluff  ", "Sir Fluff"), ("a", None),
                                          ("@everyone", "everyone"), ("<@123>", "123"), ("visit http://x.io", None),
                                          ("x" * 50, "x" * 20), ("", None), ("😻 Luna", "😻 Luna")])
def test_clean_name(raw, expected):
    assert ca.clean_name(raw) == expected


# ── Adopt button ──────────────────────────────────────────────────────────

def _msg_with_embed():
    return SimpleNamespace(embeds=[discord.Embed(title="x")])


def test_adopt_ok_edits_the_spawn_and_tells_the_adopter(monkeypatch):
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult("ok", mkcat(rarity="rare"))))
    i = FI(10, _msg_with_embed())
    run(v.AdoptButton(5).callback(i))
    e = i.edits[0]
    assert "adopted by <@10>" in e["embed"].description and "Rare" in e["embed"].description
    btn = e["view"].children[0].item
    assert btn.disabled and btn.label.startswith("Adopted")
    assert "adopted **Mochi**" in i.followups[0][0] and i.followups[0][1]["ephemeral"] is True


@pytest.mark.parametrize("status,word", [("taken", "already adopted"), ("gone", "no longer available"),
                                         ("limit", "home is full")])
def test_adopt_refusals(monkeypatch, status, word):
    monkeypatch.setattr(ca, "adopt", AsyncMock(return_value=ca.AdoptResult(status)))
    i = FI(10, _msg_with_embed())
    run(v.AdoptButton(5).callback(i))
    assert word in i.sent[0][0] and i.sent[0][1]["ephemeral"] is True and not i.edits


def test_adopt_database_error_is_friendly(monkeypatch):
    monkeypatch.setattr(ca, "adopt", AsyncMock(side_effect=RuntimeError("db")))
    i = FI(10, _msg_with_embed())
    run(v.AdoptButton(5).callback(i))
    assert "try again" in i.sent[0][0]


def test_dynamic_item_ids_round_trip():
    assert v.AdoptButton(42).item.custom_id == "cat_adopt:42"
    m = __import__("re").match(v.AdoptButton.__discord_ui_compiled_template__.pattern, "cat_adopt:42")
    assert m and m.group(1) == "42"
    assert v.MyCatsButton().item.custom_id == "cat_mine"


# ── spawn posts ───────────────────────────────────────────────────────────

def _guild_with_channel():
    g = MagicMock(); g.id = 77
    ch = MagicMock(spec=discord.TextChannel); ch.id = 1
    ch.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    ch.send = AsyncMock(return_value=SimpleNamespace(id=555))
    g.get_channel = lambda cid: ch
    return g, ch


def test_pooled_spawn_gets_an_adopt_button(monkeypatch):
    g, ch = _guild_with_channel()
    monkeypatch.setattr(cat, "fetch_cat_url", AsyncMock(return_value=cat_pool.PooledUrl("https://cdn.discordapp.com/x.jpg", 9)))
    create = AsyncMock(return_value=31)
    monkeypatch.setattr(ca, "create_spawn", create)
    setmsg = AsyncMock()
    monkeypatch.setattr(ca, "set_spawn_message", setmsg)
    bot = SimpleNamespace(clone_id=None)
    assert run(cat.post_cat(bot, g, 1)) is True
    create.assert_awaited_once_with(77, None, 1, 9)
    ids = [c.custom_id for c in ch.send.await_args.kwargs["view"].children]
    assert ids == ["cat_adopt:31", "cat_mine", "cat_market", "cat_help", "cat_menu"]
    setmsg.assert_awaited_once_with(31, 555)


def test_unpooled_picture_posts_without_adopt(monkeypatch):
    g, ch = _guild_with_channel()
    monkeypatch.setattr(cat, "fetch_cat_url", AsyncMock(return_value="https://cdn2.thecatapi.com/x.jpg"))
    create = AsyncMock()
    monkeypatch.setattr(ca, "create_spawn", create)
    assert run(cat.post_cat(SimpleNamespace(clone_id=None), g, 1)) is True
    create.assert_not_awaited() and "view" not in ch.send.await_args.kwargs


def test_spawn_still_posts_if_recording_it_fails(monkeypatch):
    g, ch = _guild_with_channel()
    monkeypatch.setattr(cat, "fetch_cat_url", AsyncMock(return_value=cat_pool.PooledUrl("https://cdn.discordapp.com/x.jpg", 9)))
    monkeypatch.setattr(ca, "create_spawn", AsyncMock(side_effect=RuntimeError("db")))
    assert run(cat.post_cat(SimpleNamespace(clone_id=None), g, 1)) is True
    assert "view" not in ch.send.await_args.kwargs


# ── collection ────────────────────────────────────────────────────────────

def test_empty_collection_says_how_to_adopt(monkeypatch):
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[]))
    i = FI()
    run(v.open_collection(i))
    assert "Adopt me" in i.sent[0][0] and i.sent[0][1]["ephemeral"] is True


def test_collection_browsing_wraps_around_and_is_owner_only(monkeypatch):
    cats = [mkcat(1, name="A1"), mkcat(2, name="B2"), mkcat(3, name="C3")]
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), cats)
    i = FI(10)
    run(view._prev(i))
    assert view.current.name == "C3" and "Cat 3 of 3" in i.edits[0]["embed"].footer.text
    run(view._next(FI(10)))
    assert view.current.name == "A1"
    stranger = FI(11)
    assert run(view.interaction_check(stranger)) is False and "someone else" in stranger.sent[0][0]


def test_release_needs_two_taps(monkeypatch):
    rel = AsyncMock(return_value=True)
    monkeypatch.setattr(ca, "release", rel)
    monkeypatch.setattr(ca, "list_cats", AsyncMock(return_value=[mkcat(2, name="B2")]))
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(1), mkcat(2, name="B2")])
    run(view._release(FI(10)))
    rel.assert_not_awaited() and view.confirm_release
    assert any(getattr(c, "label", "") == "Tap again to release" for c in view.children)
    i = FI(10)
    run(view._release(i))
    rel.assert_awaited_once_with(1, 10)
    assert "new home" in i.edits[0]["content"]


def test_trading_is_blocked_in_dms():
    view = v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat()])
    i = FI(10, guild=False)
    run(view._trade(i))
    assert "server channel" in i.sent[0][0]


# ── trade offers ──────────────────────────────────────────────────────────

def _offer(take=True):
    return v.TradeOfferView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10, mention="<@10>"),
                            SimpleNamespace(id=20, mention="<@20>"), mkcat(1, owner=10, name="Mine"),
                            mkcat(2, owner=20, name="Theirs") if take else None)


def _offer_interaction(uid):
    return FI(uid, SimpleNamespace(embeds=[discord.Embed(title="t", description="d")]))


def test_only_the_recipient_can_accept_or_decline_and_only_the_sender_can_cancel(monkeypatch):
    t = AsyncMock(return_value="ok")
    monkeypatch.setattr(ca, "trade", t)
    o = _offer()
    for fn, who in ((o._accept, 10), (o._accept, 99), (o._decline, 10), (o._cancel, 20)):
        i = _offer_interaction(who)
        run(fn(i))
        assert i.sent and i.sent[0][1]["ephemeral"] is True and not i.edits
    t.assert_not_awaited()


def test_accepting_a_swap_runs_one_atomic_trade(monkeypatch):
    t = AsyncMock(return_value="ok")
    monkeypatch.setattr(ca, "trade", t)
    i = _offer_interaction(20)
    run(_offer()._accept(i))
    t.assert_awaited_once_with(1, 10, 2, 20, None, taker_clone_id=None)
    assert "Trade done" in i.edits[0]["embed"].description and i.edits[0]["view"] is None


def test_accepting_a_gift(monkeypatch):
    t = AsyncMock(return_value="ok")
    monkeypatch.setattr(ca, "trade", t)
    i = _offer_interaction(20)
    run(_offer(take=False)._accept(i))
    t.assert_awaited_once_with(1, 10, None, 20, None, taker_clone_id=None)
    assert "gifted" in i.edits[0]["embed"].description


@pytest.mark.parametrize("status,word", [("gone", "changed owners"), ("limit", "home is full")])
def test_failed_trades_say_why(monkeypatch, status, word):
    monkeypatch.setattr(ca, "trade", AsyncMock(return_value=status))
    i = _offer_interaction(20)
    run(_offer()._accept(i))
    assert word in i.edits[0]["embed"].description


def test_decline_and_cancel_move_nothing(monkeypatch):
    t = AsyncMock()
    monkeypatch.setattr(ca, "trade", t)
    i = _offer_interaction(20); run(_offer()._decline(i)); assert "declined" in i.edits[0]["embed"].description
    j = _offer_interaction(10); run(_offer()._cancel(j)); assert "cancelled" in j.edits[0]["embed"].description
    t.assert_not_awaited()


def test_sending_an_offer_posts_it_publicly_and_pings_only_the_target(monkeypatch):
    target = SimpleNamespace(id=20, mention="<@20>", display_name="bob", bot=False)
    view = v.TradeAskView(v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(1)]),
                          target, [mkcat(2, owner=20, name="Theirs")])
    view.pick._values = ["2"]
    i = FI(10)
    run(view._chosen(i))
    sent = i.channel.send.await_args.kwargs
    assert sent["content"] == "<@20>" and sent["allowed_mentions"].users == [target]
    assert "in exchange for" in sent["embed"].description and "no real money" in sent["embed"].description
    assert "Offer sent" in i.edits[0]["content"]


def test_gift_option_is_first_and_swap_choices_page_through_every_cat():
    """40 cats used to be cut off at 24. Now every cat is reachable: 23 per page plus the gift slot."""
    cats = [mkcat(i, owner=20, name=f"C{i}") for i in range(2, 42)]            # 40 cats
    view = v.TradeAskView(v.CollectionView(SimpleNamespace(clone_id=None), SimpleNamespace(id=10), [mkcat(1)]),
                          SimpleNamespace(id=20), cats)
    assert view.pick.options[0].value == v.GIFT_VALUE and len(view.pick.options) == 24
    first = {o.value for o in view.pick.options[1:]}
    view.page = 1
    view._build()
    second = {o.value for o in view.pick.options[1:]}
    assert not first & second and len(first) == 23
    view.page = 1; view._build()
    assert len({o.value for o in view.pick.options[1:]}) == 17                 # 40 - 23 on the last page
    assert view.their[cats[-1].id] is cats[-1]                                 # still choosable
