from modules.catch_setup import CatchSetup, create_wild_zone_name, test_spawn_payload


def test_speed_presets_update_both_spawn_controls():
    setup = CatchSetup().with_speed("fast")
    assert setup.spawn_every_n_messages == 12
    assert setup.min_seconds_between_spawns == 45


def test_row_round_trip_and_status():
    setup = CatchSetup.from_row({"enabled": True, "spawn_channel_ids": [3], "speed_preset": "slow"})
    assert setup.spawn_channel_ids == (3,)
    assert "Creature catching: **on**" in setup.status_line()


def test_enabled_requires_spawn_channel():
    assert CatchSetup(enabled=True).validate() == ["at least one spawn channel is required"]


def test_channel_ids_must_be_positive():
    setup = CatchSetup(spawn_channel_ids=(0,), encounter_channel_ids=(-4,))
    assert setup.validate() == ["channel IDs must be positive"]


def test_wild_zone_name_is_unique():
    assert create_wild_zone_name({"general"}) == "wild-zone"
    assert create_wild_zone_name({"wild-zone", "wild-zone-2"}) == "wild-zone-3"


def test_test_spawn_is_dry_run():
    assert test_spawn_payload(species_id=7, channel_id=9) == {
        "species_id": 7, "channel_id": 9, "source": "test", "resolved": False,
    }
