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
per user per server (6, or 30 in a Premium server; DMs 6) tracked in
ai_chat_usage.guild_id/kind, separate from the /aichat tier limits above.

Characters, voice and links (all without any new command):
* The AI talks as the user's chosen character (Gen Z / Gentle / Sensei, see
  modules/ai_prefs.py). Users change it by asking ("switch to gentle",
  "change character" -> a picker).
* The AI can reply with a voice message by itself, or the user can ask for
  one ("reply with a voice note"). "No more voice notes" turns the automatic
  ones off for that user. Text is always sent alongside the audio, and any
  voice failure falls back to plain text (modules/ai_voice.py).
* Links in a message are read and used in the answer (modules/ai_web.py).

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

from modules import leveling, ai_prefs, ai_voice
from modules.ai_features import (
    ai_chat, generate_image, check_ai_usage_limit, get_user_ai_usage, AI_USAGE_CAPS,
    get_or_create_active_session, mentions_other_bot, OTHER_BOT_REFUSAL,
    check_reply_limit, get_reply_usage, reply_cap_for,
    is_reply_chat_enabled, is_premium_question, premium_answer,
    is_bot_invite_question, is_support_invite_question, SUPPORT_BUTTON_MARKER,
    is_levelup_question, levelup_answer,
)
from modules.superbot_adapter import get_user_tier
from modules.command_reference import build_context, is_command_question
from modules.ai_command_guard import (
    get_qualifying_commands, resolve_and_check, execute_ai_command,
    AICommandDenied, send_cap_reached_prompt, AIConfirmView,
)
from modules.ai_command_allowlist import needs_confirmation
from discord_bot.cogs._ai_interaction_proxy import ProxyInteraction
from discord_bot.cogs._views_shared import ActionButton, NavView, NavCardView, refresh_button
from discord_clone_service import build_invite_url

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


def premium_view() -> discord.ui.View:
    """Blue 'Go Premium' button (same one /help uses) for premium/credits questions."""
    from discord_bot.cogs.help import _HelpGoPremiumButton
    view = discord.ui.View(timeout=None)
    view.add_item(_HelpGoPremiumButton())
    return view


def ai_reply_view(cog, owner_id: int) -> NavView:
    # Quit Chat button removed from replies (chat clutter); /endchat still works.
    return NavView([ActionButton("Usage", discord.ButtonStyle.secondary, cog, "aistatus", emoji="📊")])


def _support_button() -> Optional[discord.ui.Button]:
    """Same shape as the bot-invite button in _quick_answer: a link-style
    button, never a URL/markdown link inside the message text."""
    from config import DISCORD_SUPPORT_SERVER_INVITE
    if not DISCORD_SUPPORT_SERVER_INVITE:
        return None
    return discord.ui.Button(
        label="Support server", style=discord.ButtonStyle.link,
        emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE,
    )


def dashboard_button(bot, path: str = "/me/rank", label: str = "Open dashboard") -> Optional[discord.ui.Button]:
    """Link-style button to the member dashboard (needs a Discord sign-in
    there). Shown on the main bot AND on clones: the account pages list the
    asker's levels/ranks from clone bots too. Hidden only when no dashboard
    URL is configured."""
    import config
    base = str(config.DASH_PAGES_URL or "").strip().rstrip("/")
    if not base:
        return None
    if not base.startswith(("https://", "http://")):
        # Discord rejects a link button whose URL has no scheme, and that rejection fails the WHOLE AI reply.
        logger.warning("[aichat] DASH_PAGES_URL has no http(s):// scheme; not attaching the dashboard button")
        return None
    return discord.ui.Button(
        label=label, style=discord.ButtonStyle.link, emoji="📈",
        url=f"{base}/#{path}",
    )


async def send_with_fallbacks(send, view, dash_btn, file=None):
    """Send an AI reply so an optional extra can never make it vanish. `send(**kw)` is the real send. Tries everything
    first, then without the dashboard button, then also without the voice file; raises the last error only when even the
    plain reply is refused. `view` may be None."""
    tries = [(True, file)]
    if dash_btn is not None:
        tries.append((False, file))
    if file is not None:
        tries.append((False, None))
    last = None
    for keep_btn, f in tries:
        if not keep_btn and dash_btn is not None and view is not None and dash_btn in view.children:
            view.remove_item(dash_btn)
        kw = {}
        if view is not None:
            kw["view"] = view
        if f is not None:
            kw["file"] = f
        try:
            return await send(**kw)
        except (discord.Forbidden, discord.HTTPException) as e:
            last = e
            logger.warning("[aichat] reply send failed (%s); retrying with fewer extras", type(e).__name__)
    raise last


def extract_support_marker(text: str, view: discord.ui.View) -> str:
    """If the AI's free-form answer wanted to point the user to the support
    server (SUPPORT_BUTTON_MARKER from modules.ai_features), strip the marker
    out of the text and drop a real link button onto `view` instead — so the
    support link is masked by construction, exactly like the bot's own invite
    link, rather than relying on markdown link-masking inside the text."""
    if SUPPORT_BUTTON_MARKER not in text:
        return text
    text = text.replace(SUPPORT_BUTTON_MARKER, "").rstrip()
    btn = _support_button()
    if btn is not None:
        view.add_item(btn)
    return text


class CharacterPickView(discord.ui.View):
    """Dropdown to choose the AI's character. Only the person it was shown to can use it."""

    def __init__(self, owner_id: int, current: str):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        select = discord.ui.Select(
            placeholder="Choose a character",
            options=[
                discord.SelectOption(label=c["label"], value=key, emoji=c["emoji"],
                                     description=c["blurb"], default=(key == current))
                for key, c in ai_prefs.CHARACTERS.items()
            ],
        )
        select.callback = self._picked
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "That picker is for someone else — ask me to change character to get your own.", ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        key = interaction.data["values"][0]
        if await ai_prefs.set_character(interaction.user.id, key):
            await interaction.response.edit_message(content=ai_prefs.CHARACTERS[key]["intro"], view=None)
        else:
            await interaction.response.send_message(
                "Couldn't save that right now — try again in a moment.", ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class AIToolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _tier(self, user_id: int) -> str:
        tier = await get_user_tier(user_id)
        return tier if tier in AI_USAGE_CAPS else "basic"

    def _quick_answer(self, text: str, in_server: bool, guild: Optional[discord.Guild] = None):
        """Fixed answers that skip the AI call (and cost no daily cap):
        premium/credits -> Go Premium button, "add the bot to my server" -> this
        bot's own generated invite link, "support server link" -> the support
        link, "how do I level up / get XP" -> short explanation (the AI never
        advertises the XP boost). Returns (text, view_or_None) or None."""
        if is_premium_question(text):
            return premium_answer(in_server), (premium_view() if in_server else None)
        if is_levelup_question(text):
            return levelup_answer(in_server), None
        if is_support_invite_question(text):
            from config import DISCORD_SUPPORT_SERVER_INVITE, DISCORD_SUPPORT_SERVER_ID
            if guild is not None and DISCORD_SUPPORT_SERVER_ID and guild.id == DISCORD_SUPPORT_SERVER_ID:
                # Asking for the support server's own invite link while
                # already inside it doesn't add up — no join button.
                return "You're already in it — this is the support server.", None
            if not DISCORD_SUPPORT_SERVER_INVITE:
                return "We don't have a support server link set up right now.", None
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Join our support server", style=discord.ButtonStyle.link,
                emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE,
            ))
            return "Here you go — tap below to join the support server.", view
        if is_bot_invite_question(text):
            app_id = getattr(self.bot, "application_id", None)
            if app_id is None:
                return "I couldn't work out my invite link just now. Try `/invite` in a moment.", None
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Add me to your server", style=discord.ButtonStyle.link,
                emoji="➕", url=build_invite_url(app_id),
            ))
            return "➕ Tap below to add me to your server.", view
        return None

    async def _prefs_quick_answer(self, text: str, user_id: int):
        """"switch to gentle" / "change character" / "no more voice notes" —
        handled in chat, no AI call, costs no daily cap. (text, view) or None."""
        change = ai_voice.voice_preference_change(text)
        if change:
            if await ai_prefs.set_voice_mode(user_id, change):
                if change == ai_prefs.VOICE_OFF:
                    return ("🔇 Got it — no more voice notes unless you ask for one. "
                            "(Say \"you can use voice notes again\" to switch them back on.)"), None
                return "🔊 Voice notes are back on. I might reply with one when it fits, or just ask me for one.", None
            return "Couldn't save that right now — try again in a moment.", None
        req = ai_prefs.detect_character_request(text)
        if req == "menu":
            current, _ = await ai_prefs.get_prefs(user_id)
            return "🎭 Pick the character you want me to chat as:", CharacterPickView(user_id, current)
        if req:
            if await ai_prefs.set_character(user_id, req):
                return ai_prefs.CHARACTERS[req]["intro"], None
            return "Couldn't save that right now — try again in a moment.", None
        return None

    async def _voice_setting(self, user_id: int, text: str) -> Optional[str]:
        """What to tell ai_chat about voice for this message: "forced" if they
        asked for it, "auto" if the AI may choose, None if they turned it off."""
        if ai_voice.wants_voice(text):
            return "forced"
        _, mode = await ai_prefs.get_prefs(user_id)
        return None if mode == ai_prefs.VOICE_OFF else "auto"

    async def _voice_file(self, user_id: int, text: str, *, explicit: bool) -> Optional[discord.File]:
        """Audio attachment for a voice reply, or None (caller sends text only)."""
        try:
            character, _ = await ai_prefs.get_prefs(user_id)
            wav = await ai_voice.render_voice(user_id, text, ai_prefs.tts_voice(character), explicit=explicit)
            return discord.File(io.BytesIO(wav), filename="voice.wav") if wav else None
        except Exception:
            logger.exception("[aichat] voice render failed; falling back to text")
            return None

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
        clone_id = getattr(self.bot, "clone_id", None)
        lines = []
        if clone_id is None:
            # The asker's OWN global numbers only (summed over every server).
            try:
                g = await db.get_global_xp_rank(user_id)
            except Exception:
                logger.debug("[aichat] global xp lookup failed", exc_info=True)
                g = None
            if g:
                total = int(g["total_xp"] or 0)
                lines.append(
                    f"- the person asking (GLOBAL, all servers combined): level {leveling.compute_level(total)}, "
                    f"{total} total XP, global rank #{g['rank']} of {g['total_players']}"
                )
            else:
                lines.append("- the person asking (GLOBAL, all servers combined): not ranked yet")
        if guild is None:
            if not lines:
                return "FACT: XP and levels are per-server, so tell the user to ask this in a server (or use /rank there)."
            return (
                "FACTS about the asker's global leveling — quote these numbers exactly, share only this person's "
                "stats, and tell them their dashboard has more analysis (per-server ranks, global leaderboard):\n"
                + "\n".join(lines)
            )
        ids = [user_id] + [int(i) for i in re.findall(r"<@!?(\d+)>", message)]
        seen, server_lines = set(), []
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
            who = "the person asking (THIS SERVER)" if uid == user_id else (member.display_name if member else f"user {uid}")
            rank_txt = f", rank #{rk['rank']} of {rk['total_players']}" if rk else ", not ranked yet"
            server_lines.append(
                f"- {who}: level {p['level']}, {p['total_xp']} total XP "
                f"({p['current_xp_in_level']}/{p['xp_needed_for_next_level']} toward next level){rank_txt}"
            )
        lines = server_lines + lines
        if not lines:
            return None
        return (
            "FACTS from the leveling data — quote these numbers exactly, share only these people's "
            "stats, and tell them /rank shows the full card"
            + (" and their dashboard shows their ranks across servers" if clone_id is not None
               else " and their dashboard has more analysis (per-server ranks, global leaderboard)")
            + ":\n" + "\n".join(lines)
        )

    def _is_xp_question(self, message: str) -> bool:
        return bool(self._XP_WORDS.search(message or ""))

    _DASH_WORDS = re.compile(r"\bdashboard\b", re.IGNORECASE)

    def _dashboard_path(self, message: str):
        """Where the 'Open dashboard' button should go for this message, or None for no button: the rank page for XP
        questions, the account home when the person just asks about the dashboard."""
        if self._is_xp_question(message):
            return "/me/rank"
        if self._DASH_WORDS.search(message or ""):
            return "/me"
        return None

    _CLAN_WORDS = re.compile(r"\bclan(s)?\b", re.IGNORECASE)

    async def _clan_facts(self, message: str, user_id: int, guild: Optional[discord.Guild]) -> Optional[str]:
        """Real clan-roster facts for the asker when the message mentions
        clans, so the AI quotes the database instead of inventing members.
        Mirrors _xp_facts. Any user can ask this (not just existing
        members) — if a named clan is mentioned that clan's roster is
        shown; otherwise it resolves to the asker's OWN clan. If the
        asker hasn't been locked into a clan yet (no discord_clan_cards
        row — see database.py's get_clan_card_if_assigned, which is
        read-only and never auto-assigns), a FACT is returned telling the
        model to explain how to get one instead of guessing."""
        if not self._CLAN_WORDS.search(message):
            return None
        if guild is None:
            return "FACT: Clans are per-server, so tell the user to ask this in a server (or use /clan view there)."
        from modules import clan_cards
        clone_id = getattr(self.bot, "clone_id", None)
        filename = clan_cards.resolve_clan_filename(message)
        if filename is None:
            filename = await db.get_clan_card_if_assigned(guild.id, user_id, clone_id=clone_id)
            if filename is None:
                return (
                    "FACT: This user hasn't been locked into a clan on this server yet. Clans are assigned "
                    "automatically the first time someone runs /clan view, or automatically every 3 levels "
                    "gained. Tell them to run /clan view (or just keep chatting to gain XP) to get locked "
                    "into a clan, then ask again to see its members."
                )
        label = clan_cards.get_clan_label(filename)
        try:
            members = await db.get_clan_members(guild.id, filename, clone_id=clone_id, limit=10, offset=0)
            total = await db.get_clan_member_count(guild.id, filename, clone_id=clone_id)
        except Exception:
            logger.debug("[aichat] clan lookup failed", exc_info=True)
            return None
        if not members:
            return f"FACT: The **{label}** clan has no members locked in on this server yet."
        lines = []
        for i, m in enumerate(members, start=1):
            member_obj = guild.get_member(m["user_id"])
            name = member_obj.display_name if member_obj else f"user {m['user_id']}"
            lines.append(f"{i}. {name} — level {m['level']} ({m['total_xp']} xp)")
        more = f" (+{total - len(members)} more not shown)" if total > len(members) else ""
        fact = (
            f"FACT — **{label}** clan roster on this server, {total} member(s) total, ranked by XP{more}:\n"
            + "\n".join(lines) + "\nQuote this list as-is; mention /clan members shows the full paginated list."
        )
        # If the asker themselves holds a chief seat whose clan_slug differs
        # from their own locked clan, surface both explicitly — this is the
        # exact situation BOT_RULES rule 10 tells the model to explain
        # instead of calling a bug, so give it the real numbers to do that
        # with instead of relying on the general rule alone.
        try:
            own_locked = await db.get_clan_card_if_assigned(guild.id, user_id, clone_id=clone_id)
            chief_seat = await db.get_chief_seat_for_user(guild.id, user_id, clone_id=clone_id)
        except Exception:
            own_locked, chief_seat = None, None
        if chief_seat and own_locked:
            own_label = clan_cards.get_clan_label(own_locked)
            if chief_seat["clan_slug"] != own_label:
                fact += (
                    f"\nFACT — this asker's own locked clan is **{own_label}**, but they currently hold the "
                    f"Chief seat for **{chief_seat['clan_slug']}** (a different clan) because they're rank "
                    f"#{chief_seat['seat_rank']} on this server's leaderboard. Their level-up messages will "
                    f"say \"{chief_seat['clan_slug']}\" while they hold that seat — that's expected, not a bug."
                )
        return fact

    _GODHOOD_WORDS = re.compile(r"\bgod(hood|s)?\b|\btrial(s)?\b|\bchosen\b|\bascen(d|sion)\b", re.IGNORECASE)

    async def _godhood_facts(self, message: str, user_id: int, guild: Optional[discord.Guild]) -> Optional[str]:
        """Real godhood-gauntlet facts for the asker when the message
        mentions gods/trials/chosen/ascension, so the AI quotes the
        database instead of inventing trial progress or a deadline.
        Mirrors _clan_facts. Only ever looks at the ASKER's own row —
        unlike clans there's no "show me someone else's gauntlet" case in
        BOT_RULES, so this doesn't try to resolve a named user."""
        if not self._GODHOOD_WORDS.search(message):
            return None
        if guild is None:
            return "FACT: Godhood trials are per-server, so tell the user to ask this in a server."
        from modules import godhood_cards
        clone_id = getattr(self.bot, "clone_id", None)
        try:
            active = await db.get_active_godhood_trial(guild.id, user_id, clone_id=clone_id)
        except Exception:
            logger.debug("[aichat] godhood lookup failed", exc_info=True)
            return None
        if active is None:
            return (
                "FACT: This user has not been chosen by a god yet (or their last gauntlet already ended). "
                "Tell them it's a random 20% chance that triggers automatically on hitting level 10, 11, 20, "
                "21, 30, or 31 — there's no command to request it, just keep leveling up."
            )
        label = godhood_cards.get_godhood_label(active["god_filename"])
        trial_no = active["current_trial_no"]
        target_type = active["trial_target_type"]
        progress = active["trial_progress_amount"]
        target = active["trial_target_amount"]
        deadline = active["trial_deadline_at"]
        deadline_txt = f"<t:{int(deadline.timestamp())}:R>" if deadline else "no deadline set yet"
        target_desc = (
            f"{progress}/{target} combined messages+XP"
            if target_type == "activity_combined"
            else f"reach level {target} (currently tracked at {progress})"
        )
        return (
            f"FACT — this user was chosen by **{label}** and is on **Trial {trial_no} of 5**: "
            f"{target_desc}. Deadline: {deadline_txt}. Quote this as-is; don't invent a different god, "
            f"trial number, or deadline."
        )

    async def _command_context(self, message: str, user_id: int,
                                perms: Optional[discord.Permissions],
                                guild: Optional[discord.Guild],
                                runnable: Optional[set] = None) -> Optional[str]:
        """Only inject the full command list when the message actually looks
        like it's asking about the bot's commands — otherwise it drowns
        out the normal chat system prompt and the AI answers like a
        command-lookup tool for every message, including plain chat. XP
        facts (real leveling numbers) are appended when relevant.
        `runnable` (command names this user can actually have the AI run
        right now — see _build_command_tools) is threaded through to
        build_context so the "I can run this" framing lines up with what
        the tool schema actually offers this turn; without it, the model
        gets told "you cannot run commands yourself" even while a matching
        tool sits right there, and it'll describe instead of act."""
        command_context = build_context(self._perm_set(perms), self.bot, runnable=runnable) if is_command_question(message) else None
        xp_facts = await self._xp_facts(message, user_id, guild)
        if xp_facts:
            command_context = f"{command_context}\n\n{xp_facts}" if command_context else xp_facts
        clan_facts = await self._clan_facts(message, user_id, guild)
        if clan_facts:
            command_context = f"{command_context}\n\n{clan_facts}" if command_context else clan_facts
        godhood_facts = await self._godhood_facts(message, user_id, guild)
        if godhood_facts:
            command_context = f"{command_context}\n\n{godhood_facts}" if command_context else godhood_facts
        return command_context

    # ── AI-executed commands (natural-language tool calling) ───────────────
    # Only wired into /aichat below, where a real discord.Interaction exists
    # (execute_ai_command/_do_call and the Premium confirm button both need
    # one). The reply/DM listener runs off a plain discord.Message and is
    # deliberately left out — not a regression, that path never called
    # execute_ai_command before either.

    _OPTION_TYPE_JSON = {
        # discord.AppCommandOptionType name -> JSON schema type. user/member/
        # channel/role/mentionable/attachment all come back from the model as
        # plain text (an ID or a "<@id>"/"<#id>" mention) and get resolved to
        # real Discord objects in _resolve_tool_args below, since Groq's tool
        # schema has no native Discord types.
        "string": "string", "integer": "integer", "number": "number",
        "boolean": "boolean", "user": "string", "channel": "string",
        "role": "string", "mentionable": "string", "attachment": "string",
    }

    @staticmethod
    def _param_defs(command: discord.app_commands.Command) -> list:
        """Parameter definitions the AI may fill in. A cog can declare them
        itself via AI_TOOL_PARAMS = {command_name: [{name, type, description,
        enum?, required?}]} for commands that deliberately have NO slash
        options (e.g. /style, which just opens a wizard) but still accept
        arguments from the AI."""
        cog = getattr(command, "binding", None)
        declared = (getattr(cog, "AI_TOOL_PARAMS", None) or {}).get(command.name)
        if declared:
            return list(declared)
        return [
            {
                "name": p.name,
                "type": getattr(p.type, "name", "string"),
                "description": p.description or p.display_name or p.name,
                "enum": [c.value for c in p.choices] if getattr(p, "choices", None) else None,
                "required": bool(p.required),
            }
            for p in command.parameters
        ]

    def _command_tool_schema(self, spec, command: discord.app_commands.Command) -> dict:
        properties, required = {}, []
        for d in self._param_defs(command):
            json_type = self._OPTION_TYPE_JSON.get(d.get("type", "string"), "string")
            prop = {"type": json_type, "description": str(d.get("description") or d["name"])[:200]}
            if d.get("enum"):
                prop["enum"] = list(d["enum"])
            properties[d["name"]] = prop
            if d.get("required"):
                required.append(d["name"])
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }

    async def _build_command_tools(self, interaction: discord.Interaction) -> list:
        """Tool schema list for whatever this user actually qualifies to run
        right now (get_qualifying_commands already applies the live
        permission + daily-cap check) — empty outside a guild."""
        if interaction.guild is None:
            return []
        specs = await get_qualifying_commands(interaction)
        tools = []
        for spec in specs:
            command = interaction.client.tree.get_command(spec.name) or next(
                (c for c in interaction.client.tree.walk_commands() if c.name == spec.name), None
            )
            if command is not None:
                tools.append(self._command_tool_schema(spec, command))
        return tools

    @staticmethod
    def _extract_id(value) -> Optional[int]:
        if value is None:
            return None
        m = re.match(r"^<[@#][!&]?(\d+)>$", str(value).strip())
        if m:
            return int(m.group(1))
        try:
            return int(str(value).strip())
        except ValueError:
            return None

    def _resolve_tool_args(self, interaction: discord.Interaction, command: discord.app_commands.Command, args: dict) -> dict:
        """Turns the model's plain-text args into the real objects the
        command's own callback expects, for the Discord-entity parameter
        types. Anything that fails to resolve is left out — the command's
        own parameter validation (required-arg check inside _do_call) is
        what ultimately catches that, same as a human leaving a field
        blank in the slash-command UI."""
        resolved = {}
        by_name = {d["name"]: d for d in self._param_defs(command)}
        # Reply/mention chat (ProxyInteraction) feeds the model the reply chain,
        # which can contain OTHER people's @mentions (e.g. the bot's own
        # "🎉 @Coachjay leveled up!" message). The model could then pick that
        # person as the target of "what's my level" and show THEIR card. So a
        # user-type argument is only honoured if it's the asker themself or
        # was actually @mentioned in the asker's own message; otherwise it's
        # dropped and the command falls back to its default (the asker).
        proxy_message = getattr(interaction, "message", None)
        allowed_user_ids = None
        if proxy_message is not None:
            allowed_user_ids = {interaction.user.id} | {
                int(i) for i in re.findall(r"<@!?(\d+)>", proxy_message.content or "")
            }
        for key, value in (args or {}).items():
            p = by_name.get(key)
            type_name = (p or {}).get("type")
            if type_name in ("user", "mentionable"):
                uid = self._extract_id(value)
                if allowed_user_ids is not None and uid not in allowed_user_ids:
                    continue
                obj = interaction.guild.get_member(uid) if (uid and interaction.guild) else None
                if obj is not None:
                    resolved[key] = obj
            elif type_name == "channel":
                cid = self._extract_id(value)
                obj = interaction.guild.get_channel(cid) if (cid and interaction.guild) else None
                if obj is None and cid is None and interaction.guild is not None:
                    # The model often can't see a channel ID — accept "this"/"here"
                    # (the channel the user is chatting in) or a plain #name.
                    label = str(value).strip().lstrip("#").lower()
                    if label in ("this", "here", "current", "this channel", "current channel"):
                        obj = getattr(interaction, "channel", None)
                    else:
                        obj = discord.utils.find(lambda c: c.name.lower() == label, interaction.guild.channels)
                        if obj is None:
                            from modules.text_styles import to_plain, strip_brackets
                            def _plain(n):
                                return strip_brackets(to_plain(n)).lower().strip(" -")
                            want = _plain(label)
                            hits = [c for c in interaction.guild.channels if _plain(c.name) == want]
                            obj = hits[0] if len(hits) == 1 else None
                if obj is not None:
                    resolved[key] = obj
            elif type_name == "role":
                rid = self._extract_id(value)
                obj = interaction.guild.get_role(rid) if (rid and interaction.guild) else None
                if obj is not None:
                    resolved[key] = obj
            else:
                resolved[key] = value
        return resolved

    async def _dispatch_tool_call(self, ctx, name: str, raw_args: dict, *, real_interaction: bool) -> None:
        """Runs one AI-chosen command end to end, from either a real
        discord.Interaction (/aichat) or a ProxyInteraction (reply/mention
        chat — real_interaction=False). Always re-checks allowlist + real
        permission + daily cap via resolve_and_check (never trusts the
        earlier qualifying-commands filter alone), then either asks for
        confirmation or executes, and always turns AICommandDenied into
        the right user-facing response — the real Premium button for a
        cap hit, plain text for anything else.

        A confirmation-required command ALWAYS runs off a genuine
        Interaction: the Confirm button click itself, which Discord issues
        for real regardless of whether the original request was a slash
        command or a plain reply. Only a non-confirmation (read-only)
        command reached via reply/mention chat runs through the
        ProxyInteraction, since there's no button click to get a real one
        from and nothing here can mutate server/member state anyway.
        """
        spec, command, reason, cap_reached = await resolve_and_check(ctx, name, raw_args)
        if spec is None:
            if cap_reached:
                await send_cap_reached_prompt(ctx)
            else:
                await ctx.followup.send(reason, ephemeral=True)
            return

        resolved_args = self._resolve_tool_args(ctx, command, raw_args)

        async def _run(confirm_interaction: discord.Interaction):
            # confirm_interaction is always a real Interaction (the button
            # click), so this always uses the default invoke_directly=False.
            try:
                await execute_ai_command(confirm_interaction, name, **resolved_args)
            except AICommandDenied as exc:
                if exc.cap_reached:
                    await send_cap_reached_prompt(confirm_interaction)
                else:
                    await confirm_interaction.followup.send(str(exc), ephemeral=True)
            except discord.app_commands.CommandSignatureMismatch:
                # Discord's global command cache on THIS guild/shard hasn't
                # picked up a just-deployed signature change yet (global
                # syncs propagate on Discord's own schedule, not instantly
                # — see bot.py's startup sync log). Nothing wrong with the
                # command itself; it resolves on its own shortly after
                # deploy. Surface something the user can act on instead of
                # letting discord.py's unhandled-exception logger eat it.
                logger.warning(f"[ai_command] signature mismatch for '{name}' — Discord's command cache is stale post-deploy")
                await confirm_interaction.followup.send(
                    "That command was just updated and Discord hasn't finished syncing it yet — "
                    "give it a few minutes and try again.", ephemeral=True,
                )

        if needs_confirmation(spec, resolved_args):
            prompt = f"I'd run **/{name}** with `{resolved_args}` — confirm?"
            # A cog can supply a clearer confirm message (and refuse early,
            # before any confirm button) via AI_CONFIRM_PROMPTS = {"cmd": "method"};
            # the method returns (ok, text).
            hook = ((getattr(command.binding, "AI_CONFIRM_PROMPTS", None) or {}).get(name)
                    if command.binding is not None else None)
            if hook:
                ok, prompt = await getattr(command.binding, hook)(ctx, **resolved_args)
                if not ok:
                    await ctx.followup.send(prompt, ephemeral=True)
                    return
            view = AIConfirmView(ctx.user.id, _run)
            await ctx.followup.send(prompt, view=view, ephemeral=True)
        elif real_interaction:
            await _run(ctx)
        else:
            try:
                await execute_ai_command(ctx, name, invoke_directly=True, **resolved_args)
            except AICommandDenied as exc:
                if exc.cap_reached:
                    await send_cap_reached_prompt(ctx)
                else:
                    await ctx.followup.send(str(exc))

    async def _run_chat_turn(self, user_id: int, message: str,
                              perms: Optional[discord.Permissions] = None,
                              guild: Optional[discord.Guild] = None,
                              interaction: Optional[discord.Interaction] = None,
                              voice: Optional[str] = None) -> tuple[str, str, Optional[int]]:
        """Shared by /aichat and the reply-to-continue listener. Returns
        (reply_text, warning, session_id). session_id is None only if
        usage was denied (caller should stop before sending anything).
        `perms` scopes which commands the AI is even told about — see
        modules/command_reference.py. `interaction`, when given (/aichat
        only), also lets the model itself pick and run a real command; if
        it does, this handles that fully and returns ("", warning,
        session_id) so the caller sends nothing further."""
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

        tools = await self._build_command_tools(interaction) if interaction is not None else None
        runnable = {t["function"]["name"] for t in tools} if tools else None
        command_context = await self._command_context(message, user_id, perms, guild, runnable=runnable)
        response = await ai_chat(user_id, message, is_anime_question=is_anime, tier=tier,
                                  session_id=session_id, command_context=command_context, tools=tools or None,
                                  voice=voice)
        if not response:
            return "AI service error. Try again later.", warning, session_id

        if isinstance(response, dict):
            call = response["tool_calls"][0]
            await self._dispatch_tool_call(interaction, call["name"], call.get("arguments") or {}, real_interaction=True)
            return "", warning, session_id

        prefix = f"⚠️ {warning}\n\n" if warning else ""
        out = f"{prefix}{response}"
        if getattr(response, "voice", False):
            out = ai_voice.VoiceReply(out)
        return out, warning, session_id

    @app_commands.command(name="aichat", description="Chat with the AI (anime questions, recommendations, or anything)")
    @app_commands.describe(message="What do you want to ask or say?")
    async def aichat(self, interaction: discord.Interaction, message: str):
        message = message.strip()
        if not message or len(message) > 1000:
            await interaction.response.send_message("Message must be 1-1000 characters.", ephemeral=True)
            return

        quick = self._quick_answer(message, interaction.guild is not None, interaction.guild)
        if quick:
            quick_text, quick_view = quick
            kwargs = {"view": quick_view} if quick_view else {}
            await interaction.response.send_message(quick_text, suppress_embeds=True, **kwargs)
            return

        pref = await self._prefs_quick_answer(message, interaction.user.id)
        if pref:
            pref_text, pref_view = pref
            await interaction.response.send_message(
                pref_text, suppress_embeds=True, **({"view": pref_view} if pref_view else {}))
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
        text, _warning, session_id = await self._run_chat_turn(
            user_id, message, perms=perms, guild=interaction.guild, interaction=interaction,
            voice=await self._voice_setting(user_id, message),
        )
        if not text:
            # A tool call was made instead of a text reply — _run_chat_turn
            # (via _dispatch_tool_call) already sent the confirm prompt,
            # the command's own result, or a denial/Premium pitch.
            return
        view = ai_reply_view(self, user_id)
        is_voice = getattr(text, "voice", False)
        text = extract_support_marker(text, view)
        dash_btn = None
        dash_path = self._dashboard_path(message)
        if dash_path:
            dash_btn = dashboard_button(self.bot, dash_path)
            if dash_btn is not None:
                view.add_item(dash_btn)
        voice_file = None
        if is_voice:
            voice_file = await self._voice_file(user_id, text, explicit=ai_voice.wants_voice(message))

        async def _send(**kw):
            return await interaction.followup.send(text, wait=True, suppress_embeds=True, **kw)
        sent = await send_with_fallbacks(_send, view, dash_btn, voice_file)

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
    REPLY_CHAIN_MAX = 6      # messages of reply-chain context (incl. the one replied to)
    DM_CONTEXT_MAX = 6       # recent DM messages used as context
    CHANNEL_CONTEXT_MAX = 4  # extra channel messages prepended before the reply chain

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
        # Drop messages from other bots / webhooks entirely — their content
        # (including embed text pulled by _msg_text) must never reach the AI
        # as context because it is an untrusted prompt-injection vector.
        if m.author.bot and m.author.id != self.bot.user.id:
            return None
        if m.webhook_id is not None and m.author.id != self.bot.user.id:
            return None
        text = self._msg_text(m)
        if not text:
            return None
        role = "assistant" if m.author.id == self.bot.user.id else "user"
        return {"role": role, "content": text}

    async def _reply_chain(self, replied: discord.Message) -> list:
        """Build history from the reply chain (oldest first, up to
        REPLY_CHAIN_MAX), then prepend recent channel messages that came
        before the root of the chain. This means a third person jumping in
        and replying to the AI inherits the conversation's context rather
        than starting fresh — the AI sees what the channel was discussing."""
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
        chain_ids = {m.id for m in chain}

        # Pull a few channel messages that predate the oldest chain message
        # so the AI isn't blind to what triggered the conversation. We
        # de-dup against chain_ids to avoid repeating messages already
        # in the reply chain.
        channel_ctx: list[dict] = []
        root = chain[0] if chain else replied
        try:
            async for m in replied.channel.history(
                limit=self.CHANNEL_CONTEXT_MAX + 5, before=root
            ):
                is_other_bot = (m.author.bot or m.webhook_id is not None) and m.author.id != self.bot.user.id
                if m.id in chain_ids or is_other_bot:
                    continue
                c = self._as_chat_message(m)
                if c:
                    channel_ctx.append(c)
                if len(channel_ctx) >= self.CHANNEL_CONTEXT_MAX:
                    break
        except (discord.Forbidden, discord.HTTPException):
            pass
        channel_ctx.reverse()

        chain_msgs = [c for c in (self._as_chat_message(m) for m in chain) if c]
        return channel_ctx + chain_msgs

    async def _dm_context(self, message: discord.Message) -> list:
        out = []
        try:
            async for m in message.channel.history(limit=self.DM_CONTEXT_MAX, before=message):
                # Skip other bots in DM history (shouldn't normally appear,
                # but defence-in-depth: _as_chat_message also filters them).
                if m.author.bot and m.author.id != self.bot.user.id:
                    continue
                c = self._as_chat_message(m)
                if c:
                    out.append(c)
        except (discord.Forbidden, discord.HTTPException):
            return []
        out.reverse()
        return out

    async def _run_reply_turn(self, message: discord.Message, content: str, history: list):
        """Returns (text, view_or_None)."""
        if mentions_other_bot(content):
            return OTHER_BOT_REFUSAL, None  # refused before any AI call, costs no cap
        quick = self._quick_answer(content, message.guild is not None, message.guild)
        if quick:
            return quick  # fixed answer (premium button / invite link), no AI call, costs no cap
        pref = await self._prefs_quick_answer(content, message.author.id)
        if pref:
            return pref   # character / voice setting change, no AI call, costs no cap
        user_id = message.author.id
        guild = message.guild
        guild_id = guild.id if guild else None
        premium = await self._is_premium(guild_id) if guild_id else False
        allowed, cap_msg = await check_reply_limit(user_id, guild_id, premium)
        if not allowed:
            return cap_msg, None

        is_anime = any(kw in content.lower() for kw in ("anime", "manga", "character", "episode", "series", "watch", "recommend"))
        perms = message.channel.permissions_for(message.author) if guild else None

        proxy = ProxyInteraction(message, self.bot) if guild else None
        tools = await self._build_command_tools(proxy) if proxy is not None else []
        runnable = {t["function"]["name"] for t in tools} if tools else None
        command_context = await self._command_context(content, user_id, perms, guild, runnable=runnable)
        response = await ai_chat(
            user_id, content, is_anime_question=is_anime, tier="basic", session_id=None,
            command_context=command_context, history_override=history,
            guild_id=guild_id, kind="reply", tools=tools or None,
            voice=await self._voice_setting(user_id, content),
        )
        if isinstance(response, dict):
            call = response["tool_calls"][0]
            await self._dispatch_tool_call(proxy, call["name"], call.get("arguments") or {}, real_interaction=False)
            return "", None
        return (response or "AI service error. Try again later."), None

    @commands.Cog.listener("on_message")
    async def on_bot_chat(self, message: discord.Message):
        """Chat without /aichat: DM the bot, reply to one of its own messages
        in a server, or @mention it anywhere in a server. Stays silent in a
        channel with a live roast battle (the roast cog owns replies there)
        and when the per-server switch is off. Answers are short (see
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
                is_mention = self.bot.user in message.mentions
                replied = None
                ref = message.reference
                if ref and ref.message_id:
                    replied = ref.resolved if isinstance(ref.resolved, discord.Message) else None
                    if replied is None:
                        try:
                            replied = await message.channel.fetch_message(ref.message_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            replied = None
                if replied is not None:
                    # Only THIS bot's own messages: each clone is its own
                    # process and answers replies to itself, so nothing
                    # answers twice.
                    if replied.author.id != self.bot.user.id:
                        return
                elif not is_mention:
                    return
                if await self._roast_active_in(message.channel):
                    return
                if not await is_reply_chat_enabled(message.guild.id):
                    return
                history = await self._reply_chain(replied) if replied is not None else []
                if is_mention:
                    # Strip the "<@bot_id>" / "<@!bot_id>" mention token so
                    # the AI only sees the actual question, not raw mention
                    # markup.
                    content = re.sub(rf"<@!?{self.bot.user.id}>", "", content).strip()
                    if not content:
                        return

            voice_file = None
            async with message.channel.typing():
                text, view = await self._run_reply_turn(message, content, history)
                if text and getattr(text, "voice", False):
                    voice_file = await self._voice_file(
                        message.author.id, str(text), explicit=ai_voice.wants_voice(content))
            if not text:
                return
            if SUPPORT_BUTTON_MARKER in text:
                view = view or discord.ui.View()
                text = extract_support_marker(text, view)
            dash_btn = None
            dash_path = self._dashboard_path(content)
            if dash_path:
                dash_btn = dashboard_button(self.bot, dash_path)
                if dash_btn is not None:
                    view = view or discord.ui.View()
                    view.add_item(dash_btn)
            none = discord.AllowedMentions.none()

            async def _send(**kw):
                if message.guild is None:
                    return await message.channel.send(text, allowed_mentions=none, suppress_embeds=True, **kw)
                return await message.reply(text, mention_author=False, allowed_mentions=none, suppress_embeds=True, **kw)
            await send_with_fallbacks(_send, view if view else None, dash_btn, voice_file)
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
