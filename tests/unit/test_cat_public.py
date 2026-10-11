"""Wishlist 'also post in this channel' and the public leaderboard post: opt-in, limited, no pings, no wallets."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from discord_bot.cogs import _views_cat_features as vf
from discord_bot.cogs import _views_cat_market as vm
from modules import cat_board, cat_public, cat_wishlist

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Resp:
    def __init__(self):
        self.sent, self.deferred = [], False

    async def send_message(self, *a, **kw):
        self.sent.append(kw | {"args": a})

    async def defer(self, **kw):
        self.deferred = True


class Inter:
    def __init__(self, uid=7, channel=100, guild=True, fail_public=False):
        self.user = SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name="Max")
        self.guild = SimpleNamespace(id=1) if guild else None
        self.client = SimpleNamespace(clone_id=None)
        self.channel_id = channel
        self.channel = SimpleNamespace(id=channel)
        self.response, self.followup = Resp(), self
        self.public, self.private, self._fail = [], [], fail_public

    async def send(self, *a, **kw):
        if kw.get("ephemeral"):
            self.private.append(kw | {"args": a})
            return
        if self._fail:
            raise RuntimeError("boom")
        self.public.append(kw)


def reset():
    cat_public._wish_day.clear()
    cat_public._board_last.clear()


def submit(inter, status="ok", share=True, note="hello"):
    async def add(*a, **kw):
        return status
    real, cat_wishlist.add = cat_wishlist.add, add

    async def go():
        m = vf.WishlistNoteModal("legendary", "Cuddly", share)
        m.note._value = note
        await m.on_submit(inter)
    try:
        run(go())
    finally:
        cat_wishlist.add = real


def test_wish_text_is_plain_and_has_the_poster():
    t = cat_public.wish_text("<@7>", "legendary", "Cuddly", "swap <@123> @everyone")
    assert t.startswith("\U0001F50E <@7> is looking for") and "legendary" in t and "Cuddly" in t
    assert "<@123>" not in t and "@everyone" not in t
    assert "http" not in cat_public.wish_text("<@7>", "rare", None, "see https://x.io")        # links are dropped


def test_wish_posts_publicly_once_a_day_without_pings():
    reset()
    i = Inter()
    submit(i)
    assert len(i.public) == 1 and i.public[0]["ephemeral"] is False
    assert i.public[0]["allowed_mentions"].users is False and i.public[0]["allowed_mentions"].roles is False
    j = Inter()
    submit(j)                                                   # same person, same day
    assert not j.public and j.private
    assert len(Inter(uid=8).public) == 0
    k = Inter(uid=8)
    submit(k)
    assert len(k.public) == 1                                   # another person is fine


def test_wish_not_posted_when_off_failed_or_in_dms():
    reset()
    a = Inter(); submit(a, share=False); assert not a.public
    b = Inter(); submit(b, status="limit"); assert not b.public and not cat_public._wish_day
    c = Inter(); submit(c, status="duplicate"); assert not c.public
    d = Inter(fail_public=True); submit(d); assert not d.public and not cat_public._wish_day   # a failed post doesn't use the day
    e = Inter(guild=False); submit(e); assert not e.public and not cat_public._wish_day


def test_board_text_has_no_wallets_or_you_line():
    rows = [{"owner_id": 1, "cats": 5, "score": 40, "legendary": 1}, {"owner_id": 2, "cats": 3, "score": 9, "legendary": 0}]
    t = cat_public.board_text(rows)
    assert "Top cat collectors" in t and "<@1>" in t and "40 pts" in t
    assert "coins" not in t.lower() and "You:" not in t


def board_inter(monkeypatch, rows, fail=False):
    async def top(limit, me):
        if fail:
            raise RuntimeError("db")
        assert me is None                                        # the public post never asks for 'my rank'
        return rows, None
    monkeypatch.setattr(cat_board, "top_collectors", top)


def post_board(inter):
    async def go():
        await vm.BoardView(inter.user)._post(inter)
    run(go())


def test_board_post_public_rate_limited_and_safe(monkeypatch):
    reset()
    rows = [{"owner_id": 1, "cats": 5, "score": 40, "legendary": 1}]
    board_inter(monkeypatch, rows)
    i = Inter()
    post_board(i)
    assert len(i.public) == 1 and i.public[0]["allowed_mentions"].users is False
    j = Inter(uid=9)
    post_board(j)                                                # same channel within 10 min
    assert not j.public and j.response.sent
    k = Inter(uid=7, channel=200)
    post_board(k)                                                # same player, other channel, still limited
    assert not k.public
    reset()
    board_inter(monkeypatch, [])
    e = Inter(); post_board(e); assert not e.public
    board_inter(monkeypatch, rows, fail=True)
    f = Inter(); post_board(f); assert not f.public and f.private
    board_inter(monkeypatch, rows)
    g = Inter(fail_public=True); post_board(g); assert not g.public and g.private
    reset()
    assert not Inter(guild=False).public
    d = Inter(guild=False); post_board(d); assert not d.public and d.response.sent


def test_richest_board_has_no_post_button():
    async def go():
        assert any(getattr(c, "label", "") == "Post to channel" for c in vm.BoardView(SimpleNamespace(id=1)).children)
        assert not any(getattr(c, "label", "") == "Post to channel" for c in vm.BoardView(SimpleNamespace(id=1), "richest").children)
    run(go())


def test_wired_in():
    feats = (ROOT / "discord_bot/cogs/_views_cat_features.py").read_text(encoding="utf-8")
    assert "Also post in this channel" in feats and "cat_public.wish_text(" in feats
