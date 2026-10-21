"""Welcome Card Studio Phase 2a: which gallery picture a newcomer gets (plan section 3.1).

Pure logic only: no database, no Discord. Never raises, so a join can never fail here.
The caller (a later slice) passes the filled slots, the member number and the saved rotation state.
"""
from __future__ import annotations

import random
from typing import Optional, Sequence

MAX_PICTURES = 10

MODE_PIN = "pin"          # same picture every time
MODE_RANDOM = "random"    # random, never the same twice in a row (default)
MODE_ORDER = "order"      # one after another
MODE_MEMBER = "member"    # member number N gets picture N mod count
MODES = (MODE_PIN, MODE_RANDOM, MODE_ORDER, MODE_MEMBER)
DEFAULT_MODE = MODE_RANDOM

# Plain words for the wizard select.
MODE_LABELS = {
    MODE_PIN: "Same picture every time",
    MODE_RANDOM: "Random, never the same twice in a row",
    MODE_ORDER: "In order, one after another",
    MODE_MEMBER: "By member number",
}


def clean_mode(mode: Optional[str]) -> str:
    return mode if mode in MODES else DEFAULT_MODE


def pick_slot(
    mode: Optional[str],
    slots: Sequence[int],
    member_number: Optional[int] = None,
    last_slot: Optional[int] = None,
    counter: int = 0,
    pinned: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> Optional[int]:
    """Return the slot number to use, or None when there are no usable pictures.

    `slots` are the slot numbers that currently hold a usable picture (missing or flagged ones
    already removed by the caller). Order is by slot number.
    """
    try:
        usable = sorted({int(s) for s in slots if 1 <= int(s) <= MAX_PICTURES})
        if not usable:
            return None
        if len(usable) == 1:
            return usable[0]
        mode = clean_mode(mode)
        if mode == MODE_PIN:
            return pinned if pinned in usable else usable[0]
        if mode == MODE_ORDER:
            return usable[int(counter) % len(usable)]
        if mode == MODE_MEMBER:
            n = int(member_number) if member_number else 0
            return usable[n % len(usable)]
        choices = [s for s in usable if s != last_slot] or usable
        return (rng or random).choice(choices)
    except Exception:
        return None
