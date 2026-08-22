"""
Slash commands for the premium paywall — now supporting any number of
independently-priced premium groups per guild (per clone), plus the
on_member_join gate. Discord equivalent of handlers/premium_group_handler.py
and the payment gate inside handlers/moderation.py's handle_join_request().

i18n: ephemeral command responses go through discord_bot.i18n_helpers.tr()
(see economy.py's module docstring for how the translation cache works).
Group *names* (admin-authored, e.g. "VIP", "Founders") are sent as-is like
any other admin-authored freeform text elsewhere in this expansion.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord_bot.cogs._dm_support import GuildOnlyCog

from database import db
from discord_bot.i18n_helpers import get_lang, tr
from discord_bot.views import (
    PremiumPayView,
    grant_premium_role,
    PAYMENT_TYPE,
)
from discord_bot.cogs._views_shared import ActionButton, NavCardView, refresh_button
from discord_bot.cogs._views_premium_admin import PremiumAdminPanelView

logger = logging.getLogger(__name__)


def _is_admin(interaction: discord.Interaction) -> bool:
    # interaction.permissions (not interaction.user.guild_permissions) — the
    # latter needs a real discord.Member, which Discord doesn't give us when
    # this app is invoked via a user-install context, even inside a guild
    # channel. interaction.permissions is always populated correctly there.
    if interaction.guild is None:
        return False
    return bool(interaction.permissions.manage_guild)


async def _deny_admin(interaction: discord.Interaction, lang: str):
    msg = await tr("You need Manage Server permission to do that.", lang)
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


def _clone_id_of(interaction: discord.Interaction):
    """None = main bot. See discord_bot/bot.py — set once at process
    startup on the client itself, threaded through here so every premium
    group this cog creates/lists/pays into is scoped to the right bot
    instance (a clone must never see or sell into another bot's groups,
    even for the same guild_id)."""
    return getattr(interaction.client, "clone_id", None)


class PremiumCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /pay ──────────────────────────────────────────────────────────────
    @app_commands.command(name="pay", description="Pay to unlock a Premium role in this server")
    @app_commands.guild_only()
    async def pay(self, interaction: discord.Interaction):
        """Manual trigger for the same flow as the persistent button —
        useful for users who don't have the original announcement message
        handy."""
        lang = await get_lang(interaction)
        if interaction.guild_id is None:
            msg = await tr("This only works inside a server.", lang)
            await interaction.response.send_message(msg, ephemeral=True)
            return
        view = PremiumPayView()
        msg = await tr("Tap below to start (or check) your Premium payment:", lang)
        await interaction.response.send_message(msg, view=view, ephemeral=True)

    # ── /status ───────────────────────────────────────────────────────────
    @app_commands.command(name="status", description="Check your premium payment status in this server")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if interaction.guild_id is None:
            msg = await tr("This only works inside a server.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        groups = await db.list_premium_groups(interaction.guild_id, clone_id=_clone_id_of(interaction), active_only=True)
        if not groups:
            msg = await tr("This server has no premium groups set up yet.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        lines = []
        for g in groups:
            paid = await db.has_paid(interaction.user.id, PAYMENT_TYPE, chat_id=interaction.guild_id, group_id=g["group_id"])
            mark = "✅" if paid else "❌"
            lines.append(f"{mark} **{g['name']}** — GHS {float(g['fee_ghs']):g}")
        buttons = [
            refresh_button(self, "status"),
            ActionButton("Pay", discord.ButtonStyle.success, self, "pay", emoji="💳"),
        ]
        card = NavCardView("Premium status", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    # ── /createpremium (admin-only) ─────────────────────────────────────────
    @app_commands.command(name="createpremium", description="[Admin] Create a new premium group in this server")
    @app_commands.guild_only()
    @app_commands.describe(name="Display name for this group (e.g. 'VIP', 'Founders')",
                            price="Price in GHS, e.g. 20", role="Role to grant on payment")
    async def createpremium(self, interaction: discord.Interaction, name: str, price: float, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _is_admin(interaction):
            await _deny_admin(interaction, lang)
            return
        if price <= 0:
            msg = await tr("Price must be greater than 0.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        group_id = await db.create_premium_group(
            guild_id=interaction.guild_id, name=name, role_id=role.id, fee_ghs=price,
            created_by=interaction.user.id, clone_id=_clone_id_of(interaction),
        )
        msg = await tr(
            "✅ Created premium group **{name}** (id `{group_id}`) — GHS {price:g} for {role}.", lang,
            name=name, group_id=group_id, price=price, role=role.mention
        )
        await interaction.followup.send(msg, ephemeral=True)

    # ── /listpremium ──────────────────────────────────────────────────────
    @app_commands.command(name="listpremium", description="List this server's premium groups")
    @app_commands.guild_only()
    async def listpremium(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        groups = await db.list_premium_groups(interaction.guild_id, clone_id=_clone_id_of(interaction), active_only=False)
        if not groups:
            msg = await tr("No premium groups have been created in this server yet.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        active_word = await tr("active", lang)
        disabled_word = await tr("disabled", lang)
        lines = []
        for g in groups:
            role = interaction.guild.get_role(g["role_id"]) if interaction.guild else None
            role_label = role.mention if role else await tr("(role {role_id} not found)", lang, role_id=g["role_id"])
            status = active_word if g["active"] else disabled_word
            lines.append(f"**#{g['group_id']} — {g['name']}**\nGHS {float(g['fee_ghs']):g} · {role_label} · {status}")
        buttons = [refresh_button(self, "listpremium")]
        card = NavCardView("Premium groups", lines, discord.Color.blurple(), buttons)
        await interaction.followup.send(view=card, ephemeral=True)

    # ── /premiumadmin (admin-only) — dropdown panel, no group_id typing ──
    @app_commands.command(name="premiumadmin", description="[Admin] Manage premium groups from a dropdown")
    @app_commands.guild_only()
    async def premiumadmin(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _is_admin(interaction):
            await _deny_admin(interaction, lang)
            return
        groups = await db.list_premium_groups(interaction.guild_id, clone_id=_clone_id_of(interaction), active_only=False)
        if not groups:
            msg = await tr("No premium groups have been created in this server yet.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        view = PremiumAdminPanelView(groups, interaction.user.id)
        await interaction.followup.send("Pick a group below to edit or enable/disable it.", view=view, ephemeral=True)

    # ── /editpremium (admin-only) ───────────────────────────────────────────
    @app_commands.command(name="editpremium", description="[Admin] Edit one of this server's premium groups")
    @app_commands.guild_only()
    @app_commands.describe(group_id="The id shown by /listpremium", name="New display name (optional)",
                            price="New price in GHS (optional)", role="New role to grant (optional)")
    async def editpremium(self, interaction: discord.Interaction, group_id: int, name: str = None,
                           price: float = None, role: discord.Role = None):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _is_admin(interaction):
            await _deny_admin(interaction, lang)
            return
        group = await db.get_premium_group(group_id)
        if not group or group["guild_id"] != interaction.guild_id or group["clone_id"] != _clone_id_of(interaction):
            msg = await tr("No premium group with that id in this server.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        if price is not None and price <= 0:
            msg = await tr("Price must be greater than 0.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        await db.update_premium_group(group_id, name=name, role_id=(role.id if role else None), fee_ghs=price)
        msg = await tr("✅ Updated premium group `#{group_id}`.", lang, group_id=group_id)
        await interaction.followup.send(msg, ephemeral=True)

    # ── /togglepremium (admin-only) ─────────────────────────────────────────
    @app_commands.command(name="togglepremium", description="[Admin] Enable or disable one of this server's premium groups")
    @app_commands.guild_only()
    @app_commands.describe(group_id="The id shown by /listpremium", active="True to enable, false to disable (hides it from /pay)")
    async def togglepremium(self, interaction: discord.Interaction, group_id: int, active: bool):
        await interaction.response.defer(ephemeral=True)
        lang = await get_lang(interaction)
        if not _is_admin(interaction):
            await _deny_admin(interaction, lang)
            return
        group = await db.get_premium_group(group_id)
        if not group or group["guild_id"] != interaction.guild_id or group["clone_id"] != _clone_id_of(interaction):
            msg = await tr("No premium group with that id in this server.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return
        await db.update_premium_group(group_id, active=active)
        state = await tr("enabled", lang) if active else await tr("disabled", lang)
        msg = await tr("✅ Premium group `#{group_id}` is now {state}.", lang, group_id=group_id, state=state)
        await interaction.followup.send(msg, ephemeral=True)

    # ── /verify (admin-only) — manual override, bypasses payment ──────────
    @app_commands.command(name="verify", description="[Admin] Manually mark a user as paid for a premium group (bypasses payment)")
    @app_commands.guild_only()
    @app_commands.describe(member="Member to grant premium to", group_id="The id shown by /listpremium",
                            reason="Why you're bypassing payment (required, for the audit log)")
    async def verify(self, interaction: discord.Interaction, member: discord.Member, group_id: int, reason: str):
        lang = await get_lang(interaction)
        if not _is_admin(interaction):
            await _deny_admin(interaction, lang)
            return

        # Several DB round-trips + a role grant happen below — defer
        # immediately so this doesn't risk missing Discord's 3-second
        # interaction-ack window and failing with "This interaction failed".
        await interaction.response.defer(ephemeral=True, thinking=True)

        group = await db.get_premium_group(group_id)
        if not group or group["guild_id"] != interaction.guild_id or group["clone_id"] != _clone_id_of(interaction):
            msg = await tr("No premium group with that id in this server.", lang)
            await interaction.followup.send(msg, ephemeral=True)
            return

        guild_id = interaction.guild_id
        # Idempotent: if there's no completed payment on file, log one
        # manually so has_paid() reflects reality for every future check
        # (webhook re-delivery, /status, on_member_join, etc.) without a
        # special-cased "manually verified" flag scattered through the code.
        already = await db.has_paid(member.id, PAYMENT_TYPE, chat_id=guild_id, group_id=group_id)
        if not already:
            fake_reference = f"MANUAL_{guild_id}_{group_id}_{member.id}_{interaction.id}"
            await db.log_payment(
                member.id, float(group["fee_ghs"]), fake_reference, status="pending",
                payment_type=PAYMENT_TYPE, chat_id=guild_id, group_id=group_id,
            )
            await db.mark_payment_paid(fake_reference)

        # Audit trail — required per spec since this bypasses payment entirely.
        await db.log_manual_verify(
            admin_id=interaction.user.id, user_id=member.id,
            payment_type=PAYMENT_TYPE, chat_id=guild_id, reason=f"group={group['name']} ({group_id}): {reason}",
        )

        ok = await grant_premium_role(member, group["role_id"], reason=f"manual /verify by {interaction.user.id}: {reason}")
        if ok:
            msg = await tr(
                "✅ {member} manually verified for **{group_name}** and role granted.", lang,
                member=member.mention, group_name=group["name"]
            )
        else:
            msg = await tr(
                "Marked {member} as paid for **{group_name}**, but couldn't grant the role — check the role still exists.",
                lang, member=member.mention, group_name=group["name"]
            )
        await interaction.followup.send(msg, ephemeral=True)

    # ── on_member_join gate ────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Equivalent of handlers/moderation.py's handle_join_request() paid
        gate: grant every premium group this user already paid for before
        joining (e.g. paid, closed Discord, joined later) instead of waiting
        for them to click Verify again. Loops over every group since a user
        can hold payments for more than one independent group at once.

        No interaction here (this is a gateway event, not a slash command),
        so there's nothing to translate — grant_premium_role's audit-log
        `reason` stays English same as every other mod-log entry."""
        clone_id = getattr(self.bot, "clone_id", None)
        groups = await db.list_premium_groups(member.guild.id, clone_id=clone_id, active_only=True)
        for group in groups:
            paid = await db.has_paid(member.id, PAYMENT_TYPE, chat_id=member.guild.id, group_id=group["group_id"])
            if paid:
                await grant_premium_role(member, group["role_id"], reason=f"premium_group_join payment on file at join time: {group['name']}")


async def setup(bot: commands.Bot):
    await bot.add_cog(PremiumCog(bot))
