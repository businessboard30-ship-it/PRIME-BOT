"""R2c: reports reach the owner's bot-health channel (ids only, never audio)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from discord_bot.cogs import _views_market_voice as mvv
from modules import market_voice as mv, ops_reports

ROOT = Path(__file__).resolve().parents[2]


class Resp:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, **k):
        self.sent.append(text)


def _inter():
    return SimpleNamespace(user=SimpleNamespace(id=10), client=object(), response=Resp())


def test_voice_report_queues_owner_note(monkeypatch):
    got = []

    async def rep(v, u):
        return "ok"

    async def sub(bot, key, text, cooldown=0):
        got.append((key, text))
        return True
    monkeypatch.setattr(mv, "report", rep)
    monkeypatch.setattr(ops_reports, "submit", sub)
    i = _inter()
    asyncio.run(mvv.ReportButton(5).callback(i))
    assert got and got[0][0] == "voice-report-5" and "audio" in got[0][1] and "Reported" in i.response.sent[0]


def test_repeat_report_queues_nothing(monkeypatch):
    got = []

    async def rep(v, u):
        return "again"

    async def sub(*a, **k):
        got.append(1)
    monkeypatch.setattr(mv, "report", rep)
    monkeypatch.setattr(ops_reports, "submit", sub)
    asyncio.run(mvv.ReportButton(5).callback(_inter()))
    assert not got


def test_fight_report_wired():
    src = (ROOT / "discord_bot/cogs/_views_cat_board.py").read_text()
    assert "fight-report-" in src
