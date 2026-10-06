import copy

from modules.catch_species import load_file, validate


def test_roster_is_valid():
    species = load_file()
    assert len(species) == 48
    assert validate(species) == []


def test_validate_duplicate_and_evolution_errors():
    species = copy.deepcopy(load_file())
    species[1]["id"] = species[0]["id"]
    species[1]["slug"] = species[0]["slug"]
    species[0]["evolves_to"] = species[0]["id"]
    errors = validate(species)
    assert any("duplicate id" in error for error in errors)
    assert any("duplicate slug" in error for error in errors)
    assert any("cannot evolve into itself" in error for error in errors)
