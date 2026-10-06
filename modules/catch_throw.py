"""Ball and berry choices for the P2-04 catch interaction."""

from __future__ import annotations

from dataclasses import dataclass

from modules.catch_game import BALLS, BAIT_BONUS, Rarity, catch_chance


@dataclass(frozen=True)
class ThrowChoice:
    ball: str
    bait: str | None = None


def available_items(inventory: dict[str, int]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return owned usable balls and berries in stable game order."""
    balls = tuple(key for key in BALLS if inventory.get(key, 0) > 0)
    berries = tuple(key for key in BAIT_BONUS if inventory.get(key, 0) > 0)
    return balls, berries


def validate_choice(choice: ThrowChoice, inventory: dict[str, int]) -> None:
    if choice.ball not in BALLS:
        raise ValueError("unknown ball")
    if inventory.get(choice.ball, 0) < 1:
        raise ValueError("ball is not available")
    if choice.bait is not None:
        if choice.bait not in BAIT_BONUS:
            raise ValueError("unknown berry")
        if inventory.get(choice.bait, 0) < 1:
            raise ValueError("berry is not available")


def preview_chance(tier: Rarity, *, level: int, choice: ThrowChoice, streak: int = 0, shiny: bool = False) -> float:
    return catch_chance(tier, ball=choice.ball, bait=choice.bait, level=level, streak=streak, shiny=shiny)


def consume_items(choice: ThrowChoice, inventory: dict[str, int]) -> dict[str, int]:
    """Return a decremented copy; database inventory mutation belongs to the service."""
    validate_choice(choice, inventory)
    updated = dict(inventory)
    updated[choice.ball] -= 1
    if choice.bait is not None:
        updated[choice.bait] -= 1
    return updated


def choice_label(key: str) -> str:
    return key.removeprefix("capsule_").removesuffix("berry").replace("_", " ").title()


__all__ = ["ThrowChoice", "available_items", "choice_label", "consume_items", "preview_chance", "validate_choice"]
