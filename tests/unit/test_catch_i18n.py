import json
from pathlib import Path

from modules.catch_i18n import clear_cache, text


def test_unknown_locale_falls_back_to_english():
    clear_cache()
    assert text("hub.title", locale="xx") == "Catch hub"


def test_missing_key_returns_namespaced_key():
    clear_cache()
    assert text("missing.key") == "catch.missing.key"


def test_locale_files_are_valid_json():
    for path in Path("locales").glob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def test_encounter_and_claim_messages_format_values():
    clear_cache()
    assert text("encounter.cooldown", ready_at=123) == "Your next encounter is ready <t:123:R>."
    assert text("claim.success.description", name="Mossling") == "You claimed **Mossling**!"
    assert text("setup.wild_zone", channel_name="wild-zone") == (
        "Create **#wild-zone** in this server, then select it as a spawn channel."
    )
