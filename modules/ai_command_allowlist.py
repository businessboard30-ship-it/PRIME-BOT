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
    # Optional: when set, requires_confirmation only applies to a CALL that
    # actually passes one of these args (e.g. /style only mutates anything
    # when `font`/`channel` are given — a bare preview needs no confirm).
    confirm_only_with: tuple = ()
    # Discord permissions (names from discord.Permissions, e.g. "manage_guild") the asker must have. Enforced by
    # ai_command_guard on top of the command's own checks, so a command whose only gate lives inside its body can
    # never be offered to, or run for, someone who lacks the permission. Administrator always passes.
    needs_perms: tuple = ()
    # Full command path for a SUBCOMMAND of a group, e.g. "welcome enable" or "automod bannedword add". Empty means
    # the command is top-level and `name` is its registered name. `name` is always the unique tool name the model
    # sees (letters, digits, _ and - only), so for a subcommand it is the path with underscores: "welcome_enable".
    path: str = ""
    # AI-ONLY command. True means: the command exists in the code (so the AI can build its tool schema and run the
    # real permission checks from it) but it is DETACHED from the bot's command tree before slash commands are
    # synced, so it never appears in Discord's command list and a person cannot type it.
    #
    # WHY THIS EXISTS (the owner's reasoning -- read before "fixing" it): some operations are only worth having
    # when a person can describe the job in plain words ("make a channel for every day of the week, in a fancy
    # font", "remove the old event channels", "clear out that spammer's messages"). Typed by hand as a slash
    # command they are clunky, rarely used, and each one burns a slot of Discord's hard 100-command cap, which
    # this project is actively trying to free up (see the /tools and /economy merges). So they live here as
    # AI-callable operations ONLY. Do not "restore" them as slash commands, and do not count them against
    # command-cap budgeting -- they are not registered with Discord at all.
    ai_only: bool = False

    @property
    def command_path(self) -> str:
        return self.path or self.name


def _sub(path: str, module: str, confirm: bool, desc: str, perms: tuple = (), ai_only: bool = False) -> "AICommandSpec":
    """Spec for a subcommand of a group. `path` is the slash path, e.g. "welcome enable"."""
    return AICommandSpec(path.replace(" ", "_").replace("-", "_"), module, confirm, desc,
                         needs_perms=tuple(perms), path=path, ai_only=ai_only)


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
    AICommandSpec(
        "style", "discord_bot.cogs.style", True,
        "Rename a channel using a fancy unicode font. Args: `channel` (#mention/ID/name, or 'this' for the "
        "channel the user is chatting in), `font` (e.g. bold, script, kanji), `text` (optional \u2014 defaults to "
        "the channel's current name; channel names are always lowercased), `bracket` (optional wrapper/"
        "decoration around the name: cjk 【】, white_cjk 『』, corner 「」, double_angle 《》, tortoise 〔〕, "
        "math_white ⟦⟧, math_angle ⟪⟫, ornate ❰❱, heavy_angle ❮❯, curly_white ⦃⦄, guillemets «», "
        "flourish ꧁꧂, tibetan ༺༻, sparkle ✦, star ★, kira ⋆, or prefixes bar ┃, dot ・, "
        "thin_bar ︱, diamond ❖, arrow ➤, dotted_bar ┊). With font+channel it renames after the user "
        "confirms; with just channel it opens the style wizard. Needs Manage Channels.",
        confirm_only_with=("font", "channel", "text", "bracket"),
    ),

    # ── Info / read-only ────────────────────────────────────────────────
    AICommandSpec("serveranalytics", "discord_bot.cogs.analytics", False, "Snapshot of this server's size and activity"),
    AICommandSpec("rank", "discord_bot.cogs.leveling", False, "Show a member's level and XP"),
    AICommandSpec("leaderboard", "discord_bot.cogs.leveling", False, "Show this server's XP leaderboard"),
    AICommandSpec("levelcard", "discord_bot.cogs.leveling", False, "Open the level-up card designer (colours, font, bio, background, logo)"),
    AICommandSpec("help", "discord_bot.cogs.help", False, "See everything the bot can do, by category"),
    AICommandSpec("myclones", "discord_bot.cogs.clone_admin", False, "List the Discord bot clones you own"),
    AICommandSpec("discover", "discord_bot.cogs.discover", False, "Browse anime by category"),
    AICommandSpec("search", "discord_bot.cogs.discover", False, "Search for an anime by name"),
    _sub("tools imagesearch", "discord_bot.cogs.external_tools", False, "Reverse image search"),
    _sub("tools news", "discord_bot.cogs.external_tools", False, "Get top headlines about a topic"),
    _sub("tools convert", "discord_bot.cogs.external_tools", False, "Convert an amount between currencies"),
    _sub("tools stock", "discord_bot.cogs.external_tools", False, "Get a stock's current price"),
    _sub("tools crypto", "discord_bot.cogs.external_tools", False, "Get a cryptocurrency's current price"),

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
    _sub("tools download", "discord_bot.cogs.external_tools", False, "Download audio/video from a supported link"),
    AICommandSpec("removeclone", "discord_bot.cogs.clone_admin", True, "Deactivate one of your Discord bot clones"),

    # ── Fun / small setup commands ──────────────────────────────────────
    AICommandSpec("cat", "discord_bot.cogs.cat", False, "Show a random cute cat picture"),
    AICommandSpec("catsetup", "discord_bot.cogs.cat", True,
                  "Set up automatic cat spawns: pick the channel and how often (Premium raises the limits)",
                  needs_perms=("manage_guild",)),
    AICommandSpec("honeypot", "discord_bot.cogs.honeypot", True, "Set up the honeypot channel that catches spam bots",
                  needs_perms=("manage_guild",)),
    AICommandSpec("scamshield", "discord_bot.cogs.scam_shield", True,
                  "Open this server's Scam Shield settings (AI image check, allowed domains)",
                  needs_perms=("manage_guild",)),
    AICommandSpec("bumpsetup", "discord_bot.cogs.bump_setup", True, "Set up server bumping",
                  needs_perms=("manage_guild",)),

    # ── Auto-moderation ─────────────────────────────────────────────────
    _sub("automod status", "discord_bot.cogs.automod", False, "Show current auto-moderation settings"),
    _sub("automod toggle", "discord_bot.cogs.automod", True, "Turn an auto-mod filter on or off", ("manage_guild",)),
    _sub("automod action", "discord_bot.cogs.automod", True, "Set what happens when an auto-mod filter triggers", ("manage_guild",)),
    _sub("automod mentionthreshold", "discord_bot.cogs.automod", True, "Set how many mentions in one message count as spam", ("manage_guild",)),
    _sub("automod minaccountage", "discord_bot.cogs.automod", True, "Set the minimum account age for new joiners", ("manage_guild",)),
    _sub("automod bannedword list", "discord_bot.cogs.automod", False, "List blocked words and phrases"),
    _sub("automod bannedword add", "discord_bot.cogs.automod", True, "Add a blocked word or phrase", ("manage_guild",)),
    _sub("automod bannedword remove", "discord_bot.cogs.automod", True, "Remove a blocked word or phrase", ("manage_guild",)),

    # ── Server tools (subcommands of command groups) ────────────────────
    _sub("autopost status", "discord_bot.cogs.autopost", False, "Check this server's autopost configuration"),
    _sub("autopost setup", "discord_bot.cogs.autopost", True, "Turn on periodic bot feature posts in a channel", ("manage_guild",)),
    _sub("autopost disable", "discord_bot.cogs.autopost", True, "Turn off periodic bot feature posts", ("manage_guild",)),
    _sub("autoresponder list", "discord_bot.cogs.automation", False, "List auto-responses"),
    _sub("autoresponder add", "discord_bot.cogs.automation", True, "Add an auto-response trigger", ("manage_guild",)),
    _sub("autoresponder remove", "discord_bot.cogs.automation", True, "Remove an auto-response", ("manage_guild",)),
    _sub("giveaway start", "discord_bot.cogs.giveaways", True, "Start a giveaway (prize, duration, number of winners)", ("manage_guild",)),
    _sub("giveaway reroll", "discord_bot.cogs.giveaways", True, "Reroll the winner(s) of an ended giveaway", ("manage_guild",)),
    _sub("levelrole list", "discord_bot.cogs.leveling", False, "List the level-up role rewards"),
    _sub("levelrole add", "discord_bot.cogs.leveling", True, "Grant a role automatically at a given level", ("manage_roles",)),
    _sub("levelrole remove", "discord_bot.cogs.leveling", True, "Remove a level-up role reward", ("manage_roles",)),
    _sub("linkbutton list", "discord_bot.cogs.link_buttons", False, "List this server's custom link buttons", ("manage_guild",)),
    _sub("linkbutton add", "discord_bot.cogs.link_buttons", True, "Add a custom link button", ("manage_guild",)),
    _sub("linkbutton remove", "discord_bot.cogs.link_buttons", True, "Remove a link button by its label", ("manage_guild",)),
    _sub("linkbutton panel", "discord_bot.cogs.link_buttons", True, "Post the link buttons as a message in this channel", ("manage_guild",)),
    _sub("reactionrole list", "discord_bot.cogs.reaction_roles", False, "List every reaction-role panel in this server"),
    _sub("reactionrole create", "discord_bot.cogs.reaction_roles", True, "Post a new reaction-role panel in this channel", ("manage_roles",)),
    _sub("reactionrole add", "discord_bot.cogs.reaction_roles", True, "Add a role button to a reaction-role panel", ("manage_roles",)),
    _sub("reactionrole remove", "discord_bot.cogs.reaction_roles", True, "Remove a role button from a panel", ("manage_roles",)),
    _sub("schedule list", "discord_bot.cogs.schedule", False, "List scheduled messages in this server"),
    _sub("schedule once", "discord_bot.cogs.schedule", True, "Send a message once, after a delay", ("manage_guild",)),
    _sub("schedule recurring", "discord_bot.cogs.schedule", True, "Repost a message on a fixed interval", ("manage_guild",)),
    _sub("schedule daily", "discord_bot.cogs.schedule", True, "Post a message every day at a UTC time", ("manage_guild",)),
    _sub("schedule cancel", "discord_bot.cogs.schedule", True, "Cancel a scheduled message", ("manage_guild",)),
    _sub("starboard settings", "discord_bot.cogs.starboard", False, "Show the current starboard settings"),
    _sub("starboard setup", "discord_bot.cogs.starboard", True, "Set the starboard channel and star threshold", ("manage_guild",)),
    _sub("starboard emoji", "discord_bot.cogs.starboard", True, "Set which emoji counts toward the starboard", ("manage_guild",)),
    _sub("starboard disable", "discord_bot.cogs.starboard", True, "Turn off the starboard", ("manage_guild",)),
    _sub("voicexp settings", "discord_bot.cogs.voice_xp", False, "Show the voice XP settings"),
    _sub("voicexp toggle", "discord_bot.cogs.voice_xp", True, "Enable or disable voice XP", ("manage_guild",)),
    _sub("voicexp rate", "discord_bot.cogs.voice_xp", True, "Set how much XP is earned per minute in voice", ("manage_guild",)),
    _sub("voicexp afk", "discord_bot.cogs.voice_xp", True, "Choose whether time in the AFK channel counts", ("manage_guild",)),
    _sub("welcome enable", "discord_bot.cogs.welcome", True, "Turn welcome cards on in a channel", ("manage_guild",)),
    _sub("welcome disable", "discord_bot.cogs.welcome", True, "Turn welcome cards off", ("manage_guild",)),
    _sub("welcome message", "discord_bot.cogs.welcome", True, "Set the welcome text", ("manage_guild",)),
    _sub("welcome colors", "discord_bot.cogs.welcome", True, "Set the welcome card's background and accent colours", ("manage_guild",)),
    _sub("welcome style", "discord_bot.cogs.welcome", True, "Switch the welcome card between animated and static", ("manage_guild",)),
    _sub("welcome preview", "discord_bot.cogs.welcome", False, "Preview the welcome card"),
    _sub("roastarena status", "discord_bot.cogs.roast_arena", False, "Show this server's roast-battle status"),
    _sub("roastarena enable", "discord_bot.cogs.roast_arena", True, "Opt this server into inter-server roast battles", ("manage_guild",)),
    _sub("roastarena disable", "discord_bot.cogs.roast_arena", True, "Opt this server out of inter-server roast battles", ("manage_guild",)),
    _sub("suggestions log-channel", "discord_bot.cogs.suggestions", True, "Set the channel approved suggestions are logged to", ("manage_guild",)),
    _sub("clan view", "discord_bot.cogs.leveling", False, "Show a member's clan and chief status"),
    _sub("clan members", "discord_bot.cogs.leveling", False, "See who is in a clan, ranked by XP"),
    _sub("tools alert list", "discord_bot.cogs.external_tools", False, "Show your active crypto price alerts"),
    _sub("tools alert set", "discord_bot.cogs.external_tools", True, "Get notified when a coin crosses a price"),
    _sub("tools alert remove", "discord_bot.cogs.external_tools", True, "Remove your alert for one coin"),
    _sub("tools alert clear", "discord_bot.cogs.external_tools", True, "Remove all your price alerts"),
    _sub("botstore browse", "discord_bot.cogs.botstore", False, "Browse bots in the directory"),
    _sub("botstore trending", "discord_bot.cogs.botstore", False, "Show the most-clicked bots in the directory"),
    _sub("botstore search", "discord_bot.cogs.botstore", False, "Search the bot directory"),
    _sub("archive trending", "discord_bot.cogs.archive", False, "See the current top bots in the archive"),
    _sub("referral mycode", "discord_bot.cogs.referrals", False, "Show your referral code"),
    _sub("setup channels", "discord_bot.cogs.setup_channels", True, "Suggest and create commonly useful channels", ("manage_channels",)),
    # ── AI-ONLY operations (ai_only=True) ───────────────────────────────────
    # These three are hidden from Discord's slash-command list on purpose; only the AI can run them. See the long
    # comment on AICommandSpec.ai_only for the owner's reasoning. They still go through the full guard: allowlist,
    # the asker's real permissions (needs_perms), the daily cap, and an explicit Confirm click before anything runs.
    _sub("setup bulk-create", "discord_bot.cogs.setup_channels", True,
         "Bulk create several channels at once from a comma-separated list (max 15): text, voice or category, "
         "optionally inside a category and in a fancy font/bracket. Args: `names`, `kind`, `category`, `font`, "
         "`bracket`. ONE call with all the names \u2014 never one call per channel.", ("manage_channels",), ai_only=True),
    _sub("setup bulk-delete", "discord_bot.cogs.setup_channels", True,
         "Bulk delete several channels at once from a comma-separated list of names or #mentions (max 10). "
         "ONE call with all the names \u2014 never one call per channel. Needs Manage Channels.", ("manage_channels",), ai_only=True),
    _sub("messages user-purge", "discord_bot.cogs.moderation", True,
         "Delete one member's recent messages in a channel (user purge). Args: `member`, `channel` (default: this "
         "one), `amount` (max 200). Only looks at the past 13 days; never touches pinned messages.",
         ("manage_messages",), ai_only=True),
    _sub("setup reportchannel", "discord_bot.cogs.setup_channels", True, "Choose which channel receives server reports", ("manage_guild",)),
    _sub("invites setup", "discord_bot.cogs.invites", True, "Open the invite tracker setup wizard", ("manage_guild",)),
    _sub("ticket setup", "discord_bot.cogs.ticket", True, "Set up the ticket system", ("manage_guild",)),
    _sub("role setup", "discord_bot.cogs.role_setup", True, "Create common self-roles and post a member panel", ("manage_roles",)),
    _sub("bump now", "discord_bot.cogs.bump", True, "Bump this server to other opted-in servers", ("manage_guild",)),
    _sub("submissions pending", "discord_bot.cogs.submissions", False, "List submissions awaiting review", ("manage_guild",)),
    # ── Roles (cosmetic only: roles are created with NO permissions) ─────
    _sub("role create", "discord_bot.cogs.role_setup", True,
         "Create ONE role with no permissions. Args: `name`, `color` (name like red/blue or hex #ff8800), `hoist` "
         "(show members separately), `mentionable`. Needs Manage Roles.", ("manage_roles",)),
    _sub("role bulk-create", "discord_bot.cogs.role_setup", True,
         "Create several roles at once from a comma-separated list (max 15), all with no permissions. Args: `names`, "
         "`color`, `hoist`, `mentionable`. ONE call with all the names \u2014 never one call per role.", ("manage_roles",)),
    _sub("role bulk-delete", "discord_bot.cogs.role_setup", True,
         "Delete several roles at once from a comma-separated list of exact names (max 10). Only roles below both "
         "the bot's and the asker's top role. ONE call with all the names. Needs Manage Roles.", ("manage_roles",)),

    # ── More server setup (each keeps its own Discord-permission gate) ────
    _sub("messages purge", "discord_bot.cogs.moderation", True, "Bulk-delete recent messages in the current channel", ("manage_messages",)),
    _sub("automod setup", "discord_bot.cogs.automod", True, "Open the auto-moderation setup wizard", ("manage_guild",)),
    _sub("automod panel", "discord_bot.cogs.automod", False, "Open the auto-moderation control panel", ("manage_guild",)),
    _sub("automod dashboard", "discord_bot.cogs.automod", False, "Show the auto-moderation overview", ("manage_guild",)),
    _sub("automod setlogchannel", "discord_bot.cogs.automod", True, "Set the channel auto-mod actions are logged to", ("manage_guild",)),
    _sub("automod bannedword preset", "discord_bot.cogs.automod", True, "Load a preset list of blocked words", ("manage_guild",)),
    _sub("welcome setup", "discord_bot.cogs.welcome", True, "Open the welcome-card setup wizard", ("manage_guild",)),
    _sub("giveaway setup", "discord_bot.cogs.giveaways", True, "Open the guided giveaway wizard", ("manage_guild",)),
    _sub("levelrole setup", "discord_bot.cogs.leveling", True, "Open the leveling setup wizard", ("manage_roles",)),
    _sub("starboard community", "discord_bot.cogs.starboard", True, "Open the community-starboard settings", ("manage_guild",)),
    _sub("setup servers", "discord_bot.cogs.setup_channels", True, "Open the server-listing setup", ("manage_guild",)),
    _sub("setup downloadhub", "discord_bot.cogs.setup_channels", True, "Set up the download hub channel", ("manage_channels",)),
    _sub("setup roast", "discord_bot.cogs.setup_channels", True, "Set up the roast channel", ("manage_channels",)),
    _sub("setup shiptrigger", "discord_bot.cogs.setup_channels", True, "Set the ship-game trigger channel", ("manage_channels",)),
    _sub("setup shipconfig", "discord_bot.cogs.setup_channels", True, "Configure the ship game", ("manage_channels",)),
    _sub("botstore mine", "discord_bot.cogs.botstore", False, "List the bots you've submitted to the directory"),
    _sub("botstore rate", "discord_bot.cogs.botstore", True, "Rate a bot in the directory"),
    _sub("referral stats", "discord_bot.cogs.referrals", False, "See your referral count"),
    AICommandSpec("dashboard", "discord_bot.cogs.dashboard", False, "Get the link to this server's web dashboard",
                  needs_perms=("manage_guild",)),
    AICommandSpec("application", "discord_bot.cogs.applications", True, "Create an application form (role, staff, anything)",
                  needs_perms=("manage_guild",)),
    AICommandSpec("antiraid", "discord_bot.cogs.antiraid", True, "Set up raid protection: spike detection, lockdown, staff alerts",
                  needs_perms=("manage_guild",)),
)

AI_COMMANDS_BY_NAME = {c.name: c for c in AI_COMMANDS}


def needs_confirmation(spec: AICommandSpec, args: dict) -> bool:
    """Whether THIS call (not just this command) needs an explicit confirm
    click. Same as spec.requires_confirmation unless the spec narrows it
    with confirm_only_with."""
    if not spec.requires_confirmation:
        return False
    if not spec.confirm_only_with:
        return True
    return any((args or {}).get(k) not in (None, "") for k in spec.confirm_only_with)

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
    "set-storage-channel", "hostingchannel", "scamchannel",
    "approvepayment", "rejectpayment", "pendingpayments", "assignpayment",
})


def module_is_banned(module_path: str) -> bool:
    """True if a cog module path matches a money/premium marker. Checked against the REAL module the live command
    is bound to (not only what the spec claims), so a mislabelled spec can't slip a payment flow through."""
    return any(marker in (module_path or "") for marker in BANNED_COG_MODULE_MARKERS)


def runnable_labels(names) -> set:
    """What to put in the command reference's "I can run this" marker for these tool names: a top-level command's
    own name, plus for group subcommands both the group ("welcome") and the full path ("welcome enable")."""
    out = set()
    for n in names or ():
        spec = AI_COMMANDS_BY_NAME.get(n)
        if spec is None:
            continue
        out.add(spec.command_path)
        out.add(spec.command_path.split(" ", 1)[0])
    return out


def is_command_allowed(name: str) -> Optional[AICommandSpec]:
    """Returns the AICommandSpec if this command may be run by the AI, else
    None. Callers must treat None as a hard refusal, never a fallback to
    'run it anyway'."""
    if name in BANNED_OWNER_ONLY_COMMANDS:
        return None
    spec = AI_COMMANDS_BY_NAME.get(name)
    if spec is None:
        return None
    if module_is_banned(spec.cog_module):
        return None
    return spec
