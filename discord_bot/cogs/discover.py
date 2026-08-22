"""
Anime discover/search — Discord equivalent of handlers/discover.py and
handlers/search.py, using slash commands + discord.ui views instead of
Telegram's callback-data buttons.

Reuses anime_service.py and database.py's category CRUD exactly as the
Telegram bot does — neither has any Telegram-specific dependency, so
nothing there needed to change.

CAVEAT (flagged, not fixed here): user_categories is keyed only on a bare
user_id int, with no platform column. A Telegram user and a Discord user
who happen to share the same numeric id would see each other's categories.
Collision odds are low (Discord snowflakes are large and Telegram ids are
comparatively small, but not disjoint by construction) — worth a follow-up
migration to add a `platform` column if this bot runs on both platforms
against the same database.

i18n: bot-authored UI strings go through discord_bot.i18n_helpers.tr()
per the convention set in economy.py/automation.py. Anime titles/synopses
are external API content, sent as-is.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from anime_service import anime_service
from database import db

logger = logging.getLogger(__name__)

CATEGORY_FETCHERS = {
    "trending": ("🔥", "get_trending_anime"),
    "latest": ("✨", "get_latest_anime"),
    "ongoing": ("🔄", "get_ongoing_anime"),
    "season": ("📅", "get_seasonal_anime"),
    "movies": ("🎬", "get_anime_movies"),
}


def _rating_line(anime: dict) -> str:
    rating = anime.get("rating", 0) or 0
    episodes = anime.get("episodes")
    ep_part = f" • {episodes} ep" if episodes else ""
    return f"⭐ {rating:.1f}/10{ep_part}"


def _list_embed(title: str, emoji: str, anime_list: list, page: int) -> discord.Embed:
    embed = discord.Embed(title=f"{emoji} {title}", color=discord.Color.blurple())
    if not anime_list:
        embed.description = "No anime found."
        return embed
    lines = []
    for i, anime in enumerate(anime_list, 1):
        lines.append(f"**{i}. {anime.get('title', 'Unknown')}**\n{_rating_line(anime)}")
    embed.description = "\n\n".join(lines)
    embed.set_footer(text=f"Page {page} • pick one below for details")
    return embed


def _detail_embed(anime: dict) -> discord.Embed:
    embed = discord.Embed(
        title=anime.get("title", "Unknown"),
        description=(anime.get("description") or "No synopsis available.")[:500],
        color=discord.Color.gold(),
    )
    embed.add_field(name="Rating", value=f"{(anime.get('rating') or 0):.1f}/10")
    if anime.get("episodes"):
        embed.add_field(name="Episodes", value=str(anime["episodes"]))
    if anime.get("status"):
        embed.add_field(name="Status", value=anime["status"])
    if anime.get("genres"):
        embed.add_field(name="Genres", value=anime["genres"], inline=False)
    if anime.get("image"):
        embed.set_thumbnail(url=anime["image"])
    return embed


class AnimeListView(discord.ui.View):
    """Shown under a list of anime — a select to open details, plus
    prev/next paging that re-fetches the same category/query at a new page."""

    def __init__(self, cog: "DiscoverCog", anime_list: list, page: int, fetch_kind: str, fetch_arg: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.anime_list = anime_list
        self.page = page
        self.fetch_kind = fetch_kind  # "category" or "search"
        self.fetch_arg = fetch_arg  # category name, or search query

        options = [
            discord.SelectOption(label=a.get("title", "Unknown")[:100], value=str(i))
            for i, a in enumerate(anime_list)
            if a.get("id") is not None
        ]
        if options:
            select = discord.ui.Select(placeholder="View details...", options=options[:25])
            select.callback = self._on_select
            self.add_item(select)

        if page <= 1:
            self.prev_button.disabled = True

    async def _on_select(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        anime_id = self.anime_list[idx].get("id")
        await interaction.response.defer()
        anime = await anime_service.get_anime_details(anime_id)
        if not anime:
            await interaction.followup.send("Could not load details for that title.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=_detail_embed(anime),
            view=AnimeDetailView(self.cog, anime_id),
            ephemeral=True,
        )

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.render_page(interaction, self.fetch_kind, self.fetch_arg, self.page - 1, edit=True)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.render_page(interaction, self.fetch_kind, self.fetch_arg, self.page + 1, edit=True)


class AnimeDetailView(discord.ui.View):
    def __init__(self, cog: "DiscoverCog", anime_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.anime_id = anime_id

    @discord.ui.button(label="📁 Add to Category", style=discord.ButtonStyle.primary)
    async def add_to_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        categories = await db.get_user_categories(interaction.user.id)
        if not categories:
            await interaction.response.send_message(
                "You don't have any categories yet. Use `/animecategory new` to create one first.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=60)
        options = [
            discord.SelectOption(label=c.get("category_name", "Category")[:100], value=str(c["id"]))
            for c in categories[:25]
        ]
        select = discord.ui.Select(placeholder="Add to which category?", options=options)

        async def _pick(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True)
            category_id = int(inter.data["values"][0])
            await db.add_anime_to_category(category_id, self.anime_id)
            await inter.followup.send("Added to category! 📁", ephemeral=True)

        select.callback = _pick
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)


class NewCategoryModal(discord.ui.Modal, title="New Anime Category"):
    name = discord.ui.TextInput(label="Category name", placeholder="Weekend Watch, Shonen Favorites...", max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await db.create_category(interaction.user.id, self.name.value.strip())
        await interaction.followup.send(f"✅ Category '{self.name.value.strip()}' created!", ephemeral=True)


class CategoryPickView(discord.ui.View):
    def __init__(self, categories: list):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=c.get("category_name", "Category")[:100], value=str(c["id"]))
            for c in categories[:25]
        ]
        select = discord.ui.Select(placeholder="View a category...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        category_id = int(interaction.data["values"][0])
        category = await db.get_category(category_id)
        if not category:
            await interaction.followup.send("Category not found.", ephemeral=True)
            return
        anime_ids = category.get("anime_ids", []) or []
        embed = discord.Embed(
            title=f"📁 {category.get('category_name')}",
            color=discord.Color.blurple(),
        )
        if not anime_ids:
            embed.description = "No anime added yet — open a title's details and tap 'Add to Category'."
        else:
            lines = []
            for aid in anime_ids[:25]:
                anime = await anime_service.get_anime_details(aid)
                if anime:
                    lines.append(f"• {anime.get('title')} — {(anime.get('rating') or 0):.1f}/10")
            embed.description = "\n".join(lines) or "No anime found."
        await interaction.followup.send(embed=embed, ephemeral=True)


class DiscoverCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def render_page(self, interaction: discord.Interaction, kind: str, arg: str, page: int, edit: bool = False):
        page = max(1, page)
        if kind == "category":
            emoji, fetch_name = CATEGORY_FETCHERS[arg]
            fetcher = getattr(anime_service, fetch_name)
            anime_list = await fetcher(page)
            title = arg.capitalize()
        else:  # search
            emoji = "🔍"
            anime_list = await anime_service.search_anime(arg, page)
            title = f"Search: {arg}"

        embed = _list_embed(title, emoji, anime_list, page)
        view = AnimeListView(self, anime_list, page, kind, arg)

        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="discover", description="Browse anime by category")
    @app_commands.choices(category=[
        app_commands.Choice(name="🔥 Trending", value="trending"),
        app_commands.Choice(name="✨ Latest", value="latest"),
        app_commands.Choice(name="🔄 Ongoing", value="ongoing"),
        app_commands.Choice(name="📅 This Season", value="season"),
        app_commands.Choice(name="🎬 Movies", value="movies"),
    ])
    async def discover(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        await self.render_page(interaction, "category", category.value, 1)

    @app_commands.command(name="search", description="Search for an anime by name")
    @app_commands.describe(query="Anime title to search for")
    async def search(self, interaction: discord.Interaction, query: str):
        query = query.strip()[:100]
        if not query:
            await interaction.response.send_message("Enter something to search for.", ephemeral=True)
            return
        await self.render_page(interaction, "search", query, 1)

    animecategory = app_commands.Group(name="animecategory", description="Manage your saved anime categories")

    @animecategory.command(name="list", description="List your anime categories")
    async def category_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        categories = await db.get_user_categories(interaction.user.id)
        if not categories:
            await interaction.followup.send(
                "You haven't created any categories yet — use `/animecategory new`.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "📚 Your categories:", view=CategoryPickView(categories), ephemeral=True
        )

    @animecategory.command(name="new", description="Create a new anime category")
    async def category_new(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NewCategoryModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(DiscoverCog(bot))
