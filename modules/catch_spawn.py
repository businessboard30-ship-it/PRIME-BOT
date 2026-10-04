"""Wild spawn creation primitives (P2-02).

The roll is pure and deterministic when supplied a random generator. Persistence
is a single parameterized insert so a caller can publish the Discord message
and update its message id without duplicating the creature roll.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import random
from typing import Any

from modules import catch_db
from modules.catch_game import RARITY_BY_CODE, roll_ivs, roll_level, roll_shiny

DEFAULT_LIFETIME_SECONDS = 300

@dataclass(frozen=True, slots=True)
class SpawnRoll:
    species_id: int
    species_name: str
    slug: str
    rarity: str
    level: int
    shiny: bool
    ivs: tuple[int, ...]
    asset_key: str

    @property
    def display_name(self) -> str:
        return f"Shiny {self.species_name}" if self.shiny else self.species_name

def roll_spawn(species: list[dict[str, Any]], rng: random.Random, *, shiny_odds: int = 512) -> SpawnRoll:
    """Choose an enabled wild species and roll its encounter attributes."""
    candidates = [sp for sp in species if sp.get("enabled", True) and not sp.get("exclusive_to")]
    if not candidates:
        raise ValueError("no enabled wild species are available")
    weights = [max(0, int(sp.get("spawn_weight", 0))) for sp in candidates]
    if sum(weights) <= 0:
        raise ValueError("wild species weights must sum to a positive number")
    pick = rng.randrange(sum(weights))
    chosen = candidates[0]
    for candidate, weight in zip(candidates, weights):
        pick -= weight
        if pick < 0:
            chosen = candidate
            break
    rarity_value = chosen["rarity"]
    tier = RARITY_BY_CODE[int(rarity_value)] if isinstance(rarity_value, int) else next(
        tier for tier in RARITY_BY_CODE.values() if tier.key == rarity_value
    )
    return SpawnRoll(int(chosen["id"]), str(chosen["name"]), str(chosen["slug"]), tier.key,
                     roll_level(rng, tier), roll_shiny(rng, shiny_odds), tuple(roll_ivs(rng)),
                     str(chosen.get("art_key") or chosen["slug"]))

def spawn_embed_data(roll: SpawnRoll, *, expires_at: datetime) -> dict[str, str]:
    """Return safe presentation data for the public spawn message."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return {"title": "A wild creature appeared!",
            "description": f"A **{roll.display_name}** is hiding in the wild. Claim it before it runs away.",
            "rarity": roll.rarity.title(), "level": str(roll.level),
            "expires_at": f"<t:{int(expires_at.timestamp())}:R>",
            "asset_key": f"creature:{roll.asset_key}:{'shiny' if roll.shiny else 'normal'}"}

async def create_spawn(*, guild_id: int, clone_id: int | None, channel_id: int, roll: SpawnRoll,
                       expires_in: int = DEFAULT_LIFETIME_SECONDS, source: str = "chat", owner_user_id: int | None = None, conn=None) -> int:
    """Persist a rolled spawn and return its id before publishing the message."""
    if guild_id <= 0 or channel_id <= 0 or expires_in <= 0:
        raise ValueError("guild, channel, and expiry must be positive")
    if source not in {"chat", "encounter", "fishing", "test"}:
        raise ValueError("invalid spawn source")
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    async with catch_db.transaction(conn) as db:
        return int(await db.fetchval(
            """INSERT INTO catch_spawns
                (guild_id, clone_id, channel_id, species_id, level, shiny, ivs, source, owner_user_id, expires_at)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
             RETURNING id""",
            guild_id, clone_id, channel_id, roll.species_id, roll.level,
            roll.shiny, list(roll.ivs), source, owner_user_id, expires_at,
        ))


async def attach_spawn_message(spawn_id: int, message_id: int, *, conn=None) -> None:
    if spawn_id <= 0 or message_id <= 0:
        raise ValueError("spawn and message ids must be positive")
    async with catch_db.transaction(conn) as db:
        updated = await db.execute(
            "UPDATE catch_spawns SET message_id=$2 WHERE id=$1 AND message_id IS NULL",
            spawn_id, message_id,
        )
    if not updated.endswith("1"):
        raise ValueError("spawn message was already attached or does not exist")


__all__ = ["DEFAULT_LIFETIME_SECONDS", "SpawnRoll", "attach_spawn_message", "create_spawn", "roll_spawn", "spawn_embed_data"]
