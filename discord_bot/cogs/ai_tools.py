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

UPDATE: chat no longer needs /aichat. Replying to any message from this bot
(or its clone) starts/continues a chat, and DMs to the bot chat with no
command at all — see AIToolsCog.on_bot_chat. That path has its own daily cap
per user per server (10, or 30 in a Premium server; DMs 10) tracked in
ai_chat_usage.guild_id/kind, separate from the /aichat tier limits above.

i18n: bot-authored strings go through discord_bot.i18n_helpers.tr().
"""

import logging
import io
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from database import db
import re

from modules import leveling
from modules.ai_features import (
    ai_chat, generate_image, check_ai_usage_limit, get_user_ai_usage, AI_USAGE_CAPS,
    get_or_create_active_session, mentions_other_bot, OTHER_BOT_REFUSAL,
    check_reply_limit, get_reply_usage, reply_cap_for,
    is_reply_chat_enabled,
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
    # Quit Chat button removed from replies (chat clutter); /endchat still works.
    return NavView([ActionButton("Usage", discord.ButtonStyle.secondary, cog, "aistatus", emoji="📊")])


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

    _XP_WORDS = re.compile(r"\b(xp|level|levels|rank|ranking|exp)\b", re.IGNORECASE)

    async def _xp_facts(self, message: str, user_id: int, guild: Optional[discord.Guild]) -> Optional[str]:
        """Real leveling numbers for the asker (and anyone they @mention)
        when the message is about XP / level / rank, so the AI quotes the
        database instead of guessing. Leaderboards are public, so
        mentioned members are fine to include."""
        if not self._XP_WORDS.search(message):
            return None
        if guild is None:
            return "FACT: XP and levels are per-server, so tell the user to ask this in a server (or use /rank there)."
        clone_id = getattr(self.bot, "clone_id", None)
        ids = [user_id] + [int(i) for i in re.findall(r"<@!?(\d+)>", message)]
        seen, lines = set(), []
        for uid in ids[:4]:
            if uid in seen:
                continue
            seen.add(uid)
            try:
                row = await db.get_xp(guild.id, uid, clone_id=clone_id)
                p = leveling.xp_progress(row["total_xp"])
                rk = await db.get_xp_rank(guild.id, uid, clone_id=clone_id)
            except Exception:
                logger.debug("[aichat] xp lookup failed", exc_info=True)
                continue
            member = guild.get_member(uid)
            who = "the person asking" if uid == user_id else (member.display_name if member else f"user {uid}")
            rank_txt = f", rank #{rk['rank']} of {rk['total_players']}" if rk else ", not ranked yet"
            lines.append(
                f"- {who}: level {p['level']}, {p['total_xp']} total XP "
                f"({p['current_xp_in_level']}/{p['xp_needed_for_next_level']} toward next level){rank_txt}"
            )
        if not lines:
            return None
        return (
            "FACTS from this server's leveling data — quote these numbers exactly, share only these people's "
            "stats, and tell them /rank shows the full card:\n" + "\n".join(lines)
        )

    async def _command_context(self, message: str, user_id: int,
                                perms: Optional[discord.Permissions],
                                guild: Optional[discord.Guild]) -> Optional[str]:
        """Only inject the full command list when the message actually looks
        like it's asking about the bot's commands — otherwise it drowns
        out the normal chat system prompt and the AI answers like a
        command-lookup tool for every message, including plain chat. XP
        facts (real leveling numbers) are appended when relevant."""
        command_context = build_context(self._perm_set(perms), self.bot) if is_command_question(message) else None
        xp_facts = await self._xp_facts(message, user_id, guild)
        if xp_facts:
            command_context = f"{command_context}\n\n{xp_facts}" if command_context else xp_facts
        return command_context

    async def _run_chat_turn(self, user_id: int, message: str,
                              perms: Optional[discord.Permissions] = None,
                              guild: Optional[discord.Guild] = None) -> tuple[str, str, Optional[int]]:
        """Shared by /aichat and the reply-to-continue listener. Returns
        (reply_text, warning, session_id). session_id is None only if
        usage was denied (caller should stop before sending anything).
        `perms` scopes which commands the AI is even told about — see
        modules/command_reference.py."""
        if mentions_other_bot(message):
            # Refused before any AI call, so it doesn't spend the user's daily cap.
            return OTHER_BOT_REFUSAL, "", None
        tier = await self._tier(user_id)
        allowed, warning = await check_ai_usage_limit(user_id, tier, "messages")
        if not allowed:
            return f"❌ {warning}", warning, None

        session_id = await get_or_create_active_session(user_id)

        anime_keywords = ("anime", "manga", "character", "episode", "series", "watch", "recommend")
        is_anime = any(kw in message.lower() for kw in anime_keywords)

        command_context = await self._command_context(message, user_id, perms, guild)
        response = await ai_chat(user_id, message, is_anime_question=is_anime, tier=tier,
                                  session_id=session_id, command_context=command_context)
        if not response:
            return "AI service error. Try again later.", warning, session_id

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
        text, _warning, session_id = await self._run_chat_turn(user_id, message, perms=perms, guild=interaction.guild)
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
            "Use `/aichat` to keep chatting, `/endchat` when you're done. (Replying to my messages works too, with its own daily cap.)",
            ephemeral=True,
        )

    @app_commands.command(name="endchat", description="End your active AI conversation")
    async def endchat_cmd(self, interaction: discord.Interaction):
        ended = await db.end_ai_chat_session(interaction.user.id)
        if ended:
            await interaction.response.send_message("👋 Conversation ended — your next `/aichat` will start fresh.", ephemeral=True)
        else:
            await interaction.response.send_message("You don't have an active conversation right now.", ephemeral=True)

    # ── Reply / DM chat ──────────────────────────────────────────────────
    REPLY_CHAIN_MAX = 4      # messages of reply-chain context (incl. the one replied to)
    DM_CONTEXT_MAX = 6       # recent DM messages used as context

    async def _is_premium(self, guild_id: int) -> bool:
        """Premium is bought per server AND per bot (main vs. clone), so count
        the server as premium if this bot OR the main bot has it active."""
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            if await db.is_guild_premium_active(guild_id, clone_id):
                return True
            if clone_id is not None:
                return await db.is_guild_premium_active(guild_id, None)
        except Exception:
            logger.debug("[aichat] premium lookup failed", exc_info=True)
        return False

    async def _roast_active_in(self, channel: discord.abc.GuildChannel) -> bool:
        """True while a roast battle is live in THIS channel — the roast cog
        answers there, so the AI stays silent to avoid answering twice."""
        roast = self.bot.get_cog("RoastCog")
        if roast is not None and channel.id in getattr(roast, "_active_by_channel", {}):
            return True
        try:
            return bool(await db.fetchval(
                "SELECT 1 FROM discord_roast_arena_challenges "
                "WHERE status = 'active' AND battleground_channel_id = $1 LIMIT 1",
                channel.id,
            ))
        except Exception:
            logger.debug("[aichat] arena channel lookup failed", exc_info=True)
            return False

    @staticmethod
    def _msg_text(m: discord.Message) -> str:
        text = (m.content or "").strip()
        if not text and m.embeds:
            e = m.embeds[0]
            text = " — ".join(x for x in (e.title, e.description) if x)
        return text[:500]

    def _as_chat_message(self, m: discord.Message) -> Optional[dict]:
        text = self._msg_text(m)
        if not text:
            return None
        role = "assistant" if m.author.id == self.bot.user.id else "user"
        return {"role": role, "content": text}

    async def _reply_chain(self, replied: discord.Message) -> list:
        """The conversation is the reply chain itself (oldest first, up to
        REPLY_CHAIN_MAX messages) — not a per-user session — so separate
        topics in one channel never bleed into each other."""
        chain, cur = [], replied
        for _ in range(self.REPLY_CHAIN_MAX):
            chain.append(cur)
            ref = cur.reference
            if not ref or not ref.message_id:
                break
            nxt = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if nxt is None:
                try:
                    nxt = await cur.channel.fetch_message(ref.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    break
            cur = nxt
        chain.reverse()
        return [c for c in (self._as_chat_message(m) for m in chain) if c]

    async def _dm_context(self, message: discord.Message) -> list:
        out = []
        try:
            async for m in message.channel.history(limit=self.DM_CONTEXT_MAX, before=message):
                c = self._as_chat_message(m)
                if c:
                    out.append(c)
        except (discord.Forbidden, discord.HTTPException):
            return []
        out.reverse()
        return out

    async def _run_reply_turn(self, message: discord.Message, content: str, history: list) -> Optional[str]:
        if mentions_other_bot(content):
            return OTHER_BOT_REFUSAL  # refused before any AI call, costs no cap
        user_id = message.author.id
        guild = message.guild
        guild_id = guild.id if guild else None
        premium = await self._is_premium(guild_id) if guild_id else False
        allowed, cap_msg = await check_reply_limit(user_id, guild_id, premium)
        if not allowed:
            return cap_msg

        is_anime = any(kw in content.lower() for kw in ("anime", "manga", "character", "episode", "series", "watch", "recommend"))
        perms = message.channel.permissions_for(message.author) if guild else None
        command_context = await self._command_context(content, user_id, perms, guild)
        response = await ai_chat(
            user_id, content, is_anime_question=is_anime, tier="basic", session_id=None,
            command_context=command_context, history_override=history,
            guild_id=guild_id, kind="reply",
        )
        return response or "AI service error. Try again later."

    @commands.Cog.listener("on_message")
    async def on_bot_chat(self, message: discord.Message):
        """Chat without /aichat: reply to any message from this bot (or its
        clone) in a server, or just DM the bot. Stays silent in a channel with
        a live roast battle (the roast cog owns replies there) and where a
        server admin turned it off with /aireply. Answers are short (see
        BOT_RULES / trim_reply in modules/ai_features.py)."""
        if message.author.bot:
            return
        content = (message.content or "").strip()
        if not content or len(content) > 1000 or content.startswith(("!", "/")):
            return
        try:
            if message.guild is None:
                history = await self._dm_context(message)
            else:
                ref = message.reference
                if not ref or not ref.message_id:
                    return
                replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
                if replied is None:
                    try:
                        replied = await message.channel.fetch_message(ref.message_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        return
                # Only THIS bot's own messages: each clone is its own process
                # and answers replies to itself, so nothing answers twice.
                if replied.author.id != self.bot.user.id:
                    return
                if await self._roast_active_in(message.channel):
                    return
                if not await is_reply_chat_enabled(message.guild.id):
                    return
                history = await self._reply_chain(replied)

            async with message.channel.typing():
                text = await self._run_reply_turn(message, content, history)
            if not text:
                return
            none = discord.AllowedMentions.none()
            if message.guild is None:
                await message.channel.send(text, allowed_mentions=none)
            else:
                await message.reply(text, mention_author=False, allowed_mentions=none)
        except Exception:
            logger.exception("[aichat] reply/DM chat failed")

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
        try:
            if file is not None:
                await interaction.followup.send(view=view, file=file)
            else:
                await interaction.followup.send(view=view)
        except discord.HTTPException as e:
            # Discord's own explicit-content classifier can reject a
            # generated image after we've already rendered it (error code
            # 20009, "Explicit content cannot be sent to the desired
            # recipient(s)") — most often because age-restricted content
            # can't go to a non-age-gated channel/DM. This was previously
            # unhandled and surfaced to the user as a generic command
            # error instead of an explanation.
            if e.code == 20009:
                await interaction.followup.send(
                    "That image was flagged as explicit content and can't be sent here "
                    "(try an age-restricted channel, or adjust the prompt)."
                )
            else:
                raise

    @app_commands.command(name="aistatus", description="Check your daily AI usage")
    async def aistatus(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        tier = await self._tier(user_id)
        caps = AI_USAGE_CAPS[tier]

        messages_used = await get_user_ai_usage(user_id, "messages")
        images_used = await get_user_ai_usage(user_id, "images")

        gid = interaction.guild.id if interaction.guild else None
        reply_used = await get_reply_usage(user_id, gid)
        reply_cap = reply_cap_for(gid, await self._is_premium(gid) if gid else False)
        where = "in this server" if gid else "in DMs"
        line = (
            f"Tier: {tier.upper()}\n"
            f"/aichat messages today: {messages_used}/{caps['daily_messages']}\n"
            f"Reply chats today {where}: {reply_used}/{reply_cap}\n"
            f"Images today: {images_used}/{caps['daily_images']}\n"
            f"-# Limits reset daily at midnight UTC. Reply to me or DM me to chat; /aichat and /aiimage also work."
        )
        buttons = [refresh_button(self, "aistatus")]
        card = NavCardView("🤖 AI usage status", [line], discord.Color.blurple(), buttons)
        await interaction.response.send_message(view=card, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIToolsCog(bot))
