# path: discord_bot/cogs/test_harness.py

"""
AI-ONLY bot test harness -- phase 1 skeleton (docs/BOT_TEST_HARNESS_PLAN.md).

Like discord_bot/cogs/ai_server_ops.py this cog is NEVER added to the slash tree: setup() hands the commands to the
AI-only registry, so nothing shows in Discord's command list and no command slot is used. Do NOT add it to the tree,
do NOT mention it in the README or help text.

Phase 1 has exactly one command, a read-only `testharness status`. Its job is to prove the three gates:
  1. offer gate     -- modules.ai_command_guard.get_qualifying_commands hides it everywhere except the test guild
  2. execution gate -- modules.ai_command_guard.resolve_and_check refuses before running
  3. handler gate   -- this file re-checks at the top of every handler (a direct call can't skip the guard)
No destructive scenario exists yet; each later phase is its own PR.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from modules import moderation_extra as modx
from modules import test_harness as th

logger = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
# Same wording the guard gives for an unknown command, so a refusal never confirms the feature exists.
REFUSAL = "That isn't something I'm able to run."


def handler_gate(interaction) -> bool:
    """Handler gate (layer 3). Call at the top of EVERY handler in this cog, and again before every destructive step."""
    return th.check_gates(
        guild_id=getattr(interaction, "guild_id", None),
        user_id=getattr(getattr(interaction, "user", None), "id", None),
        clone_id=getattr(getattr(interaction, "client", None), "clone_id", None),
    ) is None


class TestHarnessCog(commands.Cog):
    __test__ = False  # not a pytest class

    def __init__(self, bot):
        self.bot = bot

    harness_g = app_commands.guild_only()(app_commands.Group(name="testharness", description="Owner-only bot test harness"))

    @harness_g.command(name="status", description="Report whether the test harness is armed in this server")
    async def harness_status(self, interaction: discord.Interaction):
        if not handler_gate(interaction):
            if not interaction.response.is_done():
                await interaction.response.send_message(REFUSAL, ephemeral=True)
            else:
                await interaction.followup.send(REFUSAL, ephemeral=True)
            return
        text = (f"🧪 Test harness: **armed** in this server (phase 1: gates + skeleton, no scenarios yet). "
                f"Test guild `{th.test_guild_id()}`.")
        try:
            await modx.log_action(interaction.guild.id, "ai_testharness_status", interaction.user.id, reason="status check")
        except Exception:
            logger.exception("[test_harness] audit log failed")
        if not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
        else:
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot):
    """Deliberately NOT bot.add_cog (see module docstring). Clones never register it."""
    if getattr(bot, "clone_id", None) is not None:
        return
    from modules.ai_command_guard import register_hidden_cog
    n = register_hidden_cog(TestHarnessCog(bot))
    logger.info(f"[test_harness] registered {n} AI-only command(s) (not on the slash tree; off while TEST_GUILD_ID is 0)")
