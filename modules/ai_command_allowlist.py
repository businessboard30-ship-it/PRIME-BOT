# path: modules/ai_command_allowlist.py

"""
The AI-runnable command allowlist — generated from an audit of every
top-level slash command in discord_bot/cogs/, categorized by hand and
reviewed with the project owner before this file existed. See the PR that
added this file for the full excluded/included breakdown.

Nothing outside AI_COMMANDS can ever be executed by the AI, no matter what
a model call returns — modules/ai_command_guard.py treats an unrecognized
command name as a hard refusal, not a fallback.

Two things are true about every entry here and must stay true for anything
added later:
  1. It does NOT touch money — no Paystack/Gumroad, no premium unlock, no
     economy currency, no shop/credit spend. That's enforced a second time
     at runtime in ai_command_guard.py by module-path, specifically so a
     mistake here isn't the only thing standing between the AI and a
     payment flow.
  2. It is NOT bot-owner-only. Bot-owner commands (broadcast, monetization
     overrides, storage channel config, etc.) are gated to a single person
     across every server the bot is in — that's a different permission
     model than "does this guild member have the right Discord permission
     in this guild," which is the only kind of check the AI path does.
     They're excluded categorically, not case-by-case.

`requires_confirmation`: True for anything that mutates server/member
state — the AI must show the user what it's about to do and get an
explicit confirm click first (see ai_command_guard.execute_ai_command).
False only for pure lookups, where there's nothing to undo.

Heist Wars (/heist, /inventory, /loadout) is deliberately left out for now
even though it's not real-money — it's a virtual in-game economy, and that
was a judgment call flagged back to the project owner rather than assumed.
Revisit if/when there's an explicit decision to include it.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AICommandSpec:
    name: str                      # matches the slash command's registered name
    cog_module: str                # e.g. "discord_bot.cogs.moderation" — for the money/owner guard
    requires_confirmation: bool
    description: str                # short, for the AI's own tool-schema and for the confirm prompt


AI_COMMANDS = (
    # ── Moderation ──────────────────────────────────────────────────────
    AICommandSpec("kick", "discord_bot.cogs.moderation", True, "Kick a member from this server"),
    AICommandSpec("ban", "discord_bot.cogs.moderation", True, "Ban a member from this server"),
    AICommandSpec("unban", "discord_bot.cogs.moderation", True, "Unban a user by ID"),
    AICommandSpec("timeout", "discord_bot.cogs.moderation", True, "Timeout (mute) a member"),
    AICommandSpec("untimeout", "discord_bot.cogs.moderation", True, "Remove an active timeout from a member"),
    AICommandSpec("warn", "discord_bot.cogs.moderation", True, "Warn a member"),
    AICommandSpec("unwarn", "discord_bot.cogs.moderation", True, "Clear all warns for a member"),
    AICommandSpec("warns", "discord_bot.cogs.moderation", False, "Check a member's warn count"),
    AICommandSpec("modlogs", "discord_bot.cogs.moderation", False, "Show recent moderation actions"),
    AICommandSpec("purge", "discord_bot.cogs.moderation", True, "Bulk-delete recent messages in the current channel"),

    # ── Setup / config ──────────────────────────────────────────────────
    AICommandSpec("serversetup", "discord_bot.cogs.automation", True, "Guided setup wizard for this bot's features"),
    AICommandSpec("setupverification", "discord_bot.cogs.verification", True, "Set up join verification (anti-raid gate)"),
    AICommandSpec("modlog", "discord_bot.cogs.server_logs", True, "Set up server activity logging"),
    AICommandSpec("announce", "discord_bot.cogs.automation", True, "Schedule an announcement in a channel"),
    AICommandSpec("announcements", "discord_bot.cogs.automation", False, "List scheduled announcements"),
    AICommandSpec("cancelannouncement", "discord_bot.cogs.automation", True, "Cancel a scheduled announcement"),
    AICommandSpec("language", "discord_bot.cogs.language", True, "Choose the language the bot replies in"),

    # ── Info / read-only ────────────────────────────────────────────────
    AICommandSpec("serveranalytics", "discord_bot.cogs.analytics", False, "Snapshot of this server's size and activity"),
    AICommandSpec("rank", "discord_bot.cogs.leveling", False, "Show a member's level and XP"),
    AICommandSpec("leaderboard", "discord_bot.cogs.leveling", False, "Show this server's XP leaderboard"),
    AICommandSpec("help", "discord_bot.cogs.help", False, "See everything the bot can do, by category"),
    AICommandSpec("myclones", "discord_bot.cogs.clone_admin", False, "List the Discord bot clones you own"),
    AICommandSpec("discover", "discord_bot.cogs.discover", False, "Browse anime by category"),
    AICommandSpec("search", "discord_bot.cogs.discover", False, "Search for an anime by name"),
    AICommandSpec("imagesearch", "discord_bot.cogs.image_search", False, "Reverse image search"),
    AICommandSpec("news", "discord_bot.cogs.external_tools", False, "Get top headlines about a topic"),
    AICommandSpec("convert", "discord_bot.cogs.external_tools", False, "Convert an amount between currencies"),
    AICommandSpec("stock", "discord_bot.cogs.external_tools", False, "Get a stock's current price"),
    AICommandSpec("crypto", "discord_bot.cogs.external_tools", False, "Get a cryptocurrency's current price"),

    # ── AI ───────────────────────────────────────────────────────────────
    AICommandSpec("aichat", "discord_bot.cogs.ai_tools", False, "Chat with the AI"),
    AICommandSpec("newchat", "discord_bot.cogs.ai_tools", False, "Start a fresh AI conversation"),
    AICommandSpec("endchat", "discord_bot.cogs.ai_tools", False, "End the active AI conversation"),
    AICommandSpec("aiimage", "discord_bot.cogs.ai_tools", False, "Generate an image from a text prompt"),
    AICommandSpec("aistatus", "discord_bot.cogs.ai_tools", False, "Check daily AI usage"),

    # ── Misc ────────────────────────────────────────────────────────────
    AICommandSpec("invite", "discord_bot.cogs.admin", False, "Get this bot's invite link"),
    AICommandSpec("feedback", "discord_bot.cogs.feedback", True, "Send feedback to the bot owner"),
    AICommandSpec("submit", "discord_bot.cogs.submissions", True, "Submit an anime/movie for the catalog"),
    AICommandSpec("suggest", "discord_bot.cogs.suggestions", True, "Submit a suggestion for staff/members to vote on"),
    AICommandSpec("start", "discord_bot.cogs.quickstart", False, "Show the bot's setup quickstart"),
    AICommandSpec("download", "discord_bot.cogs.external_tools", False, "Download audio/video from a supported link"),
    AICommandSpec("removeclone", "discord_bot.cogs.clone_admin", True, "Deactivate one of your Discord bot clones"),
)

AI_COMMANDS_BY_NAME = {c.name: c for c in AI_COMMANDS}

# Second, independent guard — checked by module path at execution time, not
# just by which names made it into AI_COMMANDS above. If a future command
# in one of these modules is accidentally added to AI_COMMANDS, this still
# blocks it.
BANNED_COG_MODULE_MARKERS = (
    "payments_manual",
    "discord_bot.cogs.premium",       # old premium-groups cog, if still present
    "discord_bot.cogs.guild_premium", # /premium (Go Premium)
    "discord_bot.cogs.custom_role",   # paid perk
    "discord_bot.cogs.music",         # activate-pro payment confirmation
    "economy",
    "_store",                          # ai_store.py and any future *_store cogs
)

# Bot-owner-only cogs/commands — excluded regardless of money, since the AI's
# permission model only checks guild-level Discord permissions, which is the
# wrong check for something gated to a single global owner.
BANNED_OWNER_ONLY_COMMANDS = frozenset({
    "ownerbroadcast", "broadcaststatus", "ownermonetize", "paymentmode",
    "set-storage-channel", "hostingchannel",
    "approvepayment", "rejectpayment", "pendingpayments",
})


def is_command_allowed(name: str) -> Optional[AICommandSpec]:
    """Returns the AICommandSpec if this command may be run by the AI, else
    None. Callers must treat None as a hard refusal, never a fallback to
    'run it anyway'."""
    if name in BANNED_OWNER_ONLY_COMMANDS:
        return None
    spec = AI_COMMANDS_BY_NAME.get(name)
    if spec is None:
        return None
    if any(marker in spec.cog_module for marker in BANNED_COG_MODULE_MARKERS):
        return None
    return spec
