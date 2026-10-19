"""Phase 6: Market duel = Arena challenge to the seller with the same wager from both (no new money path)."""
import asyncio
from types import SimpleNamespace

from discord_bot.cogs import _views_cat_arena as va
from discord_bot.cogs import _views_cat_market as vm


class FakeRes:
    def __init__(self):
        self.sent, self.deferred = [], False

    async def send_message(self, t, **k):
        self.sent.append(t)

    async def defer(self, **k):
        self.deferred = True


def test_challenge_view_prefills_target_and_fits_budget():
    parent = va.ArenaView()
    v = va.ChallengeView(parent, [], "note", target=77)
    assert v.target == 77 and "<@77>" in v.text()
    assert len(list(v.walk_children())) <= 40


def test_open_duel_requires_arena_on(monkeypatch):
    shown = {}

    async def reload(self, i):
        self.st = va.arena.ArenaState()
        self._build()

    async def edit(i, view=None, **k):
        shown["v"] = view
    monkeypatch.setattr(va.ArenaView, "reload", reload)
    monkeypatch.setattr(va.vis, "edit", edit)
    asyncio.run(va.open_duel(SimpleNamespace(user=SimpleNamespace(id=1), client=SimpleNamespace(clone_id=None)), 9))
    assert isinstance(shown["v"], va.ArenaView) and "Arena on" in shown["v"].note


def test_cannot_duel_yourself():
    src = open(vm.__file__).read()
    assert "async def _duel" in src and "That is your own listing." in src
    assert "Duel the seller" in src
