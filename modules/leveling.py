"""
XP curve for discord_bot/cogs/leveling.py. Uses the same cumulative-XP
formula popularized by MEE6 (xp needed to go from level n to n+1 grows
roughly quadratically) since it's a well-understood, already-tuned curve
rather than inventing a new one from scratch.
"""


def xp_for_level(level: int) -> int:
    """Total cumulative XP required to REACH this level from level 0."""
    total = 0
    for n in range(level):
        total += 5 * (n ** 2) + 50 * n + 100
    return total


def compute_level(total_xp: int) -> int:
    """Highest level whose xp_for_level(level) <= total_xp."""
    level = 0
    while xp_for_level(level + 1) <= total_xp:
        level += 1
    return level


def xp_progress(total_xp: int) -> dict:
    """Returns {level, current_xp_in_level, xp_needed_for_next_level} for
    rendering a /rank progress bar."""
    level = compute_level(total_xp)
    floor = xp_for_level(level)
    ceiling = xp_for_level(level + 1)
    return {
        "level": level,
        "current_xp_in_level": total_xp - floor,
        "xp_needed_for_next_level": ceiling - floor,
        "total_xp": total_xp,
    }
