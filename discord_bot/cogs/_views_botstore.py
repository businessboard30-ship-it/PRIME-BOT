"""
Rating flow for /botstore rate — replaces typing a listing_id (copied out
of /botstore browse) and a stars integer with two Select Menus. Triggered
from a "⭐ Rate" button attached to the browse view.
"""

import discord

from modules.botstore_adapter import add_rating, get_avg_rating, get_clicks


def _listing_embed(l: dict, position: int = 1, total: int = 1, avg_rating=None, clicks: int = None) -> discord.Embed:
    embed = discord.Embed(
        title=l["title"],
        description=(l.get("description") or "No description provided.")[:300],
        color=discord.Color.blurple(),
        url=l.get("link") if str(l.get("link", "")).startswith("https://") else None,
    )
    embed.add_field(name="📂 Category", value=l.get("category") or "Other", inline=True)
    embed.add_field(name="⭐ Rating", value=f"{avg_rating}★" if avg_rating else "No ratings yet", inline=True)
    embed.add_field(name="🖱️ Clicks", value=str(clicks if clicks is not None else l.get("clicks", 0)), inline=True)
    embed.set_footer(text=f"Listing #{l['id']} · {position} of {total}")
    return embed


class BrowseNavView(discord.ui.View):
    """Paginates listings one-per-embed instead of cramming every result
    into a single message as stacked fields."""

    def __init__(self, listings: list, invoker_id: int):
        super().__init__(timeout=180)
        self.listings = listings
        self.invoker_id = invoker_id
        self.index = 0
        self.open_btn = discord.ui.Button(label="🔗 Open", style=discord.ButtonStyle.link, row=0, url="https://discord.com")
        self.add_item(self.open_btn)
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who opened this can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def _sync_buttons(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.listings) - 1
        link = self.current.get("link") or ""
        self.open_btn.disabled = not str(link).startswith("https://")
        self.open_btn.url = link if str(link).startswith("https://") else "https://discord.com"

    @property
    def current(self) -> dict:
        return self.listings[self.index]

    async def current_embed(self) -> discord.Embed:
        avg = await get_avg_rating(self.current["id"])
        clicks = await get_clicks(self.current["id"])
        return _listing_embed(self.current, self.index + 1, len(self.listings), avg, clicks)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=await self.current_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.listings) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=await self.current_embed(), view=self)

    @discord.ui.button(label="⭐ Rate this", style=discord.ButtonStyle.primary, row=1)
    async def rate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = StarsOnlyView(self.current, interaction.user.id)
        await interaction.response.send_message(f"Rate **{self.current['title']}**:", view=view, ephemeral=True)


class StarsOnlyView(discord.ui.View):
    """Single-listing star picker — used from the Rate button on a listing
    the user is already looking at, so there's no need to pick it again."""

    def __init__(self, listing: dict, invoker_id: int):
        super().__init__(timeout=120)
        self.listing = listing
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who opened this can use it.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="Pick a star rating",
        options=[discord.SelectOption(label=f"{'⭐' * n} ({n})", value=str(n)) for n in range(1, 6)],
    )
    async def stars(self, interaction: discord.Interaction, select: discord.ui.Select):
        stars = int(select.values[0])
        await add_rating(self.listing["id"], interaction.user.id, stars)
        avg = await get_avg_rating(self.listing["id"])
        embed = discord.Embed(
            description=f"✅ Rated **{self.listing['title']}** {stars}★. Average is now {avg}★.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)


class StarsSelect(discord.ui.Select):
    def __init__(self, parent_view: "RateFlowView"):
        options = [discord.SelectOption(label=f"{'⭐' * n} ({n})", value=str(n)) for n in range(1, 6)]
        super().__init__(placeholder="Pick a star rating", options=options, row=1, disabled=True)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        listing = self.parent_view.selected_listing
        stars = int(self.values[0])
        await add_rating(listing["id"], interaction.user.id, stars)
        avg = await get_avg_rating(listing["id"])
        embed = discord.Embed(
            description=f"✅ Rated **{listing['title']}** {stars}★. Average is now {avg}★.",
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(content=None, embed=embed, view=None)


class ListingSelect(discord.ui.Select):
    def __init__(self, parent_view: "RateFlowView", listings: list):
        options = [
            discord.SelectOption(label=l["title"][:100], description=(l.get("description") or "")[:100], value=l["id"])
            for l in listings[:25]
        ]
        super().__init__(placeholder="Pick a bot to rate", options=options, row=0)
        self.parent_view = parent_view
        self.listings_by_id = {l["id"]: l for l in listings}

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.selected_listing = self.listings_by_id[self.values[0]]
        self.parent_view.stars_select.disabled = False
        self.parent_view.stars_select.placeholder = f"Rate {self.parent_view.selected_listing['title'][:60]}"
        await interaction.response.edit_message(view=self.parent_view)


class RateFlowView(discord.ui.View):
    def __init__(self, listings: list, invoker_id: int):
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.selected_listing = None
        self.add_item(ListingSelect(self, listings))
        self.stars_select = StarsSelect(self)
        self.add_item(self.stars_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who opened this can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
