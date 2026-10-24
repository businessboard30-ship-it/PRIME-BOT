"""
/help — top-level command overview, grouped by category, with a search modal.

Exists mainly so the bot satisfies listing-site requirements (top.gg,
discordbotlist.com, etc. require a `help` command or alias) but it's also
genuinely useful: a new server admin has no other single place that lists
everything this bot can do across ~25 cogs.

Kept as static command data rather than walking self.bot.tree at runtime —
the tree includes owner-only commands (bot administration, clone
management internals, moderation of the moderation itself) that shouldn't
be advertised to every user, and a curated list reads better than a raw
dump of the bot's 250+ slash commands anyway.

Data model: CATEGORIES maps a category name to a list of (command_names,
description) pairs. command_names is a list because several related
commands often share one line/description (e.g. `/kick` `/ban` `/timeout`
`/warn` — manual moderation). This same structure both renders the category
embeds and powers `/help` search, so the two can never drift out of sync.
"""

import discord
from discord import app_commands
from discord.ext import commands


CATEGORIES: dict[str, list[tuple[list[str], str]]] = {
    "🛡️ Moderation & Safety": [
        (["kick", "ban", "unban", "timeout", "untimeout", "warn", "unwarn", "warns", "modlogs"], "manual moderation"),
        (["automod"], "configurable word/invite/mention filters and raid protection"),
        (["reactionrole"], "self-assignable role panels via reactions"),
        (["role"], "bulk-create self-assignable roles with a setup wizard"),
        (["welcome"], "welcome cards for new members — themes, colors, custom background, sticker/GIF"),
        (["setupverification"], "anti-raid join verification gate"),
        (["antiraid"], "raid protection: spike detection, lockdown and staff alerts"),
        (["scamshield"], "scam protection: status, allowed domains, link checker, plus premium strict mode and deep reports"),
        (["honeypot"], "free trap channel that auto-actions spam bots and hacked accounts"),
        (["modlog"], "server activity logging (roles, channels, members, moderation, voice, invites)"),
    ],
    "🎮 Leveling & Economy": [
        (["rank", "levelcard", "leaderboard"], "XP and leveling"),
        (["levelrole"], "level-up role rewards"),
        (["voicexp"], "voice-channel XP settings"),
        (["economy daily", "economy work", "economy beg", "economy balance", "economy coinflip", "economy rob", "economy buy", "economy vote", "economy watchad"], "server currency, jobs, and games"),
        (["shop"], "browse and manage the server shop"),
        (["ecoconfig"], "configure this server's economy"),
        (["referrals"], "invite your friends for rewards"),
    ],
    "🃏 Cards & Games": [
        (["card"], "trading cards — pull, trade, and browse the marketplace"),
        (["heist"], "Heist Wars operations console"),
        (["inventory", "loadout"], "Heist Wars inventory and loadout"),
        (["roastarena"], "inter-server roast battles — challenge another server and let the crowd vote"),
        (["giveaway"], "run giveaways"),
        (["discoverplayers"], "find and connect with other players/devs"),
    ],
    "🎌 Anime Discovery": [
        (["discover"], "browse trending anime by category"),
        (["search"], "search for an anime by name"),
        (["animecategory"], "manage your saved anime categories"),
        (["submit"], "submit an anime/movie for the catalog"),
        (["submissions"], "review submitted anime/movies (admin)"),
    ],
    "📣 Bump Network": [
        (["bumpsetup"], "pick this server's bump channel, language filter and NSFW/frequency settings"),
        (["bump now"], "bump this server to other opted-in servers"),
        (["bump edit"], "edit this server's bump listing, including the support server/channel link"),
    ],
    "🤖 AI Tools": [
        (["aichat"], "AI-powered chat (anime questions, recommendations, or anything)"),
        (["newchat", "endchat"], "manage your AI conversation"),
        (["aiimage"], "generate an image from a text prompt"),
        (["aistatus"], "check your daily AI usage"),
        (["imagesearch"], "reverse image search"),
        (["aistore"], "AI feature marketplace — chat with paid AI personas"),
    ],
    "🎬 Media & Integrations": [
        (["connect plex", "connect jellyfin", "connect gdrive"], "connect your own media server or cloud folder"),
        (["connect subscribe", "connect status", "connect disconnect"], "Media Connect subscription"),
        (["movie search", "movie play", "movie library"], "search and play from your connected library"),
        (["download"], "download audio or video from a supported link"),
        (["news"], "get top headlines about a topic"),
        (["convert"], "convert an amount between currencies"),
        (["stock"], "get a stock's current price and recent change"),
        (["crypto"], "get a cryptocurrency's current price"),
        (["alert"], "crypto price alerts"),
    ],
    "💰 Marketplace & Premium": [
        (["premium pay", "premium status"], "pay to unlock a Premium role in this server"),
        (["createpremium", "listpremium", "premiumadmin", "editpremium", "togglepremium", "verify"], "manage this server's premium groups (admin)"),
        (["ad"], "sponsored ads (owner-approved)"),
        (["marketplace"], "buy/sell services with other members"),
        (["botstore"], "directory of member-submitted bots"),
        (["archive"], "Bots Archive — submit, review, and browse"),
    ],
    "🧬 Bot Cloning": [
        (["myclones", "removeclone"], "manage clones you own"),
        (["clonemonetize"], "configure your clone's own pricing"),
    ],
    "⚙️ Utility & Server Setup": [
        (["dashboard"], "open the web dashboard to configure this server from your browser"),
        (["language"], "set your preferred language"),
        (["feedback"], "send feedback to the bot owner"),
        (["suggest"], "submit a suggestion for staff and members to vote on"),
        (["suggestions"], "configure the suggestion box"),
        (["schedule"], "schedule messages to a channel"),
        (["linkbutton"], "custom labeled link buttons for this server"),
        (["starboard"], "configure the starboard"),
        (["botprofile"], "edit the bot's name, avatar, banner and bio in this server (opens the wizard)"),
        (["ticket"], "configure a ticket system"),
        (["invites"], "invite tracker setup"),
        (["start"], "resend the setup quickstart DM (feature toggles, one tap each) to yourself"),
        (["serversetup"], "Server Owners Panel — configure every feature of this bot from one place"),
        (["customrole"], "create/restyle your own custom role"),
        (["style"], "restyle channel names with fancy fonts (wizard)"),
        (["autoresponder"], "manage auto-responses"),
        (["announce", "announcements", "cancelannouncement"], "scheduled announcements"),
        (["autopost"], "periodic bot feature posts in this server"),
        (["serveranalytics"], "snapshot of this server's size and activity, plus growth suggestions"),
        (["setup channels", "setup reportchannel", "setup servers", "setup roastme", "setup roaststart", "setup roast", "setup shiptrigger", "setup shipconfig", "setup downloadhub"], "misc server setup helpers"),
        (["cat"], "your cat menu: a cat picture, your cats, daily care, market"),
        (["catsetup"], "set up automatic cats in this server (channel, how many a day, spawn hours)"),
        (["invite"], "get this bot's invite link"),
        (["help"], "this menu"),
    ],
}

# Bump-network note kept out of the searchable data above (it's not a
# command) but still shown on that category's embed.
_BUMP_NOTE = "\nNote: your bump channel also receives other servers'/bots' bumps — that's required to use `/bump`"

MAX_SEARCH_RESULTS = 15


def _line_for(cmds: list[str], desc: str) -> str:
    return " ".join(f"`/{c}`" for c in cmds) + f" — {desc}"


def category_description(name: str) -> str:
    text = "\n".join(_line_for(cmds, desc) for cmds, desc in CATEGORIES[name])
    if name == "📣 Bump Network":
        text += _BUMP_NOTE
    return text


def search_commands(query: str) -> list[tuple[str, str, str]]:
    """Returns (command, description, category) tuples matching query
    against either the command name or its description, case-insensitive."""
    query = query.strip().lower()
    if not query:
        return []
    results = []
    for category, entries in CATEGORIES.items():
        for cmds, desc in entries:
            haystack = " ".join(cmds).lower() + " " + desc.lower()
            if query in haystack:
                for c in cmds:
                    results.append((c, desc, category))
    return results


def _HelpGoPremiumButton():
    """Kept as a factory so existing callers (HelpView, ai_tools.premium_view)
    don't change; returns the persistent, restart-safe button."""
    from discord_bot.cogs._views_premium import PremiumPitchButton
    return PremiumPitchButton()


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="See everything this bot can do, by category")
    async def help(self, interaction: discord.Interaction):
        # interaction.client is whichever bot process actually answered this
        # (the main bot, or a specific clone's own process/token) — using
        # its real user name here instead of a hardcoded brand means /help
        # says the right thing on every clone, not just the main bot.
        from discord_bot.cogs._views_help import build_home_view
        bot_name = interaction.client.user.display_name if interaction.client.user else "this bot"
        await interaction.response.send_message(
            view=build_home_view(bot_name, interaction.guild_id, getattr(interaction.client, "clone_id", None)),
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
