"""Owner-facing catch setup state and validation (P1-08)."""
from __future__ import annotations

from dataclasses import dataclass, replace

from modules.catch_db import connection

SPEED_PRESETS = {
    "slow": {"spawn_every_n_messages": 40, "min_seconds_between_spawns": 180},
    "normal": {"spawn_every_n_messages": 25, "min_seconds_between_spawns": 90},
    "fast": {"spawn_every_n_messages": 12, "min_seconds_between_spawns": 45},
}

@dataclass(frozen=True)
class CatchSetup:
    enabled: bool = False
    join_dm_enabled: bool = True
    spawn_channel_ids: tuple[int, ...] = ()
    encounter_channel_ids: tuple[int, ...] = ()
    announce_channel_id: int | None = None
    rare_ping_role_id: int | None = None
    speed_preset: str = "normal"
    spawn_every_n_messages: int = 25
    min_seconds_between_spawns: int = 90
    despawn_seconds: int = 300

    @classmethod
    def from_row(cls, row: dict | None) -> "CatchSetup":
        row = row or {}
        preset = row.get("speed_preset", "normal")
        if preset not in SPEED_PRESETS:
            preset = "normal"
        return cls(
            enabled=bool(row.get("enabled", False)), join_dm_enabled=bool(row.get("join_dm_enabled", True)),
            spawn_channel_ids=tuple(int(v) for v in (row.get("spawn_channel_ids") or ())),
            encounter_channel_ids=tuple(int(v) for v in (row.get("encounter_channel_ids") or ())),
            announce_channel_id=row.get("announce_channel_id"), rare_ping_role_id=row.get("rare_ping_role_id"),
            speed_preset=preset,
            spawn_every_n_messages=int(row.get("spawn_every_n_messages", SPEED_PRESETS[preset]["spawn_every_n_messages"])),
            min_seconds_between_spawns=int(row.get("min_seconds_between_spawns", SPEED_PRESETS[preset]["min_seconds_between_spawns"])),
            despawn_seconds=int(row.get("despawn_seconds", 300)),
        )

    def with_speed(self, preset: str) -> "CatchSetup":
        if preset not in SPEED_PRESETS:
            raise ValueError("speed preset must be slow, normal, or fast")
        return replace(self, speed_preset=preset, **SPEED_PRESETS[preset])

    def with_spawn_channels(self, channel_ids) -> "CatchSetup":
        return replace(self, spawn_channel_ids=tuple(dict.fromkeys(int(v) for v in channel_ids)))

    def validate(self) -> list[str]:
        errors = []
        if self.speed_preset not in SPEED_PRESETS:
            errors.append("invalid speed preset")
        if self.enabled and not self.spawn_channel_ids:
            errors.append("at least one spawn channel is required")
        if self.despawn_seconds <= 0:
            errors.append("despawn_seconds must be positive")
        return errors

    def status_line(self) -> str:
        state = "on" if self.enabled else "off"
        channels = len(self.spawn_channel_ids)
        return f"Creature catching: **{state}** · {channels} spawn channel{'s' if channels != 1 else ''} · {self.speed_preset.title()}"

    def to_db_values(self, guild_id: int, clone_id: int | None = None) -> tuple:
        return (
            guild_id, clone_id, self.enabled, list(self.spawn_channel_ids),
            list(self.encounter_channel_ids), self.announce_channel_id,
            self.rare_ping_role_id, self.speed_preset, self.spawn_every_n_messages,
            self.min_seconds_between_spawns, self.despawn_seconds, self.join_dm_enabled,
        )


async def load_setup(guild_id: int, clone_id: int | None = None, *, conn=None) -> CatchSetup:
    async with connection(conn) as db:
        row = await db.fetchrow(
            "SELECT enabled, join_dm_enabled, spawn_channel_ids, encounter_channel_ids, announce_channel_id, rare_ping_role_id, speed_preset, spawn_every_n_messages, min_seconds_between_spawns, despawn_seconds FROM catch_guild_config WHERE guild_id=$1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id,
        )
    return CatchSetup.from_row(dict(row) if row else None)


async def save_setup(guild_id: int, setup: CatchSetup, clone_id: int | None = None, *, conn=None) -> CatchSetup:
    errors = setup.validate()
    if errors:
        raise ValueError("; ".join(errors))
    async with connection(conn) as db:
        await db.execute(
            """INSERT INTO catch_guild_config (guild_id, clone_id, enabled, spawn_channel_ids, encounter_channel_ids, announce_channel_id, rare_ping_role_id, speed_preset, spawn_every_n_messages, min_seconds_between_spawns, despawn_seconds, join_dm_enabled) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT (guild_id, clone_key) DO UPDATE SET enabled=EXCLUDED.enabled, spawn_channel_ids=EXCLUDED.spawn_channel_ids, encounter_channel_ids=EXCLUDED.encounter_channel_ids, announce_channel_id=EXCLUDED.announce_channel_id, rare_ping_role_id=EXCLUDED.rare_ping_role_id, speed_preset=EXCLUDED.speed_preset, spawn_every_n_messages=EXCLUDED.spawn_every_n_messages, min_seconds_between_spawns=EXCLUDED.min_seconds_between_spawns, despawn_seconds=EXCLUDED.despawn_seconds, join_dm_enabled=EXCLUDED.join_dm_enabled, updated_at=now()""",
            *setup.to_db_values(guild_id, clone_id),
        )
    return setup


def test_spawn_payload(*, species_id: int = 1, channel_id: int | None = None) -> dict:
    return {"species_id": int(species_id), "channel_id": channel_id, "source": "test", "resolved": False}


def create_wild_zone_name(existing_names: set[str]) -> str:
    names = {name.casefold() for name in existing_names}
    base = "wild-zone"
    if base not in names:
        return base
    index = 2
    while f"{base}-{index}" in names:
        index += 1
    return f"{base}-{index}"

__all__ = ["CatchSetup", "SPEED_PRESETS", "create_wild_zone_name", "test_spawn_payload"]
