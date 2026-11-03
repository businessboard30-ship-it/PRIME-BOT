# path: discord_bot/cogs/clone_transfer.py

"""/admin clonedata transfer | status  (owner only, DISCORD_CLONE_ADMIN_IDS). Mounted under /admin: one shared slash slot.
Rules and the safety model live in modules/clone_transfer.py."""

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import DISCORD_CLONE_ADMIN_IDS
from database import db, get_pool
from discord_bot.cogs._admin_mount import mount_admin_command
from modules import clone_transfer as ct

logger = logging.getLogger(__name__)
MAX_SERVERS_SHOWN = 8


def _owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id in DISCORD_CLONE_ADMIN_IDS


def _fmt_state(state: str, old: int) -> str:
    return {ct.LIVE: f"\U0001F7E2 clone #{old} is ONLINE", ct.DOWN: f"\U0001F534 clone #{old} is DOWN (no recent heartbeat)",
            ct.INACTIVE: f"\u26AB clone #{old} is switched off (inactive)", ct.REMOVED: f"\u26AB clone #{old} no longer exists"}[state]


class _ConfirmView(discord.ui.View):
    def __init__(self, cog, owner_id: int, old: int, new: int, tables, guilds):
        super().__init__(timeout=180)
        self.cog, self.owner_id, self.old, self.new, self.tables, self.guilds = cog, owner_id, old, new, tables, guilds

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran this can confirm.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm and move the data", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="\u23F3 Moving data...", embed=None, view=self)
        self.stop()
        # Re-check the blocker at the moment of confirming: the source may have come back online while the owner was reading.
        state = await ct.source_state(self.old)
        if state == ct.LIVE:
            await interaction.edit_original_response(content=f"\u26D4 Cancelled: clone #{self.old} came back online. Nothing was moved.")
            return
        await ct.authorize(self.old, self.new, interaction.user.id)
        results = {}
        pool = await get_pool()
        for gid in self.guilds:
            try:
                async with pool.acquire() as conn:
                    res = await ct.transfer_guild(conn, self.tables, gid, self.old, self.new)
                    await ct.log_run(conn, gid, self.old, self.new, "moved" if not res["failed"] else "partial", res,
                                     "confirmed by owner", interaction.user.id)
                results[gid] = res
            except Exception:
                logger.exception("[clone-transfer] guild %s failed", gid)
                results[gid] = {"moved": {}, "skipped": {}, "failed": {"(whole server)": "error"}}
        moved, skipped, failed, tables = ct.summarize(results)
        lines = [f"\u2705 **Done.** Clone #{self.old} \u2192 #{self.new}: **{moved}** rows moved across **{len(results)}** servers"
                 f"{f', {skipped} skipped (the new clone already had them)' if skipped else ''}"
                 f"{f', {failed} table errors (see /admin clonedata status)' if failed else ''}.",
                 f"This pair is now approved: when #{self.new} newly joins a server that still has #{self.old} data, it moves automatically."]
        todo = ct.panel_checklist(tables)
        if todo:
            lines += ["", "**Do next (buttons belong to the bot that posted them):**"] + [f"\u2022 {t}" for t in todo]
        await interaction.edit_original_response(content="\n".join(lines)[:1990])

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was moved.", embed=None, view=None)


class CloneTransferCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        mount_admin_command(self.bot, self.transfer, name="transfer", subgroup="clonedata",
                            description="[Owner] Move a retired clone's server data to its replacement clone")
        mount_admin_command(self.bot, self.status, name="status", subgroup="clonedata",
                            description="[Owner] Approved transfers, servers waiting, recent runs")

    @app_commands.describe(source_clone_id="The OLD clone being shut down", target_clone_id="The NEW clone that replaces it")
    async def transfer(self, interaction: discord.Interaction, source_clone_id: int, target_clone_id: int):
        if not _owner(interaction):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        state = await ct.source_state(source_clone_id)
        ok, why = ct.check_pair(source_clone_id, target_clone_id, state, await db.get_discord_clone(target_clone_id))
        if not ok:
            await interaction.followup.send(f"\u26D4 **Blocked.** {why}", ephemeral=True)
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            tables, skipped = await ct.discover_tables(conn)
            guilds = await ct.servers_to_move(conn, tables, source_clone_id, target_clone_id)
            plans = {g: await ct.plan_guild(conn, tables, g, source_clone_id) for g in guilds}
        total = sum(sum(p.values()) for p in plans.values())
        if not guilds:
            await interaction.followup.send(
                f"{_fmt_state(state, source_clone_id)}.\nNothing to move: no server where clone #{target_clone_id} is present still has "
                f"clone #{source_clone_id} data. (Servers #{target_clone_id} joins later are handled once you approve below.)", ephemeral=True,
                view=_ApproveOnlyView(self, interaction.user.id, source_clone_id, target_clone_id))
            return
        by_table = {}
        for p in plans.values():
            for t, n in p.items():
                by_table[t] = by_table.get(t, 0) + n
        top = ", ".join(f"{t} {n}" for t, n in sorted(by_table.items(), key=lambda kv: -kv[1])[:10])
        denied = {}
        for t, why in skipped.items():
            denied.setdefault(why, []).append(t)
        embed = discord.Embed(title=f"Move clone #{source_clone_id} \u2192 #{target_clone_id}", color=discord.Color.orange(),
                              description=f"{_fmt_state(state, source_clone_id)}.\n**{len(guilds)} servers**, **{total} rows** in "
                                          f"**{len(by_table)} tables**.")
        embed.add_field(name="Biggest tables", value=top[:1000] or "-", inline=False)
        embed.add_field(name="Never moved (by design)", value="\n".join(f"\u2022 {w}: {len(t)} tables" for w, t in denied.items())[:1000] or "-", inline=False)
        embed.add_field(name="Rules", value="If the new clone already has a row, **its row wins** and the old one is skipped. "
                                            "Confirming approves this pair for good: later servers move automatically. "
                                            "Revoke any time in `/admin clonedata status`.", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True,
                                        view=_ConfirmView(self, interaction.user.id, source_clone_id, target_clone_id, tables, guilds))

    @app_commands.describe(revoke_source="Optional: revoke an approval: old clone id", revoke_target="Optional: revoke an approval: new clone id")
    async def status(self, interaction: discord.Interaction, revoke_source: Optional[int] = None, revoke_target: Optional[int] = None):
        if not _owner(interaction):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        note = ""
        if revoke_source is not None and revoke_target is not None:
            note = ("\u2705 Approval revoked.\n" if await ct.revoke(revoke_source, revoke_target) else "No such approval.\n")
        auths = await ct.list_authorizations()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await ct._ensure(conn)
            blocked = await conn.fetch(
                "SELECT DISTINCT ON (guild_id, from_clone, to_clone) guild_id, from_clone, to_clone, note, created_at FROM clone_transfer_log "
                "WHERE status = 'blocked' ORDER BY guild_id, from_clone, to_clone, id DESC LIMIT 25")
            recent = await conn.fetch("SELECT guild_id, from_clone, to_clone, status, created_at FROM clone_transfer_log "
                                      "WHERE status <> 'blocked' ORDER BY id DESC LIMIT 8")
        parts = [note + "**Approved transfers**\n" + ("\n".join(f"\u2022 #{a['from_clone']} \u2192 #{a['to_clone']} (<@{a['confirmed_by']}>, <t:{int(a['confirmed_at'].timestamp())}:R>)" for a in auths) or "none")]
        parts.append("**Waiting for your approval** (data found under another clone, nothing moved)\n" +
                     ("\n".join(f"\u2022 server `{b['guild_id']}`: #{b['from_clone']} \u2192 #{b['to_clone']}" for b in blocked[:MAX_SERVERS_SHOWN]) or "none"))
        parts.append("**Recent runs**\n" + ("\n".join(f"\u2022 server `{r['guild_id']}` #{r['from_clone']} \u2192 #{r['to_clone']}: {r['status']}" for r in recent) or "none"))
        await interaction.followup.send("\n\n".join(parts)[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class _ApproveOnlyView(discord.ui.View):
    def __init__(self, cog, owner_id, old, new):
        super().__init__(timeout=120)
        self.owner_id, self.old, self.new = owner_id, old, new

    @discord.ui.button(label="Approve this pair for future servers", style=discord.ButtonStyle.primary)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran this can confirm.", ephemeral=True)
            return
        if await ct.source_state(self.old) == ct.LIVE:
            await interaction.response.edit_message(content=f"\u26D4 Clone #{self.old} is online. Not approved.", view=None)
            return
        await ct.authorize(self.old, self.new, interaction.user.id)
        await interaction.response.edit_message(content=f"\u2705 Approved #{self.old} \u2192 #{self.new}.", view=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(CloneTransferCog(bot))
