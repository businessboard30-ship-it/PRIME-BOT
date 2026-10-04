"""Player-only encounter creation with an atomic cooldown claim."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from modules import catch_db
from modules.catch_spawn import SpawnRoll, create_spawn


class EncounterOnCooldown(Exception):
    def __init__(self, ready_at: datetime):
        super().__init__("encounter_on_cooldown")
        self.ready_at = ready_at


async def claim_encounter_slot(*, user_id: int, clone_id: int | None, cooldown_seconds: int = 300, conn=None) -> datetime:
    if user_id <= 0 or cooldown_seconds <= 0:
        raise ValueError("user_id and cooldown_seconds must be positive")
    ready_at = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
    async with catch_db.transaction(conn) as db:
        row = await db.fetchrow(
            """INSERT INTO catch_players (user_id, clone_id, encounter_ready_at)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id, clone_key) DO UPDATE
               SET encounter_ready_at = EXCLUDED.encounter_ready_at, updated_at = now()
               WHERE catch_players.encounter_ready_at IS NULL OR catch_players.encounter_ready_at <= now()
               RETURNING encounter_ready_at""",
            user_id, clone_id, ready_at,
        )
        if row is None:
            current = await db.fetchval(
                "SELECT encounter_ready_at FROM catch_players WHERE user_id=$1 AND clone_key=$2",
                user_id, catch_db.clone_key(clone_id),
            )
            raise EncounterOnCooldown(current)
        return row["encounter_ready_at"]


async def create_player_encounter(*, user_id: int, clone_id: int | None, guild_id: int, channel_id: int, roll: SpawnRoll, cooldown_seconds: int = 300, conn=None) -> int:
    await claim_encounter_slot(user_id=user_id, clone_id=clone_id, cooldown_seconds=cooldown_seconds, conn=conn)
    return await create_spawn(guild_id=guild_id, clone_id=clone_id, channel_id=channel_id, roll=roll, source="encounter", owner_user_id=user_id, conn=conn)


__all__ = ["EncounterOnCooldown", "claim_encounter_slot", "create_player_encounter"]
