"""Wild zone: the active spawns a player can see in one server (read-only).

Only spawns that are still claimable are listed: not caught, not fled, not expired. A
personal spawn (``owner_user_id`` set, e.g. an Encounter) is visible only to its owner.
Every query is scoped by guild and ``clone_key``. Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from modules import catch_db
from modules.catch_game import RARITY_BY_KEY
from modules.catch_species import all_species

WILD_LIST_LIMIT = 10
RARITY_MARK = {"common": "⚪", "uncommon": "🟢", "rare": "🔵", "epic": "🟣", "mythic": "🟠"}


@dataclass(frozen=True, slots=True)
class ActiveSpawn:
    id: int
    channel_id: int | None
    message_id: int | None
    name: str
    rarity: str
    level: int
    shiny: bool
    personal: bool
    expires_at: datetime


def jump_url(guild_id: int, channel_id: int | None, message_id: int | None) -> str | None:
    if not channel_id:
        return None
    suffix = f"/{message_id}" if message_id else ""
    return f"https://discord.com/channels/{guild_id}/{channel_id}{suffix}"


async def list_active_spawns(
    guild_id: int, user_id: int, clone_id: int | None, *, limit: int = WILD_LIST_LIMIT, conn=None,
) -> tuple[list[ActiveSpawn], int]:
    """Return (spawns soonest-to-expire first, total live count visible to this player)."""
    key = catch_db.clone_key(clone_id)
    live = (
        "guild_id = $1 AND clone_key = $2 AND caught_by IS NULL AND NOT fled AND expires_at > now() "
        "AND (owner_user_id IS NULL OR owner_user_id = $3)"
    )
    async with catch_db.connection(conn) as db:
        total = int(await db.fetchval(f"SELECT count(*) FROM catch_spawns WHERE {live}", guild_id, key, user_id) or 0)  # noqa: S608 - constant fragment
        records = await db.fetch(
            "SELECT id, channel_id, message_id, species_id, level, shiny, owner_user_id, expires_at "
            f"FROM catch_spawns WHERE {live} ORDER BY expires_at ASC, id ASC LIMIT $4",  # noqa: S608 - constant fragment
            guild_id, key, user_id, limit,
        )
    roster = all_species()
    spawns = []
    for rec in records:
        sp = roster.get(int(rec["species_id"])) or {}
        rarity = sp.get("rarity") if sp.get("rarity") in RARITY_BY_KEY else "common"
        spawns.append(ActiveSpawn(
            int(rec["id"]), rec["channel_id"], rec["message_id"], str(sp.get("name") or "A creature"),
            str(rarity), int(rec["level"]), bool(rec["shiny"]), rec["owner_user_id"] is not None, rec["expires_at"],
        ))
    return spawns, total


__all__ = ["ActiveSpawn", "WILD_LIST_LIMIT", "jump_url", "list_active_spawns", "RARITY_MARK"]
