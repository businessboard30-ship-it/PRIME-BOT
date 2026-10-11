"""Opt-in public posts for the wishlist and the leaderboard (play game only).

Both happen only because the player tapped a button for it. Wording is built here from sanitised text so no mention can
ping (the posts also go out with mentions disabled), and the limits keep one active player from flooding a channel.
The leaderboard post only ever shows the collectors board (cats and points), never coins or diamonds.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Optional

from modules import cat_wishlist

BOARD_GAP = 600          # seconds between two public leaderboard posts in the same channel, and by the same player
_wish_day: dict = {}
_board_last: dict = {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def wish_text(mention: str, rarity: str, trait: Optional[str], note: str = "") -> str:
    """\U0001F50E <@1> is looking for a legendary Cuddly cat. The note is cleaned again here (plain text only)."""
    clean = cat_wishlist.clean_note(note) or ""
    want = cat_wishlist.Want(id=0, owner_id=0, clone_id=None, rarity=rarity, trait=trait, note=clean)
    return f"\U0001F50E {mention} is looking for {want.text()}\nGot a match? Open **My cats**, then Trade / gift."


def wish_ok(owner_id: int, day: Optional[date] = None) -> bool:
    """One public want per person per UTC day."""
    return _wish_day.get(owner_id) != (day or _today())


def mark_wish(owner_id: int, day: Optional[date] = None) -> None:
    if len(_wish_day) > 5000:
        _wish_day.clear()
    _wish_day[owner_id] = day or _today()


def board_wait(channel_id, user_id: int, now: Optional[float] = None) -> float:
    """Seconds until this channel / player may post the leaderboard again (0 = go ahead)."""
    t = time.monotonic() if now is None else now
    last = max(_board_last.get(("c", channel_id), -1e9), _board_last.get(("u", user_id), -1e9))
    return max(0.0, BOARD_GAP - (t - last))


def mark_board(channel_id, user_id: int, now: Optional[float] = None) -> None:
    if len(_board_last) > 5000:
        _board_last.clear()
    t = time.monotonic() if now is None else now
    _board_last[("c", channel_id)] = t
    _board_last[("u", user_id)] = t


def board_text(rows) -> str:
    """Top collectors only: names (mentions, no ping), cats, points. No 'you' line, no wallet numbers."""
    from modules import cat_board
    return "\U0001F3C6 **Top cat collectors**\n" + cat_board.format_board(rows, "collectors", None)
