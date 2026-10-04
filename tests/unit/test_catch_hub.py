from discord import ButtonStyle

from discord_bot.cogs.catch import (
    DYNAMIC_ITEMS,
    CatchHubDynamicButton,
    CATEGORIES,
    CatchHubView,
    build_hub_embed,
    category_for,
    component_count,
)


def test_all_categories_are_reachable_and_have_three_actions():
    assert [category.key for category in CATEGORIES] == ["play", "collect", "social", "economy", "battle", "info"]
    assert all(len(category.actions) == 3 for category in CATEGORIES)


def test_unknown_category_falls_back_to_play():
    assert category_for("missing").key == "play"


def test_hub_stays_under_discord_component_limit():
    view = CatchHubView()
    assert component_count(view) == 7
    assert component_count(view) <= 25
    assert all(child.style in {ButtonStyle.primary, ButtonStyle.secondary, ButtonStyle.success} for child in view.children if hasattr(child, "style"))


def test_embed_renders_each_category():
    for category in CATEGORIES:
        embed = build_hub_embed(category.key)
        assert embed.title == "Catch hub"
        assert embed.fields[0].name == category.label
        assert category.description in embed.fields[0].value


def test_catch_dynamic_items_cover_restartable_buttons():
    assert DYNAMIC_ITEMS == (CatchHubDynamicButton,)
    assert CatchHubDynamicButton.template.pattern == r"catch:hub:(?P<action>[a-z-]+)"
    assert len("catch:hub:encounter") <= 100
