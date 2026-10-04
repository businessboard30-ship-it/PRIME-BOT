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
    # select + Encounter + Daily + 3 category actions + Server setup + Home
    assert component_count(view) == 8
    assert component_count(view) <= 25
    # discord.py auto-places items with no explicit row, so check the payload Discord will receive
    action_rows = view.to_components()
    assert len(action_rows) <= 5
    assert all(len(row["components"]) <= 5 for row in action_rows)
    assert all(len(child.custom_id) <= 100 for child in view.children)
    assert all(child.style in {ButtonStyle.primary, ButtonStyle.secondary, ButtonStyle.success} for child in view.children if hasattr(child, "style"))


def test_embed_renders_each_category():
    for category in CATEGORIES:
        embed = build_hub_embed(category.key)
        assert embed.title == "Catch hub"
        assert embed.fields[0].name == category.label
        assert category.description in embed.fields[0].value


def test_catch_dynamic_items_cover_restartable_buttons():
    assert DYNAMIC_ITEMS[0] is CatchHubDynamicButton
    template = CatchHubDynamicButton.__discord_ui_compiled_template__
    assert template.pattern == r"catch:hub:(?!category$)(?:(?P<category>[a-z-]+):)?(?P<action>[a-z-]+)"
    for custom_id in ("catch:hub:home", "catch:hub:play:encounter", "catch:hub:collect:box"):
        assert template.fullmatch(custom_id)
    assert len("catch:hub:encounter") <= 100


def test_hub_setup_button_and_category_select_survive_restart(monkeypatch):
    import asyncio
    import discord
    import re
    from types import SimpleNamespace
    import discord_bot.cogs.catch as catch

    assert catch.CatchHubDynamicSelect in catch.DYNAMIC_ITEMS
    assert re.fullmatch(catch.CatchHubDynamicSelect.__discord_ui_compiled_template__.pattern, "catch:hub:category")
    # the button template must NOT claim the select's custom_id (discord.py dispatches all matches)
    assert not catch.CatchHubDynamicButton.__discord_ui_compiled_template__.fullmatch("catch:hub:category")
    assert catch.CatchHubDynamicButton.__discord_ui_compiled_template__.fullmatch("catch:hub:setup")

    seen = []

    async def fake_setup(self, interaction):
        seen.append("setup")

    monkeypatch.setattr(catch.CatchHubView, "_setup", fake_setup)
    button = catch.CatchHubDynamicButton(discord.ui.Button(custom_id="catch:hub:setup"), action="setup")
    asyncio.run(button.callback(SimpleNamespace()))
    assert seen == ["setup"]

    edits = []

    async def edit_message(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(data={"values": ["collect"]}, response=SimpleNamespace(edit_message=edit_message))
    asyncio.run(catch.CatchHubDynamicSelect().callback(interaction))
    assert len(edits) == 1 and edits[0]["view"].category.key == "collect"
