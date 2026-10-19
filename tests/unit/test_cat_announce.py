"""Public cat announcements: wording, never pings, never breaks the action, hooked in where it matters."""
import asyncio
from discord_bot.cogs._views_cat_adopt import view_text
from pathlib import Path

import discord

from modules import cat_announce as an

ROOT = Path(__file__).resolve().parents[2]


def run(c):
    return asyncio.run(c)


class Inter:
    def __init__(self, guild=True, followup_fails=False, channel_fails=False):
        self.guild = object() if guild else None
        self.sent = []
        self.followup = self
        self._ff, self._cf = followup_fails, channel_fails
        self.channel = type("Ch", (), {"send": self._chsend})()

    async def send(self, **kw):
        if self._ff:
            raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom")
        self.sent.append(("followup", kw))

    async def _chsend(self, **kw):
        if self._cf:
            raise RuntimeError("no perms")
        self.sent.append(("channel", kw))


def test_wording():
    assert "LEGENDARY" in an.adopt_text("<@1>", "Mochi", 5, "\U0001F451 Legendary", "Brave", "legendary")
    plain = an.adopt_text("<@1>", "Mochi", 5, "\U0001F431 Common", "Brave", "common")
    assert "LEGENDARY" not in plain and "Cat #5" in plain and "<@1>" in plain
    assert "Rank 2" in an.rank_up_text("<@1>", "Mochi", 5, 2) and "top rank" not in an.rank_up_text("<@1>", "Mochi", 5, 2)
    assert "top rank" in an.rank_up_text("<@1>", "Mochi", 5, 25) and "top rank" not in an.rank_up_text("<@1>", "Mochi", 5, 5)
    s = an.sale_text("<@2>", "<@1>", "Mochi", 5, 3, 1500)
    assert "1,500 coins" in s and "Rank 3" in s and "<@1>" in s
    assert "Rank" not in an.sale_text("<@2>", "<@1>", "Mochi", 5, 0, 99)        # nothing to brag about at rank 0


def test_posts_publicly_without_pinging():
    i = Inter()
    assert run(an.announce(i, "hi")) is True
    kind, kw = i.sent[0]
    assert kind == "followup" and kw["ephemeral"] is False and "embed" not in kw
    assert isinstance(kw["view"], discord.ui.LayoutView) and view_text(kw["view"]) == "hi"
    assert kw["allowed_mentions"].users is False and kw["allowed_mentions"].roles is False


def test_falls_back_to_the_channel_and_never_raises():
    i = Inter(followup_fails=True)
    assert run(an.announce(i, "hi")) is True and i.sent[0][0] == "channel"
    assert run(an.announce(Inter(followup_fails=True, channel_fails=True), "hi")) is False


def test_silent_in_dms():
    i = Inter(guild=False)
    assert run(an.announce(i, "hi")) is False and not i.sent


def test_announcements_are_hooked_in_where_people_expect_them():
    adopt = (ROOT / "discord_bot/cogs/_views_cat_adopt.py").read_text()
    market = (ROOT / "discord_bot/cogs/_views_cat_market.py").read_text()
    assert "cat_announce.adopt_text(" in adopt                                            # public adoption line
    i = adopt.index("cat_announce.rank_up_text(")
    assert 'if res.status == "ok":' in adopt[i - 120:i]                                  # only when it worked
    assert "cat_announce.sale_text(" in market                                           # market browser purchases
