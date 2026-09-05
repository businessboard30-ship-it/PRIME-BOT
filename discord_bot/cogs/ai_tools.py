"""
AI chat + image generation — Discord equivalent of handlers/ai_handler.py.

Reuses modules/ai_features.py (Groq chat, Fal/Gemini/Pollinations image gen,
per-tier daily caps) and modules/superbot_adapter.get_user_tier exactly
as-is — neither has any Telegram dependency.

DELIBERATELY NOT PORTED: handlers/utility_paywall.py's "2 free uses then
25 GHS/2mo via Paystack" gate. That's a Telegram-specific *global, per-user*
subscription tied to the Ghana-market payment flow — it doesn't compose
with how monetization already works on the Discord side (premium.py's
per-guild premium groups, priced and role-gated per server by each guild's
own admin). Bolting the old paywall on top would mean a user pays a guild
admin for one thing and Paystack for another, with no shared source of
truth. What's kept instead is the portable, platform-agnostic piece: the
per-tier daily caps from AI_USAGE_CAPS (basic/pro/elite/founder), same as
/aiimage always used. If you want AI chat/image gen to be a paid unlock on
Discord too, that's a real product decision (new premium-group perk? a
separate Paystack flow? tier granted some other way?) — happy to wire it
up once you pick a direction, rather than guessing.

i18n: bot-authored strings go through discord_bot.i18n_helpers.tr().
"""

import logging
import io
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
from modules.ai_features import (
    ai_chat, generate_image, check_ai_usage_limit, get_user_ai_usage, AI_USAGE_CAPS,
    get_or_create_active_session,
)
from modules.superbot_adapter import get_user_tier
from modules.command_reference import build_context, is_command_question
from discord_bot.cogs._views_shared import ActionButton, NavView, NavCardView, refresh_button

logger = logging.getLogger(__name__)

IMAGE_STYLES = ["anime", "realistic", "3d"]


class QuitChatButton(discord.ui.Button):
    """Red 'Quit Chat' button attached to every AI reply. Ends the user's
    active session on tap; if they never tap it, replying to the message
    (on_reply_continue) just keeps the conversation going — no /endchat
    needed either way. Kept generic (no cog callback) so it works whether
    session_id came from /aichat or the reply-to-continue listener.
    Locked to the original asker — anyone else tapping it gets a rejection
    instead of silently ending someone else's session."""

    def __init__(self, owner_id: int):
        super().__init__(label="Quit Chat", style=discord.ButtonStyle.danger, emoji="🛑")
        self.owner_id = owner_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who started this chat can end it.", ephemeral=True)
            return
        await db.end_ai_chat_session(interaction.user.id)
        for child in self.view.children:
            child.disabled = True
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send("👋 Conversation ended — reply or use `/aichat` to start fresh.", ephemeral=True)


def ai_reply_view(cog, owner_id: int) -> NavView:
    return NavView([QuitChatButton(owner_id), ActionButton("Usage", discord.ButtonStyle.secondary, cog, "aistatus", emoji="📊")])


class AIToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _tier(self, user_id: int) -> str:
        tier = await get_user_tier(user_id)
        return tier if tier in AI_USAGE_CAPS else "basic"

    @staticmethod
    def _perm_set(perms: Optional[discord.Permissions]) -> set:
        """Turns a discord.Permissions object into the set of attribute
        names that are True for this user in this channel — the same
        source moderation.py's _require_perm checks against, so a user
        only ever gets told about commands they could actually run."""
        if perms is None:
            return set()
        return {name for name, value in perms if value}

    async def _run_chat_turn(self, user_id: int, message: str,
                              perms: Optional[discord.Permissions] = None) -> tuple[str, str, Optional[int]]:
        """Shared by /aichat and the reply-to-continue listener. Returns
        (reply_text, warning, session_id). session_id is None only if
        usage was denied (caller should stop before sending anything).
        `perms` scopes which commands the AI is even told about — see
        modules/command_reference.py."""
        tier = await self._tier(user_id)
        allowed, warning = await check_ai_usage_limit(user_id, tier, "messages")
        if not allowed:
            return f"❌ {warning}", warning, None

        session_id = await get_or_create_active_session(user_id)

        anime_keywords = ("anime", "manga", "character", "episode", "series", "watch", "recommend")
        is_anime = any(kw in message.lower() for kw in anime_keywords)

        # Only inject the full command list when the message actually looks
        # like it's asking about the bot's commands — otherwise it drowns
        # out the normal chat system prompt and the AI answers like a
        # command-lookup tool for every message, including plain chat.
        command_context = build_context(self._perm_set(perms)) if is_command_question(message) else None
        response = await ai_chat(user_id, message, is_anime_question=is_anime, tier=tier,
                                  session_id=session_id, command_context=command_context)
        if not response:
            return "AI service error. Try again later.", warning, session_id
        if len(response) > 1900:
            response = response[:1900] + "..."

        prefix = f"⚠️ {warning}\n\n" if warning else ""
        return f"{prefix}{response}", warning, session_id

    @app_commands.command(name="aichat", description="Chat with the AI (anime questions, recommendations, or anything)")
    @app_commands.describe(message="What do you want to ask or say?")
    async def aichat(self, interaction: discord.Interaction, message: str):
        message = message.strip()
        if not message or len(message) > 1000:
            await interaction.response.send_message("Message must be 1-1000 characters.", ephemeral=True)
            return

        user_id = interaction.user.id
        allowed, warning = await check_ai_usage_limit(user_id, await self._tier(user_id), "messages")
        if not allowed:
            await interaction.response.send_message(f"❌ {warning}", ephemeral=True)
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException as e:
            # error code 40060 = "Interaction has already been acknowledged".
            # Seen in prod when two bot processes briefly overlap (a
            # redeploy where the old container hadn't fully exited) and
            # Discord dispatches the same interaction to both — one
            # process's defer() wins, the other's throws this instead of
            # a normal exception the user could recover from. There's no
            # valid interaction left for THIS process to respond on if
            # that's what happened, so just stop instead of letting an
            # unhandled CommandInvokeError surface as "the app didn't
            # respond" with no explanation in the logs.
            if getattr(e, "code", None) == 40060:
                logger.warning(f"[aichat] interaction already acknowledged (likely duplicate dispatch), user={user_id}")
                return
            raise

        # interaction.permissions (not interaction.user.guild_permissions) —
        # same reasoning as moderation.py's _require_perm: stays correct
        # even for user-installed contexts where guild_permissions is
        # unreachable.
        perms = interaction.permissions if interaction.guild else None
        text, _warning, session_id = await self._run_chat_turn(user_id, message, perms=perms)
        view = ai_reply_view(self, user_id)
        sent = await interaction.followup.send(text, view=view, wait=True)

        # Remember this message's id so a reply to it continues the same
        # session without the user having to retype /aichat.
        if session_id and sent is not None:
            await db.set_ai_chat_session_last_bot_message(session_id, sent.id)

    @app_commands.command(name="newchat", description="Start a fresh AI conversation (clears prior context)")
    async def newchat_cmd(self, interaction: discord.Interaction):
        session_id = await db.start_ai_chat_session(interaction.user.id)
        await interaction.response.send_message(
            "🆕 Started a new conversation — I won't recall anything before this. "
            "Use `/aichat` (or just reply to my messages) to keep chatting, `/endchat` when you're done.",
            ephemeral=True,
        )

    @app_commands.command(name="endchat", description="End your active AI conversation")
    async def endchat_cmd(self, interaction: discord.Interaction):
        ended = await db.end_ai_chat_session(interaction.user.id)
        if ended:
            await interaction.response.send_message("👋 Conversation ended — your next `/aichat` will start fresh.", ephemeral=True)
        else:
            await interaction.response.send_message("You don't have an active conversation right now.", ephemeral=True)

    @commands.Cog.listener("on_message")
    async def on_reply_continue(self, message: discord.Message):
        """Reply-to-continue: slash commands can't be "typed into" like a
        normal chat, so replying to the bot's own last /aichat message
        continues that same session — no need to re-invoke /aichat and
        retype context. Ignores bots, DMs from the bot itself, and any
        message that isn't a direct reply to a tracked bot message."""
        if message.author.bot:
            return
        if not message.reference or not message.reference.message_id:
            return
        if message.reference.resolved and getattr(message.reference.resolved, "author", None) != self.bot.user:
            return

        session = await db.get_ai_chat_session_by_last_bot_message(message.reference.message_id)
        if not session or session["user_id"] != message.author.id:
            return

        content = message.content.strip()
        if not content or len(content) > 1000:
            return

        perms = message.channel.permissions_for(message.author) if message.guild else None
        async with message.channel.typing():
            text, _warning, session_id = await self._run_chat_turn(message.author.id, content, perms=perms)
        sent = await message.reply(text, mention_author=False, view=ai_reply_view(self, message.author.id))
        if session_id:
            await db.set_ai_chat_session_last_bot_message(session_id, sent.id)

    @app_commands.command(name="aiimage", description="Generate an image from a text prompt")
    @app_commands.describe(prompt="Describe the image you want", style="Art style (default: anime)")
    @app_commands.choices(style=[app_commands.Choice(name=s, value=s) for s in IMAGE_STYLES])
    async def aiimage(self, interaction: discord.Interaction, prompt: str, style: app_commands.Choice[str] = None):
        prompt = prompt.strip()
        if not prompt or len(prompt) > 500:
            await interaction.response.send_message("Prompt must be 1-500 characters.", ephemeral=True)
            return
        style_value = style.value if style else "anime"

        user_id = interaction.user.id
        tier = await self._tier(user_id)

        # Guard against a duplicate INTERACTION_CREATE dispatch (Discord can
        # occasionally redeliver the same interaction across a gateway
        # resume/reconnect — this previously crashed with "Interaction has
        # already been acknowledged" when a second, concurrent invocation of
        # this same callback reached defer() after the first had already
        # acknowledged it). If it's already been responded to, there's
        # nothing more for this invocation to do — the other one owns it.
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return

        allowed, warning = await check_ai_usage_limit(user_id, tier, "images")
        if not allowed:
            await interaction.followup.send(f"❌ {warning}\n\n💎 Higher tiers get more daily images.", ephemeral=True)
            return

        result = await generate_image(user_id, prompt, style_value)
        if not result or "error" in result:
            err = (result or {}).get("error", "Image generation failed. Try again.")
            await interaction.followup.send(f"❌ {err}")
            return

        prefix = f"⚠️ {warning}\n\n" if warning else ""
        buttons = [ActionButton("Usage", discord.ButtonStyle.secondary, self, "aistatus", emoji="📊")]
        caption = (result.get("prompt") or prompt)[:200]
        text_lines = [prefix + "### ✨ Generated image" if prefix else "### ✨ Generated image", caption,
                      f"-# Model: {result.get('model', 'Unknown')}"]
        text = discord.ui.TextDisplay("\n".join(text_lines))

        file = None
        if result.get("url"):
            # Fal AI: a stable hosted URL — MediaGalleryItem takes it directly.
            gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(result["url"]))
        elif result.get("image_bytes"):
            # Gemini/Pollinations: raw bytes with no hosted URL of their own.
            # MediaGalleryItem(file) builds the attachment://<filename>
            # reference for us, but the File itself still has to be passed
            # to followup.send(file=...) below — it is NOT auto-attached
            # just by living inside the view/gallery item.
            ext = "png" if "png" in (result.get("mime_type") or "") else "jpg"
            filename = f"generated.{ext}"
            file = discord.File(io.BytesIO(result["image_bytes"]), filename=filename)
            gallery = discord.ui.MediaGallery(discord.MediaGalleryItem(file))
        else:
            await interaction.followup.send("Image generation failed. Try again.")
            return

        row = discord.ui.ActionRow()
        for b in buttons:
            row.add_item(b)
        view = discord.ui.LayoutView()
        view.add_item(discord.ui.Container(text, gallery, discord.ui.Separator(), row, accent_colour=discord.Color.purple()))
        # MediaGalleryItem(file) only builds the attachment://<filename>
        # reference inside the component tree — discord.py does not walk
        # the view to auto-upload File objects, so the actual bytes must
        # still be attached explicitly or Discord rejects the whole
        # message with "referenced attachment was not found".
        if file is not None:
            await interaction.followup.send(view=view, file=file)
        else:
            await interaction.followup.send(view=view)

    @app_commands.command(name="aistatus", description="Check your daily AI usage")
    async def aistatus(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        tier = await self._tier(user_id)
        caps = AI_USAGE_CAPS[tier]

        messages_used = await get_user_ai_usage(user_id, "messages")
        images_used = await get_user_ai_usage(user_id, "images")

        line = (
            f"Tier: {tier.upper()}\n"
            f"Chat messages today: {messages_used}/{caps['daily_messages']}\n"
            f"Images today: {images_used}/{caps['daily_images']}\n"
            f"-# Limits reset daily at midnight UTC. Use /aichat and /aiimage."
        )
        buttons = [refresh_button(self, "aistatus")]
        card = NavCardView("🤖 AI usage status", [line], discord.Color.blurple(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIToolsCog(bot))
