"""Market voice messages, Discord side (R2b): wait window, incoming clip handling, card and Voice screen budgets, sweeper,
static checks. No live Discord."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import discord

from discord_bot.cogs import _views_market_voice as mvv
from modules import market_voice as mv

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


def count(view):
    return len(list(view.walk_children()))


class Chan:
    def __init__(self):
        self.sent = []

    async def send(self, text=None, **k):
        self.sent.append(text)


def msg(uid=20, voice=True, guild=None, size=1000):
    att = SimpleNamespace(is_voice_message=lambda: voice, duration=5.0, size=size, read=_read)
    m = SimpleNamespace(author=SimpleNamespace(id=uid, bot=False), guild=guild, attachments=[att], channel=Chan())
    m.deleted = False

    async def _del():
        raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
    m.delete = _del
    return m


async def _read():
    return b"OggS" + b"\x00" * 100


def test_wait_window_is_one_shot_and_expires(monkeypatch):
    mvv._waiting.clear()
    mvv.start_wait(20, 7)
    assert mvv._take_wait(20) == 7
    assert mvv._take_wait(20) is None
    mvv.start_wait(21, 7)
    mvv._waiting[21] = (7, 0.0)
    assert mvv._take_wait(21) is None


def test_ignores_clip_when_not_waiting_or_in_server():
    mvv._waiting.clear()
    assert run(mvv.handle_incoming(None, msg())) is False
    mvv.start_wait(20, 7)
    assert run(mvv.handle_incoming(None, msg(guild=object()))) is False
    assert run(mvv.handle_incoming(None, msg(voice=False))) is False
    assert 20 in mvv._waiting            # wait not used up by a wrong message


def test_incoming_stores_and_tells_buyer_and_drops_bytes(monkeypatch):
    mvv._waiting.clear()
    mvv.start_wait(20, 7)
    seen = {}

    async def fake_send(sender, listing, audio, dur):
        seen["args"] = (sender, listing, len(audio), dur)
        return mv.SendResult("ok", 5, 10)

    async def fake_deliver(client, vid, rid):
        return False
    monkeypatch.setattr(mv, "send", fake_send)
    monkeypatch.setattr(mvv, "_deliver_card", fake_deliver)
    m = msg()
    assert run(mvv.handle_incoming(None, m)) is True
    assert seen["args"] == (20, 7, 104, 5000)
    text = m.channel.sent[-1]
    assert "Voice screen" in text and "delete your own" in text       # DM: bot can't delete, says so


def test_incoming_rule_failure_is_reported(monkeypatch):
    mvv._waiting.clear()
    mvv.start_wait(20, 7)

    async def fake_send(*a):
        return mv.SendResult("off")
    monkeypatch.setattr(mv, "send", fake_send)
    m = msg()
    run(mvv.handle_incoming(None, m))
    assert "not taking voice" in m.channel.sent[-1]


def test_card_and_screen_budgets():
    v = mvv.card_layout(1, 20, 5000)
    assert count(v) <= 40
    pending = [mv.Pending(i, 20, 3, 5000, None, None) for i in range(1, 11)]
    s = mvv.VoiceView(SimpleNamespace(id=10), True, pending)
    assert count(s) <= 40
    total = sum(len(c.content) for c in s.walk_children() if isinstance(c, discord.ui.TextDisplay))
    assert total <= 4000


def test_sweeper_deletes_messages_then_marks(monkeypatch):
    calls = []

    async def due():
        return [(1, 11, 111)]

    async def mark(v):
        calls.append(("mark", v))

    async def exp():
        return [(2, 12, 112)]

    async def purge():
        return []

    async def prune():
        calls.append("prune")
        return 0

    async def dele(client, pairs):
        calls.append(("del", list(pairs)))
    monkeypatch.setattr(mv, "due_to_delete", due)
    monkeypatch.setattr(mv, "mark_deleted", mark)
    monkeypatch.setattr(mv, "expire_due", exp)
    monkeypatch.setattr(mv, "purge_closed_listings", purge)
    monkeypatch.setattr(mv, "prune", prune)
    monkeypatch.setattr(mvv, "delete_messages", dele)
    run(mvv.sweep(None))
    assert ("del", [(11, 111)]) in calls and ("mark", 1) in calls and ("del", [(12, 112)]) in calls and "prune" in calls


def test_static_wiring_and_no_audio_logging():
    src = (ROOT / "discord_bot/cogs/_views_market_voice.py").read_text()
    assert "market_voice:" in src
    for ln in src.splitlines():
        if "logger." in ln:
            assert "audio" not in ln.lower().replace("couldn't send", "") or "played audio failed" in ln
    adopt = (ROOT / "discord_bot/cogs/_views_cat_adopt.py").read_text()
    assert "_mvoice.DYNAMIC_ITEMS" in adopt
    cat = (ROOT / "discord_bot/cogs/cat.py").read_text()
    assert "_voice_sweep.start()" in cat and "_voice_sweep.cancel()" in cat
    assert re.search(r"def on_message", cat)
