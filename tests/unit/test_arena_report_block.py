"""Report / Block on Arena result screens."""
import asyncio
from types import SimpleNamespace

from discord_bot.cogs import _views_cat_arena as va
from modules import cat_requests, ops_reports


class Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, **k):
        self.sent.append(text)


def res(fight):
    return SimpleNamespace(fight=fight)


def test_no_fight_no_buttons():
    assert va.report_buttons(res(None), 1) == []


def test_buttons_target_the_other_player(monkeypatch):
    seen = {}

    async def rep(fid, uid):
        seen["rep"] = (fid, uid)
        return "ok"

    async def blk(owner, foe):
        seen["blk"] = (owner, foe)
        return True

    async def sub(bot, key, text, cooldown=0):
        seen["ops"] = key
    monkeypatch.setattr(cat_requests, "report", rep)
    monkeypatch.setattr(cat_requests, "block", blk)
    monkeypatch.setattr(ops_reports, "submit", sub)
    btns = va.report_buttons(res(SimpleNamespace(id=9, challenger_id=1, challenged_id=2)), 2)
    assert len(btns) == 2
    i = SimpleNamespace(user=SimpleNamespace(id=2), client=object(), response=Resp())
    asyncio.run(btns[0].callback(i))
    asyncio.run(btns[1].callback(i))
    assert seen["rep"] == (9, 2) and seen["blk"] == (2, 1) and seen["ops"] == "fight-report-9"


def test_result_screen_stays_in_budget():
    btns = va.report_buttons(res(SimpleNamespace(id=9, challenger_id=1, challenged_id=2)), 2)
    v = va._screen("x" * 1500, [va.discord.ui.ActionRow(*btns, va._btn("Back to Arena", None))])
    assert len(list(v.walk_children())) <= 40
