import json
from pathlib import Path

import discord
import pytest

from modules import catch_theme


def test_theme_roles_map_to_discord_styles():
    raw = json.loads((Path(__file__).parents[2] / "data/catch/theme.json").read_text())
    styles = {discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success, discord.ButtonStyle.danger, discord.ButtonStyle.link}
    assert set(catch_theme.button_roles()) == set(raw["button_roles"])
    assert all(catch_theme.button_style(role) in styles for role in catch_theme.button_roles())
    for group in ("rarities", "elements", "states"):
        assert all(catch_theme.parse_hex(value) >= 0 for value in raw[group].values())


def test_theme_override_validation_and_precedence():
    with pytest.raises(ValueError):
        catch_theme.validate_override("state:success", "#12")
    with pytest.raises(KeyError):
        catch_theme.validate_override("state:not-real", "#123456")
    catch_theme.apply_overrides({"state:success": "#123456", "state:bad": "#ffffff"})
    assert catch_theme.state_color("success").value == 0x123456
    catch_theme.apply_overrides({})
