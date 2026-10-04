import random
from datetime import datetime, timezone

import pytest

from modules.catch_spawn import roll_spawn, spawn_embed_data


SPECIES = [
    {"id": 1, "name": "Emberling", "slug": "emberling", "rarity": "common", "spawn_weight": 100, "art_key": "emberling"},
    {"id": 2, "name": "Rarefin", "slug": "rarefin", "rarity": "rare", "spawn_weight": 0, "exclusive_to": "fishing"},
]


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
