# path: discord_bot/cogs/ai_agents.py

"""/aiagents -- see the AI's specialist agents, switch them on/off for this server, and read the task history.
Rules live in modules/ai_agents.py. Deliberately NOT in the AI allowlist: the AI cannot enable agents or read this."""

import discord
from discord import app_commands
from discord.ext import commands

from modules import ai_agents


async def _agent_names(interaction: discord.Interaction, current: str):
    cur = (current or "").lower()
    return [app_commands.Choice(name=a.name, value=a.name) for a in ai_agents.all_agents() if cur in a.name][:25]


def _can_manage(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and bool(getattr(interaction.permissions, "manage_guild", False))


class AIAgentsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.guild_only()(app_commands.Group(name="aiagents", description="AI specialist agents (off by default)"))

    @group.command(name="list", description="Show the AI agents and whether they're on in this server")
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ov = await ai_agents.overrides(interaction.guild_id)
        embed = discord.Embed(title="AI agents", color=discord.Color.blurple(),
                              description="Ask the AI in plain words; it hands the job to the right agent. An agent only gets a "
                                          "fixed short list of commands, can't give itself more, and everything it proposes still "
                                          "checks your real permissions and asks you to Confirm.")
        for a in ai_agents.all_agents():
            on = ov.get(a.name, a.enabled_by_default)
            need = ", ".join(p.replace("_", " ") for p in a.needs_perms) or "none"
            embed.add_field(name=f"{'\U0001f7e2' if on else '\u26aa'} {a.name}",
                            value=f"{a.description[:170]}\nNeeds: {need} \u00b7 up to {a.max_tool_calls} actions per task", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _set(self, interaction: discord.Interaction, agent: str, enabled: bool):
        await interaction.response.defer(ephemeral=True)
        if not _can_manage(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to do that.", ephemeral=True)
            return
        ok = await ai_agents.set_enabled(interaction.guild_id, agent, enabled, interaction.user.id)
        await interaction.followup.send(
            f"\u2705 Agent `{agent}` {'enabled' if enabled else 'disabled'}." if ok else f"No agent called `{agent}`.", ephemeral=True)

    @group.command(name="enable", description="Turn an agent on in this server")
    @app_commands.autocomplete(agent=_agent_names)
    async def enable(self, interaction: discord.Interaction, agent: str):
        await self._set(interaction, agent, True)

    @group.command(name="disable", description="Turn an agent off in this server")
    @app_commands.autocomplete(agent=_agent_names)
    async def disable(self, interaction: discord.Interaction, agent: str):
        await self._set(interaction, agent, False)

    @group.command(name="history", description="Recent agent tasks in this server (who asked, what they proposed)")
    async def history(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not _can_manage(interaction):
            await interaction.followup.send("You need the **Manage Server** permission to see agent history.", ephemeral=True)
            return
        rows = await ai_agents.history(interaction.guild_id, 10)
        if not rows:
            await interaction.followup.send("No agent tasks yet.", ephemeral=True)
            return
        lines = []
        for r in rows:
            acts = ", ".join(c.get("name", "?") for c in (r.get("proposed") if isinstance(r.get("proposed"), list) else [])) or "nothing"
            when = f"<t:{int(r['created_at'].timestamp())}:R>" if r.get("created_at") else ""
            task = discord.utils.escape_mentions(discord.utils.escape_markdown(str(r["task"])))[:90]
            lines.append(f"**#{r['id']}** {when} <@{r['user_id']}> \u2192 `{r['agent']}` \u00b7 {r['status']}\n  \u201c{task}\u201d \u2192 {acts}")
        await interaction.followup.send("\n".join(lines)[:1990], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(AIAgentsCog(bot))
