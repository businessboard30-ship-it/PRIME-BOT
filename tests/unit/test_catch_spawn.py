import random
from datetime import datetime, timezone

import pytest

from modules.catch_spawn import (
    SpawnRoll,
    attach_spawn_message,
    create_spawn,
    roll_spawn,
    spawn_embed_data,
)


SPECIES = [
    {"id": 1, "name": "Emberling", "slug": "emberling", "rarity": "common", "spawn_weight": 100, "art_key": "emberling"},
    {"id": 2, "name": "Rarefin", "slug": "rarefin", "rarity": "rare", "spawn_weight": 0, "exclusive_to": "fishing"},
]


class FakeTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, *, inserted_id=41, update_result="UPDATE 1"):
        self.inserted_id = inserted_id
        self.update_result = update_result
        self.fetchval_calls = []
        self.execute_calls = []

    def transaction(self):
        return FakeTransaction(self)

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        return self.inserted_id

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return self.update_result


def test_roll_spawn_excludes_exclusive_species_and_has_valid_rolls():
    result = roll_spawn(SPECIES, random.Random(4), shiny_odds=1)
    assert result.species_id == 1
    assert result.rarity == "common"
    assert result.level >= 2
    assert len(result.ivs) == 5
    assert result.shiny is True


def test_roll_spawn_rejects_empty_or_zero_weight_roster():
    with pytest.raises(ValueError, match="no enabled"):
        roll_spawn([], random.Random(1))
    with pytest.raises(ValueError, match="weights"):
        roll_spawn([{**SPECIES[0], "spawn_weight": 0}], random.Random(1))


def test_spawn_embed_data_has_relative_expiry_and_asset_variant():
    result = roll_spawn(SPECIES, random.Random(2), shiny_odds=1)
    data = spawn_embed_data(result, expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert data["title"] == "A wild creature appeared!"
    assert data["expires_at"].startswith("<t:")
    assert data["asset_key"] == "creature:emberling:shiny"


@pytest.mark.asyncio
async def test_create_spawn_persists_roll_before_message_attachment():
    connection = FakeConnection(inserted_id=73)
    roll = SpawnRoll(1, "Emberling", "emberling", "common", 7, False, (1, 2, 3, 4, 5), "emberling")

    spawn_id = await create_spawn(
        guild_id=10,
        clone_id=None,
        channel_id=20,
        roll=roll,
        expires_in=60,
        conn=connection,
    )

    assert spawn_id == 73
    assert len(connection.fetchval_calls) == 1
    query, args = connection.fetchval_calls[0]
    assert "INSERT INTO catch_spawns" in query
    assert args[:5] == (10, None, 20, 1, 7)
    assert args[5:9] == (False, [1, 2, 3, 4, 5], "chat", None)


@pytest.mark.asyncio
async def test_attach_spawn_message_rejects_duplicate_attachment():
    connection = FakeConnection(update_result="UPDATE 0")

    with pytest.raises(ValueError, match="already attached"):
        await attach_spawn_message(73, 9001, conn=connection)

    assert connection.execute_calls[0][1] == (73, 9001)


@pytest.mark.asyncio
async def test_spawn_rejects_invalid_scope_and_source_before_database_call():
    connection = FakeConnection()
    roll = SpawnRoll(1, "Emberling", "emberling", "common", 7, False, (1, 2, 3, 4, 5), "emberling")

    with pytest.raises(ValueError, match="positive"):
        await create_spawn(guild_id=0, clone_id=None, channel_id=20, roll=roll, conn=connection)
    with pytest.raises(ValueError, match="invalid spawn source"):
        await create_spawn(guild_id=10, clone_id=None, channel_id=20, roll=roll, source="bad", conn=connection)
    assert connection.fetchval_calls == []
