# path: discord_bot/cogs/ai_skills.py

"""/aiskills -- see which AI skills exist and turn them on/off for this server. Rules live in modules/ai_skills.py.
Deliberately NOT in the AI allowlist: the AI cannot enable or disable skills."""

import discord
from discord import app_commands
from discord.ext import commands

from modules import ai_skills


async def _skill_names(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [app_commands.Choice(name=s.name, value=s.name) for s in ai_skills.all_skills() if cur in s.name][:25]


class AISkillsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.guild_only()(app_commands.Group(name="aiskills", description="AI skills: ready-made multi-step helpers"))

    @group.command(name="list", description="Show the AI skills and whether they're on in this server")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ov = await ai_skills.overrides(interaction.guild_id)
        embed = discord.Embed(title="AI skills", color=discord.Color.blurple(),
                              description="Ask the AI in plain words; it picks the skill. Every step still checks your real "
                                          "permissions and asks you to Confirm.")
        for s in ai_skills.all_skills():
            on = ov.get(s.name, s.enabled_by_default)
            need = ", ".join(p.replace("_", " ") for p in s.needs_perms) or "none"
            icon = "\U0001f7e2" if on else "\u26aa"
            embed.add_field(name=f"{icon} {s.name}",
                            value=f"{s.description[:180]}\nNeeds: {need}", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _set(self, interaction: discord.Interaction, skill: str, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        if not getattr(interaction.permissions, "manage_guild", False):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        ok = await ai_skills.set_enabled(interaction.guild_id, skill, enabled, interaction.user.id)
        await interaction.followup.send(
            f"\u2705 Skill `{skill}` {'enabled' if enabled else 'disabled'}." if ok else f"No skill called `{skill}`.", ephemeral=True)

    @group.command(name="enable", description="Turn a skill on in this server")
    @app_commands.autocomplete(skill=_skill_names)
    async def enable(self, interaction: discord.Interaction, skill: str):
        await self._set(interaction, skill, True)

    @group.command(name="disable", description="Turn a skill off in this server")
    @app_commands.autocomplete(skill=_skill_names)
    async def disable(self, interaction: discord.Interaction, skill: str):
        await self._set(interaction, skill, False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AISkillsCog(bot))
