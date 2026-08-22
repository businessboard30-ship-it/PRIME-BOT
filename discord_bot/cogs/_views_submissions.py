"""
Review panel for /submissions — replaces typing a submission_id (copied
out of /submissions pending) into separate approve/reject commands with a
Select Menu + Approve/Reject buttons. Reject opens a Modal for the reason
instead of a slash-command string arg.
"""

import discord

from database import db


def render_submission(s: dict) -> str:
    """Kept for any caller still expecting plain text (e.g. logs)."""
    return (
        f"**#{s['submission_id']} {s['anime_name']}**\n"
        f"Episodes: {s.get('episodes') or '—'}\n"
        f"Genres: {s.get('genres') or 'no genres'}\n"
        f"Synopsis: {(s.get('synopsis') or '')[:300]}"
    )


def render_submission_embed(s: dict, position: int = 1, total: int = 1) -> discord.Embed:
    embed = discord.Embed(
        title=s["anime_name"],
        description=(s.get("synopsis") or "No synopsis provided.")[:300],
        color=discord.Color.blurple(),
    )
    if s.get("image_url"):
        embed.set_thumbnail(url=s["image_url"])
    embed.add_field(name="📺 Episodes", value=str(s.get("episodes") or "—"), inline=True)
    embed.add_field(name="🏷️ Genres", value=s.get("genres") or "no genres", inline=True)
    embed.add_field(name="👤 Submitted by", value=f"<@{s['user_id']}>" if s.get("user_id") else "—", inline=True)
    embed.set_footer(text=f"Submission #{s['submission_id']} · {position} of {total} pending")
    return embed


class RejectReasonModal(discord.ui.Modal, title="Reject submission"):
    def __init__(self, parent_view: "SubmissionReviewView"):
        super().__init__()
        self.parent_view = parent_view
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=200, required=True)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        submission_id = self.parent_view.current["submission_id"]
        await db.reject_submission(submission_id, str(self.reason.value).strip()[:200])
        self.parent_view.remove_current()
        embed = self.parent_view.render_next_embed(status_note=f"Rejected submission #{submission_id}", status_color=discord.Color.red())
        await interaction.response.edit_message(
            content=None, embed=embed, view=self.parent_view if self.parent_view.pending else None,
        )


class PendingSelect(discord.ui.Select):
    def __init__(self, parent_view: "SubmissionReviewView"):
        options = [
            discord.SelectOption(label=f"#{s['submission_id']} {s['anime_name']}"[:100], value=str(s["submission_id"]))
            for s in parent_view.pending[:25]
        ]
        super().__init__(placeholder="Pick a submission to review", options=options, row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        submission_id = int(self.values[0])
        self.parent_view.current = next(s for s in self.parent_view.pending if s["submission_id"] == submission_id)
        embed = self.parent_view.render_current_embed()
        await interaction.response.edit_message(content=None, embed=embed, view=self.parent_view)


class SubmissionReviewView(discord.ui.View):
    def __init__(self, pending: list, invoker_id: int):
        super().__init__(timeout=300)
        self.pending = pending
        self.invoker_id = invoker_id
        self.current = pending[0] if pending else None
        self.select = PendingSelect(self)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran this command can use it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

    def remove_current(self):
        self.pending = [s for s in self.pending if s["submission_id"] != self.current["submission_id"]]
        self.current = self.pending[0] if self.pending else None
        self.remove_item(self.select)
        if self.pending:
            self.select = PendingSelect(self)
            self.add_item(self.select)

    def render_next(self) -> str:
        if not self.pending:
            return "No more submissions pending review."
        return render_submission(self.current)

    def render_current_embed(self) -> discord.Embed:
        if self.current is None:
            return discord.Embed(description="No more submissions pending review.", color=discord.Color.greyple())
        position = self.pending.index(self.current) + 1
        return render_submission_embed(self.current, position, len(self.pending))

    def render_next_embed(self, status_note: str = None, status_color: discord.Color = None) -> discord.Embed:
        if not self.pending:
            embed = discord.Embed(description="No more submissions pending review.", color=discord.Color.greyple())
        else:
            embed = self.render_current_embed()
        if status_note:
            embed.set_author(name=status_note)
            if status_color:
                embed.color = status_color
        return embed

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, row=1)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current is None:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        submission_id = self.current["submission_id"]
        await db.approve_submission(submission_id)
        self.remove_current()
        embed = self.render_next_embed(status_note=f"Approved submission #{submission_id}", status_color=discord.Color.green())
        await interaction.response.edit_message(content=None, embed=embed, view=self if self.pending else None)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, row=1)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current is None:
            await interaction.response.send_message("Nothing selected.", ephemeral=True)
            return
        await interaction.response.send_modal(RejectReasonModal(self))
