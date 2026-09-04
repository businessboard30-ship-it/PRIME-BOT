# path: discord_bot/cogs/_views_verification.py

"""
Join-verification gate: the persistent parts that live in the #verify
channel long-term (posted once by the wizard, clicked by new members
indefinitely afterward).

Two modes, one shared role-swap:
  - "button": VerifyButton just needs a click.
  - "captcha": VerifyButton opens a CaptchaModal with a small addition
    question instead of verifying immediately.

Built as discord.ui.DynamicItem (same pattern as every other persistent
button in this codebase — see _views_direct_paid.py's docstring) so the
button keeps working after bot restarts and never times out. The
guild_id rides in the custom_id itself; registered once via
bot.add_dynamic_items(...) in setup_hook.

CaptchaModal is deliberately NOT a DynamicItem — modals aren't shown
until a (persistent) button is clicked, so there's no restart-survival
problem to solve for it. Its correct answer is encoded directly in its
own custom_id (verify_captcha:<guild_id>:<a>:<b>) instead of kept in
memory, so a bot restart between "modal opened" and "modal submitted"
can't lose the question — the tradeoff is the answer is visible to
anyone inspecting the interaction payload client-side, which is fine
for a bot-filtering captcha (it only needs to stop naive script kiddies
hammering /interactions, not a determined human).
"""

import re
import logging
import random

import discord

from database import db

logger = logging.getLogger(__name__)

# custom_id shapes:
#   verify_btn:<guild_id>
#   verify_captcha:<guild_id>:<a>:<b>   (modal, not a DynamicItem — see above)
_VERIFY_BTN_RE = re.compile(r"^verify_btn:(\d+)$")
_CAPTCHA_MODAL_RE = re.compile(r"^verify_captcha:(\d+):(\d+):(\d+)$")


async def do_verify(member: discord.Member, config: dict) -> bool:
    """Swaps Unverified -> Verified (or just drops Unverified if no
    verified_role_id is configured). Returns True on success. Best-effort:
    a missing role / stale role id / missing bot permission just logs and
    reports failure rather than raising into the interaction handler."""
    guild = member.guild
    unverified_role = guild.get_role(config.get("unverified_role_id")) if config.get("unverified_role_id") else None
    verified_role = guild.get_role(config.get("verified_role_id")) if config.get("verified_role_id") else None
    try:
        if unverified_role and unverified_role in member.roles:
            await member.remove_roles(unverified_role, reason="Passed join verification")
        if verified_role:
            await member.add_roles(verified_role, reason="Passed join verification")
        return True
    except discord.Forbidden:
        logger.warning("verification: missing permission to swap roles for %s in guild %s", member.id, guild.id)
        return False
    except discord.HTTPException as e:
        logger.warning("verification: role swap failed for %s in guild %s: %s", member.id, guild.id, e)
        return False


class CaptchaModal(discord.ui.Modal, title="Verify you're human"):
    answer = discord.ui.TextInput(label="Your answer", placeholder="Type the number", max_length=5)

    def __init__(self, guild_id: int, a: int, b: int):
        super().__init__(custom_id=f"verify_captcha:{guild_id}:{a}:{b}")
        self.guild_id = guild_id
        self.a = a
        self.b = b
        self.answer.label = f"What is {a} + {b}?"

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This challenge isn't for this server.", ephemeral=True)
            return
        try:
            given = int(str(self.answer.value).strip())
        except ValueError:
            given = None
        if given != self.a + self.b:
            await interaction.response.send_message(
                "❌ That's not right. Click the verify button again to get a new question.", ephemeral=True
            )
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        config = await db.get_verification_config(self.guild_id, clone_id=clone_id)
        ok = await do_verify(interaction.user, config)
        if ok:
            await interaction.response.send_message("✅ You're verified — welcome in!", ephemeral=True)
        else:
            await interaction.response.send_message(
                "⚠️ Verified, but I couldn't update your roles — please ping a staff member.", ephemeral=True
            )


class VerifyButton(discord.ui.DynamicItem[discord.ui.Button], template=_VERIFY_BTN_RE.pattern):
    def __init__(self, guild_id: int):
        super().__init__(
            discord.ui.Button(
                label="I'm not a bot",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"verify_btn:{guild_id}",
            )
        )
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This button isn't for this server.", ephemeral=True)
            return
        clone_id = getattr(interaction.client, "clone_id", None)
        config = await db.get_verification_config(self.guild_id, clone_id=clone_id)
        if not config.get("enabled"):
            await interaction.response.send_message("Verification isn't currently active here.", ephemeral=True)
            return
        if config.get("mode") == "captcha":
            a, b = random.randint(1, 9), random.randint(1, 9)
            await interaction.response.send_modal(CaptchaModal(self.guild_id, a, b))
            return
        ok = await do_verify(interaction.user, config)
        if ok:
            await interaction.response.send_message("✅ You're verified — welcome in!", ephemeral=True)
        else:
            await interaction.response.send_message(
                "⚠️ I couldn't update your roles — please ping a staff member.", ephemeral=True
            )


def build_verify_panel_embed(guild_name: str, mode: str) -> discord.Embed:
    desc = "Click the button below to verify you're a real person and unlock the rest of the server."
    if mode == "captcha":
        desc += "\nYou'll be asked to solve a quick math question."
    embed = discord.Embed(title=f"Welcome to {guild_name} 👋", description=desc, color=discord.Color.green())
    return embed


def build_verify_panel_view(guild_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyButton(guild_id))
    return view


async def lockdown_guild_channels(guild: discord.Guild, unverified_role: discord.Role, verify_channel_id: int) -> int:
    """Applies the Unverified: view_channel=False overwrite across every
    existing channel except the verify channel (which gets the opposite
    overwrite so it's the one thing new members can see). Returns the
    number of channels touched. Best-effort per-channel — one failed
    overwrite (e.g. missing Manage Channels in that specific channel)
    doesn't stop the rest from being locked down."""
    touched = 0
    for channel in guild.channels:
        try:
            if channel.id == verify_channel_id:
                overwrite = channel.overwrites_for(unverified_role)
                overwrite.view_channel = True
                overwrite.send_messages = False
                await channel.set_permissions(unverified_role, overwrite=overwrite, reason="Verification setup")
            else:
                overwrite = channel.overwrites_for(unverified_role)
                overwrite.view_channel = False
                await channel.set_permissions(unverified_role, overwrite=overwrite, reason="Verification setup")
            touched += 1
        except discord.Forbidden:
            logger.warning("verification lockdown: missing permission on channel %s in guild %s", channel.id, guild.id)
        except discord.HTTPException as e:
            logger.warning("verification lockdown: failed on channel %s in guild %s: %s", channel.id, guild.id, e)
    return touched


DYNAMIC_ITEMS = [VerifyButton]
