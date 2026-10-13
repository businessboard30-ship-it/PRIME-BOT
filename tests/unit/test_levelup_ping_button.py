"""Level-up message "Pings off" button: registered, owner-only, flips the same opt-out as the leaderboard button."""
import asyncio

from discord_bot.cogs import _views_leveling_leaderboard as lb
from discord_bot.cogs import leveling as lv


def run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **k):
        self.sent.append((a, k))

    async def defer(self, **k):
        pass


class _Follow:
    def __init__(self):
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append((a, k))


def _inter(uid):
    return type("I", (), {"user": type("U", (), {"id": uid})(), "response": _Resp(), "followup": _Follow()})()


def test_registered_and_custom_id():
    assert lb.LevelUpPingButton in lb.DYNAMIC_ITEMS
    view = lb.level_up_ping_view(5, 9)
    assert [c.custom_id for c in view.children] == ["lvlup_ping:5:9"]
    assert view.timeout is None
    muted_btn = lb.LevelUpPingButton(5, 9, muted=True)
    assert muted_btn.item.label == "Pings on"


def test_only_the_member_can_press(monkeypatch):
    calls = []

    async def toggle(g, u):
        calls.append((g, u))
        return True

    monkeypatch.setattr(lb.db, "toggle_level_ping_optout", toggle, raising=False)
    other = _inter(1)
    run(lb.LevelUpPingButton(5, 9).callback(other))
    assert calls == [] and other.response.sent
    me = _inter(9)
    run(lb.LevelUpPingButton(5, 9).callback(me))
    assert calls == [(5, 9)]
    assert "OFF" in me.followup.sent[0][0][0]


def test_button_chance_zero_means_no_view(monkeypatch):
    monkeypatch.setattr(lv, "LEVELUP_PING_BUTTON_CHANCE", 0.0)
    cog = lv.LevelingCog.__new__(lv.LevelingCog)
    member = type("M", (), {"id": 9, "guild": type("G", (), {"id": 5})()})()
    assert run(cog._ping_button_view(member)) is None
