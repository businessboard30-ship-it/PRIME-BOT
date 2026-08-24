"""
Anime Submission Workflow — Discord equivalent of handlers/submit.py (+ the
admin review half that lived in handlers/admin_tools.py), using
database.py's existing add_submission/get_pending_submissions/
approve_submission/reject_submission — none of that is Telegram-specific,
so it's reused as-is (same pattern as every other port in this project).

Telegram's version is a 4-step conversation (name -> episodes -> genres ->
synopsis) gated by a click-through disclaimer. Collapsed here into one
slash command with all fields as options — Discord's option UI already
shows each field with its label before the user submits, so the
disclaimer's actual content (rights/accuracy/content-warning) is folded
into the command description + a footer note rather than a separate
click-through step.

The Telegram flow also built (but never sent) a "submission_type" choice
(anime vs movie) — it was never written to the submissions table (no
column for it), so that's not carried over; if you want it tracked, that
needs a schema change, not a Discord-specific fix.

Admin notification: Telegram DMed ADMIN_ID on every new submission. No
live-push equivalent here (same reasoning as ads_marketplace.py's /ad
pending and automation.py's /announce) — admins pull pending submissions
with /submissions pending instead of getting pushed one.
"""

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from utils.rate_limiter import rate_limiter
from config import DISCORD_CLONE_ADMIN_IDS
from discord_bot.cogs._views_shared import NavCardView, refresh_button
from discord_bot.cogs._views_submissions import SubmissionReviewView, render_submission_embed

DISCLAIMER_FOOTER = (
    "By submitting: you confirm you have the right to submit this, the info is accurate, "
    "and you're not violating copyright. Content may include violence, suggestive themes, "
    "or strong language."
)


def _is_submissions_admin(user_id: int) -> bool:
    return user_id in DISCORD_CLONE_ADMIN_IDS


class SubmissionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="submit", description="Submit an anime/movie for the catalog")
    @app_commands.describe(
        title="Title of the anime/movie", episodes="Episode count (leave blank if a movie or unknown)",
        genres="Comma-separated, e.g. Action, Adventure, Drama", synopsis="Brief description",
        image_url="Optional cover image URL",
    )
    async def submit(
        self, interaction: discord.Interaction, title: str, genres: str, synopsis: str,
        episodes: int = None, image_url: str = "",
    ):
        await interaction.response.defer()
        if not await rate_limiter.check_submission_limit(interaction.user.id):
            await interaction.followup.send(
                "You've reached your submission limit for today. Try again tomorrow.", ephemeral=True
            )
            return
        submission_id = await db.add_submission(
            interaction.user.id, title.strip()[:255], episodes, genres.strip(), synopsis.strip(), image_url.strip()
        )
        line = f"**{title.strip()}** · id `{submission_id}`\n-# {DISCLAIMER_FOOTER}"
        card = discord.ui.LayoutView()
        text = discord.ui.TextDisplay(f"### ✅ Submitted for review\n{line}")
        if image_url.strip():
            section = discord.ui.Section(text, accessory=discord.ui.Thumbnail(image_url.strip()))
            card.add_item(discord.ui.Container(section, accent_colour=discord.Color.green()))
        else:
            card.add_item(discord.ui.Container(text, accent_colour=discord.Color.green()))
        await interaction.followup.send(view=card, ephemeral=True)

    submissions = app_commands.Group(name="submissions", description="[Admin] Review submitted anime/movies")

    @submissions.command(name="pending", description="[Admin] List submissions awaiting review")
    async def submissions_pending(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_submissions_admin(interaction.user.id):
            await interaction.followup.send("You're not authorized to review submissions.", ephemeral=True)
            return
        pending = await db.get_pending_submissions()
        if not pending:
            await interaction.followup.send("No submissions pending review.", ephemeral=True)
            return
        lines = [f"**#{s['submission_id']} — {s['anime_name']}**\n{s.get('genres') or 'no genres'}" for s in pending[:15]]
        buttons = [refresh_button(self, "submissions_pending")]
        card = NavCardView("Pending submissions", lines, discord.Color.orange(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    @submissions.command(name="review", description="[Admin] Review pending submissions from a dropdown")
    async def submissions_review(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _is_submissions_admin(interaction.user.id):
            await interaction.followup.send("You're not authorized to review submissions.", ephemeral=True)
            return
        pending = await db.get_pending_submissions()
        if not pending:
            await interaction.followup.send("No submissions pending review.", ephemeral=True)
            return
        view = SubmissionReviewView(pending, interaction.user.id)
        embed = render_submission_embed(pending[0], 1, len(pending))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @submissions.command(name="approve", description="[Admin] Approve a pending submission")
    @app_commands.describe(submission_id="The submission's id")
    async def submissions_approve(self, interaction: discord.Interaction, submission_id: int):
        await interaction.response.defer(ephemeral=True)
        if not _is_submissions_admin(interaction.user.id):
            await interaction.followup.send("You're not authorized to review submissions.", ephemeral=True)
            return
        await db.approve_submission(submission_id)
        embed = discord.Embed(description=f"✅ Approved submission #{submission_id}.", color=discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @submissions.command(name="reject", description="[Admin] Reject a pending submission")
    @app_commands.describe(submission_id="The submission's id", reason="Why it's being rejected")
    async def submissions_reject(self, interaction: discord.Interaction, submission_id: int, reason: str):
        await interaction.response.defer(ephemeral=True)
        if not _is_submissions_admin(interaction.user.id):
            await interaction.followup.send("You're not authorized to review submissions.", ephemeral=True)
            return
        await db.reject_submission(submission_id, reason.strip()[:200])
        embed = discord.Embed(
            title=f"❌ Rejected submission #{submission_id}",
            description=f"Reason: {reason.strip()[:200]}",
            color=discord.Color.red(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SubmissionsCog(bot))
