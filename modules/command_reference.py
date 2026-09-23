"""
Command reference for /aichat — lets the AI tell anyone the exact
slash command + syntax for something ("how do I ban someone"), across
every command the bot has. The AI only ever *describes* commands, never
runs them — Discord's own permission system is the real gate when a
mod-only command actually gets used, so there's no harm in the AI naming
every command to everyone. What it must NOT do is guess: if something
isn't in this list, it should say so and point to /help instead of
inventing a command.

`perm` is the discord.Permissions attribute name that gates the command
in code (matches each cog's _require_perm calls), or None for anyone-
can-use commands. Shown for context ("needs Ban Members") — not used to
hide anything.
"""

from typing import Optional

# (command, usage, one-line description, required permission attr or None)
COMMANDS: list[tuple[str, str, str, Optional[str]]] = [
    # --- Moderation ---
    ("/ban", "/ban member:@user reason:<text>", "Ban a member from the server", "ban_members"),
    ("/unban", "/unban user_id:<id>", "Unban a user by ID", "ban_members"),
    ("/kick", "/kick member:@user reason:<text>", "Kick a member from the server", "kick_members"),
    ("/timeout", "/timeout member:@user duration:<minutes> reason:<text>", "Timeout (mute) a member", "moderate_members"),
    ("/untimeout", "/untimeout member:@user", "Remove an active timeout from a member", "moderate_members"),
    ("/warn", "/warn member:@user reason:<text>", "Warn a member", "moderate_members"),
    ("/unwarn", "/unwarn member:@user", "Clear all warns for a member", "moderate_members"),
    ("/warns", "/warns [member:@user]", "Check a member's warn count (defaults to yourself)", None),
    ("/modlogs", "/modlogs", "Show recent moderation actions in this server", "moderate_members"),

    # --- Server configuration (admin) ---
    ("/automod", "/automod ...", "Configure auto-moderation for this server", "manage_guild"),
    ("/automod bannedword", "/automod bannedword ...", "Manage the blocked word/phrase list", "manage_guild"),
    ("/welcome", "/welcome ...", "Configure welcome cards for new members", "manage_guild"),
    ("/reactionrole", "/reactionrole ...", "Self-assignable role panels", "manage_roles"),
    ("/levelrole", "/levelrole ...", "Configure level-up role rewards", "manage_roles"),
    ("/ecoconfig", "/ecoconfig ...", "Configure this server's economy", "manage_guild"),
    ("/shop", "/shop ...", "Browse and manage the server shop", "manage_guild"),
    ("/starboard", "/starboard ...", "Configure the starboard", "manage_guild"),
    ("/ticket", "/ticket ...", "Configure the ticket system", "manage_channels"),
    ("/giveaway", "/giveaway ...", "Run giveaways", "manage_guild"),
    ("/schedule", "/schedule ...", "Schedule messages to a channel", "manage_guild"),
    ("/suggestions", "/suggestions ...", "Configure the suggestion box", "manage_guild"),
    ("/voicexp", "/voicexp ...", "Configure voice-channel XP", "manage_guild"),
    ("/linkbutton", "/linkbutton ...", "Custom labeled link buttons for this server", "manage_guild"),
    ("/autoresponder", "/autoresponder ...", "Manage auto-responses", "manage_guild"),
    ("/announce", "/announce channel:#chan message:<text> time:<when>", "Schedule an announcement in a channel", "manage_guild"),
    ("/announcements", "/announcements", "List this server's scheduled announcements", "manage_guild"),
    ("/cancelannouncement", "/cancelannouncement id:<id>", "Cancel a scheduled announcement", "manage_guild"),
    ("/serversetup", "/serversetup", "Guided setup wizard for this bot's features", None),
    ("/animecategory", "/animecategory ...", "Manage your saved anime categories", None),

    # --- Premium / payments ---
    ("/pay", "/pay group:<name>", "Pay to unlock a Premium role in this server", None),
    ("/status", "/status", "Check your premium payment status in this server", None),
    ("/createpremium", "/createpremium ...", "[Admin] Create a new premium group in this server", "manage_guild"),
    ("/listpremium", "/listpremium", "List this server's premium groups", None),
    ("/editpremium", "/editpremium ...", "[Admin] Edit one of this server's premium groups", "manage_guild"),
    ("/togglepremium", "/togglepremium ...", "[Admin] Enable or disable one of this server's premium groups", "manage_guild"),
    ("/verify", "/verify member:@user group:<name>", "[Admin] Manually mark a user as paid for a premium group", "manage_guild"),

    # --- Economy / games ---
    ("/daily", "/daily", "Claim your daily currency bonus", None),
    ("/work", "/work", "Work a shift for currency", None),
    ("/beg", "/beg", "Beg for spare change", None),
    ("/balance", "/balance [member:@user]", "Check your (or someone's) balance", None),
    ("/leaderboard-economy", "/leaderboard-economy", "Show this server's richest members", None),
    ("/coinflip", "/coinflip amount:<n>", "Bet currency on a coin flip", None),
    ("/rob", "/rob member:@user", "Attempt to rob another member", None),
    ("/buy", "/buy item:<name>", "Buy an item from the shop", None),
    ("/vote", "/vote", "Vote for the bot for a currency bonus", None),
    ("/watchad", "/watchad", "View a sponsor message for a currency bonus", None),
    ("/heist", "/heist", "Open the Heist Wars operations console", None),
    ("/inventory", "/inventory", "View your Heist Wars inventory", None),
    ("/loadout", "/loadout", "Manage your equipped tools and cosmetics", None),

    # --- Leveling ---
    ("/rank", "/rank [member:@user]", "Show your (or someone's) level and XP", None),
    ("/leaderboard", "/leaderboard", "Show this server's top XP earners", None),

    # --- AI tools ---
    ("/aichat", "/aichat message:<text>", "Chat with the AI (anime questions, recommendations, or anything)", None),
    ("/newchat", "/newchat", "Start a fresh AI conversation (clears prior context)", None),
    ("/endchat", "/endchat", "End your active AI conversation", None),
    ("/aiimage", "/aiimage prompt:<text> style:<anime|realistic|3d>", "Generate an image from a text prompt", None),
    ("/aistatus", "/aistatus", "Check your daily AI usage", None),
    ("/aistore", "/aistore ...", "AI Store — chat with paid AI personas", None),

    # --- Discovery / search / utility ---
    ("/discover", "/discover", "Browse anime by category", None),
    ("/search", "/search query:<name>", "Search for an anime by name", None),
    ("/imagesearch", "/imagesearch", "Reverse image search — find where an image is from", None),
    ("/news", "/news topic:<text>", "Get top headlines about a topic", None),
    ("/convert", "/convert amount:<n> from:<cur> to:<cur>", "Convert an amount between currencies", None),
    ("/stock", "/stock symbol:<ticker>", "Get a stock's current price and recent change", None),
    ("/crypto", "/crypto symbol:<coin>", "Get a cryptocurrency's current price", None),
    ("/download", "/download url:<link>", "Download audio or video from a supported link", None),
    ("/currency", "/currency", "Set your preferred currency for paid features", None),
    ("/connect", "/connect ...", "Connect a media server or cloud folder you own", None),
    ("/movie", "/movie ...", "Search and play from your connected library", None),
    ("/language", "/language", "Choose the language I reply to you in", None),
    ("/feedback", "/feedback message:<text>", "Send feedback or a suggestion to the bot owner", None),
    ("/suggest", "/suggest text:<text>", "Submit a suggestion for staff and members to vote on", None),
    ("/submit", "/submit", "Submit an anime/movie for the catalog", None),
    ("/submissions", "/submissions ...", "[Admin] Review submitted anime/movies", "manage_guild"),
    ("/help", "/help", "See everything this bot can do, by category", None),
    ("/invite", "/invite", "Get this bot's invite link, so you can add it to another server", None),

    # --- Community / marketplace ---
    ("/ad", "/ad ...", "Sponsored ads (owner-approved)", None),
    ("/marketplace", "/marketplace ...", "Buy/sell services with other members", None),
    ("/referral", "/referral ...", "Ad/marketplace referral program (tracked, not a real payout)", None),
    ("/botstore", "/botstore ...", "Directory of member-submitted bots", None),
    ("/archive", "/archive ...", "Bots Archive — submit, review, and browse", None),
    ("/discoverplayers", "/discoverplayers ...", "Find and connect with other players/devs", None),
    ("/alert", "/alert ...", "Crypto price alerts", None),

    # --- Bot clones (owning your own instance) ---
    ("/registerclone", "/registerclone", "Run your own branded clone of this bot (DM only)", None),
    ("/myclones", "/myclones", "List the Discord bot clones you own", None),
    ("/removeclone", "/removeclone", "Deactivate one of your Discord bot clones", None),
    ("/clonemonetize", "/clonemonetize ...", "Monetization settings for a clone you own", None),
    ("/botmanager", "/botmanager ...", "Manage Discord bots you own by token", None),

    # --- Owner-only (bot owner, not server admin) ---
    ("/admin", "/admin ...", "[Owner] Bot administration", "owner_only"),
    ("/ownermonetize", "/ownermonetize ...", "[Owner] Force-activate monetization on any clone, no payment", "owner_only"),
    ("/autopost", "/autopost ...", "Periodic bot self-promo posts in this server", "owner_only"),
    ("/autopostcontent", "/autopostcontent ...", "[Owner] Manage the shared autopost content library", "owner_only"),
]


# Cheap keyword gate so the ~80-line command dump only gets added to the
# system prompt when the user is actually asking about the bot's commands.
# Without this, /aichat used to inject the full list on EVERY message
# (even "hey what's up"), which drowned out SYSTEM_PROMPT_GENERAL/ANIME
# and made the model answer like a command-lookup bot instead of chatting
# normally. This is intentionally permissive (false positives are cheap;
# false negatives mean a real command question gets a worse answer).
_COMMAND_QUESTION_HINTS = (
    "/", "command", "commands", "slash", "how do i", "how to", "how can i",
    "what commands", "bot do", "bot commands", "use the bot", "use this bot",
    "syntax", "usage", "ban ", "kick ", "timeout", "mute", "warn ",
    "premium", "economy", "leaderboard", "level up", "leveling",
    "help menu", "/help",
)


def is_command_question(message: str) -> bool:
    """Heuristic: does this message look like it's asking how to do
    something with the bot, as opposed to normal conversation?"""
    lowered = message.lower()
    return any(hint in lowered for hint in _COMMAND_QUESTION_HINTS)


def _live_lines(bot) -> list:
    """Command list generated from the bot's REAL command tree, so it can't
    go stale when commands are added/renamed. The hand-written COMMANDS
    table above is only used for the permission label when a command has
    no default_permissions of its own, and as a fallback if the tree is
    unavailable/empty."""
    try:
        import discord
        from discord import app_commands
        hand_perm = {c.split()[0].lstrip("/"): perm for c, _u, _d, perm in COMMANDS}
        lines = []
        for cmd in sorted(bot.tree.get_commands(type=discord.AppCommandType.chat_input), key=lambda c: c.name):
            dp = getattr(cmd, "default_permissions", None)
            needs = None
            if dp is not None:
                needs = ", ".join(n for n, v in dp if v) or None
            elif hand_perm.get(cmd.name):
                needs = hand_perm[cmd.name]
            tag = " [bot owner only]" if needs == "owner_only" else (f" [needs {needs}]" if needs else "")
            desc = (cmd.description or "").strip()[:90]
            if isinstance(cmd, app_commands.Group):
                subs = [c.qualified_name.split(" ", 1)[1] for c in cmd.walk_commands()
                        if isinstance(c, app_commands.Command)]
                lines.append(f"/{cmd.name} <{'|'.join(subs)[:200]}> — {desc}{tag}")
            else:
                args = " ".join(
                    (f"{p.name}:<..>" if p.required else f"[{p.name}:<..>]") for p in cmd.parameters
                )
                lines.append(f"/{cmd.name}{(' ' + args) if args else ''} — {desc}{tag}")
        return lines
    except Exception:
        return []


def build_context(user_perms: Optional[set] = None, bot=None, runnable: Optional[set] = None) -> str:
    """Returns a system-prompt snippet listing every command the bot has.
    Every command is visible to every asker — the bot doesn't hide its own
    command list, and Discord's permission system is what actually blocks
    a mod-only command from running, not what the AI is willing to talk
    about. Commands that need a permission just say so, same as /help
    would show. `user_perms` is accepted but unused (kept so callers don't
    need changes if per-user filtering is ever wanted again).

    `runnable`: names (no leading "/") of commands the AI can actually
    execute for THIS user right now via modules.ai_command_guard — the
    same set discord_bot.cogs.ai_tools built its tool schema from. Only
    those get the "I can run this" framing below and the corresponding
    system rule; everything else stays describe-only, same as before
    this parameter existed (the default, runnable=None, is unchanged
    behavior).
    """
    lines = _live_lines(bot) if bot is not None else []
    for cmd, usage, desc, perm in ([] if lines else COMMANDS):
        if perm == "owner_only":
            lines.append(f"{usage} — {desc} [only the bot's owner can use this]")
        elif perm:
            lines.append(f"{usage} — {desc} [needs '{perm}' permission]")
        else:
            lines.append(f"{usage} — {desc}")
    # The person is ALREADY talking to the AI, so never list the chat commands:
    # otherwise the model tells them to "use /aichat" while they are chatting.
    _chat_cmds = {"/aichat", "/newchat", "/endchat"}
    lines = [ln for ln in lines if ln.split(" ", 1)[0] not in _chat_cmds]
    runnable = runnable or set()
    if runnable:
        lines = [
            (f"{ln} [I can run this for you — use the matching tool instead of just describing it]"
             if ln.split(" ", 1)[0].lstrip("/") in runnable else ln)
            for ln in lines
        ]
    joined = "\n".join(lines)
    can_run_rule = (
        "- Some commands below are marked \"I can run this for you\" — for THOSE, call the matching "
        "tool instead of just telling the person to type the command; only fall back to describing "
        "the command as text for one that ISN'T marked that way.\n"
        if runnable else
        "- You can only tell someone what to type — you cannot run commands yourself.\n"
    )
    return (
        "You also know every command this Discord bot has, listed below. Rules:\n"
        "- The person is already chatting with you right now. Never tell them to use /aichat "
        "(or \"/ai chat\") to talk to you; just answer them.\n"
        "- Anyone can be told about any command, including mod/admin/owner-only ones — "
        "just mention the permission it needs, since Discord itself is what blocks someone "
        "without that permission from actually running it.\n"
        "- Never invent a command, flag, or syntax that isn't in this list.\n"
        "- If what someone wants isn't in this list at all, don't guess — say something "
        "like \"I don't see a command for that — try /help and scroll through the "
        "categories, there may be something close to what you need.\"\n"
        f"{can_run_rule}"
        "- Keep answers to 1-3 short sentences; give just the command and what it does.\n"
        f"{joined}"
    )
