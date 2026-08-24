"""
Review panel for /archive pending — replaces typing a listing_id (copied
out of /archive pending) into /archive resolve with a Select Menu +
Approve/Deny buttons. Deny opens a Modal for the reason. Everything else
(post the card, DM the submitter) reuses ArchiveCog's own logic via a
callback passed in from the cog, so behavior stays identical to the
existing /archive resolve command.
"""

import discord


def _risk_color(risk_score) -> discord.Color:
    try:
        score = float(risk_score)
    except (TypeError, ValueError):
        return discord.Color.greyple()
    if score >= 70:
        return discord.Color.red()
    if score >= 40:
        return discord.Color.orange()
    return discord.Color.blurple()


def render_listing(r: dict) -> str:
    """Kept for any caller still expecting plain text (e.g. logs)."""
    return f"`#{r['id']}` **{r['bot_name']}** — risk {r['risk_score']} — <@{r['submitter_id']}>"


def render_listing_embed(r: dict, position: int = 1, total: int = 1) -> discord.Embed:
    embed = discord.Embed(
        title=r["bot_name"],
        description=(r.get("description") or "No description provided.")[:300],
        color=_risk_color(r.get("risk_score")),
    )
    if r.get("bot_icon_url"):
        embed.set_thumbnail(url=r["bot_icon_url"])
    embed.add_field(name="Risk score", value=str(r.get("risk_score", "—")), inline=True)
    embed.add_field(name="Category", value=r.get("category") or "Other", inline=True)
    embed.add_field(name="Developer", value=f"<@{r['submitter_id']}>", inline=True)
    embed.set_footer(text=f"Listing #{r['id']} · {position} of {total} pending")
    return embed


class ResolveReasonModal(discord.ui.Modal, title="Deny listing"):
    def __init__(self, parent_view: "ArchiveReviewView"):
        super().__init__()
        self.parent_view = parent_view
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        listing_id = self.parent_view.current["id"]
        await self.parent_view.resolve_callback(interaction, listing_id, False, str(self.reason.value).strip())
        self.parent_view.remove_current()
        embed = self.parent_view.render_next_embed(status_note=f"Denied listing #{listing_id}", status_color=discord.Color.red())
        await interaction.response.edit_message(
            content=None, embed=embed, view=self.parent_view if self.parent_view.pending else None
        )


class PendingListingSelect(discord.ui.Select):
    def __init__(self, parent_view: "ArchiveReviewView"):
        options = [
            discord.SelectOption(
                label=f"#{r['id']} {r['bot_name']}"[:100],
                description=f"risk {r['risk_score']}",
                value=str(r["id"]),
                emoji="🚩",
            )
            for r in parent_view.pending[:25]
        ]
        super().__init__(placeholder="Pick a flagged listing to resolve", options=options, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        listing_id = int(self.values[0])
        self.parent_view.current = next(r for r in self.parent_view.pending if r["id"] == listing_id)
        embed = self.parent_view.render_current_embed()
        await interaction.response.edit_message(content=None, embed=embed, view=self.parent_view)


class ArchiveReviewView(discord.ui.View):
    def __init__(self, pending: list, invoker_id: int, resolve_callback):
        super().__init__(timeout=300)
        self.pending = pending
        self.invoker_id = invoker_id
        self.resolve_callback = resolve_callback  # async (interaction, listing_id, approve, reason) -> None
        self.current = pending[0] if pending else None
        self.select = PendingListingSelect(self)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the archive owner who opened this can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def remove_current(self):
        self.pending = [r for r in self.pending if r["id"] != self.current["id"]]
        self.current = self.pending[0] if self.pending else None
        self.remove_item(self.select)
        if self.pending:
            self.select = PendingListingSelect(self)
            self.add_item(self.select)

    def render_next(self) -> str:
        return render_listing(self.current) if self.pending else "Nothing else pending review."

    def render_current_embed(self) -> discord.Embed:
        if self.current is None:
            return discord.Embed(description="Nothing else pending review.", color=discord.Color.greyple())
        position = self.pending.index(self.current) + 1
        return render_listing_embed(self.current, position, len(self.pending))

    def render_next_embed(self, status_note: str = None, status_color: discord.Color = None) -> discord.Embed:
        if not self.pending:
            embed = discord.Embed(description="Nothing else pending review.", color=discord.Color.greyple())
        else:
            embed = self.render_current_embed()
        if status_note:
            embed.set_author(name=status_note, icon_url=None)
            if status_color:
                embed.color = status_color
        return embed

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, row=1)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current is None:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        listing_id = self.current["id"]
        await interaction.response.defer(ephemeral=True)
        await self.resolve_callback(interaction, listing_id, True, None)
        self.remove_current()
        embed = self.render_next_embed(status_note=f"Approved listing #{listing_id}", status_color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)
        if self.pending:
            await interaction.edit_original_response(embed=self.render_current_embed(), view=self)
        else:
            await interaction.edit_original_response(content="Nothing else pending review.", embed=None, view=None)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, row=1)
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current is None:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        await interaction.response.send_modal(ResolveReasonModal(self))
