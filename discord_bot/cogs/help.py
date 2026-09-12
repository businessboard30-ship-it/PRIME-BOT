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

from config import DISCORD_SUPPORT_SERVER_INVITE

CATEGORIES: dict[str, list[tuple[list[str], str]]] = {
    "🛡️ Moderation & Safety": [
        (["kick", "ban", "unban", "timeout", "untimeout", "warn", "unwarn", "warns", "modlogs"], "manual moderation"),
        (["automod"], "configurable word/invite/mention filters and raid protection"),
        (["reactionrole"], "self-assignable role panels via reactions"),
        (["role"], "bulk-create self-assignable roles with a setup wizard"),
        (["welcome"], "welcome cards for new members"),
        (["setupverification"], "anti-raid join verification gate"),
    ],
    "🎮 Leveling & Economy": [
        (["rank", "leaderboard"], "XP and leveling"),
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
        (["bumpsetup"], "pick this server's bump channel (auto-suggests a description and tags for you)"),
        (["bump now"], "bump this server to other opted-in servers"),
        (["bump bot"], "add or bump a bot listing owned by this server"),
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
        (["registerclone"], "run your own branded clone of this bot (DM only)"),
        (["myclones", "removeclone"], "manage clones you own"),
        (["clonemonetize"], "configure your clone's own pricing"),
        (["botmanager"], "manage Discord bots you own by token"),
    ],
    "⚙️ Utility & Server Setup": [
        (["language"], "set your preferred language"),
        (["feedback"], "send feedback to the bot owner"),
        (["suggest"], "submit a suggestion for staff and members to vote on"),
        (["suggestions"], "configure the suggestion box"),
        (["schedule"], "schedule messages to a channel"),
        (["linkbutton"], "custom labeled link buttons for this server"),
        (["starboard"], "configure the starboard"),
        (["ticket"], "configure a ticket system"),
        (["invites"], "invite tracker setup"),
        (["start"], "resend the setup quickstart DM (feature toggles, one tap each) to yourself"),
        (["serversetup"], "guided setup wizard for this bot's features"),
        (["autoresponder"], "manage auto-responses"),
        (["announce", "announcements", "cancelannouncement"], "scheduled announcements"),
        (["autopost"], "periodic bot feature posts in this server"),
        (["serveranalytics"], "snapshot of this server's size and activity, plus growth suggestions"),
        (["setup channels", "setup reportchannel", "setup servers", "setup roastme", "setup roaststart", "setup roast", "setup shiptrigger", "setup shipconfig", "setup downloadhub"], "misc server setup helpers"),
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


def build_category_embed(name: str, *, highlight: str | None = None) -> discord.Embed:
    """highlight: a command name (without slash) to bold in the listing."""
    lines = []
    for cmds, desc in CATEGORIES[name]:
        line = _line_for(cmds, desc)
        if highlight and highlight in cmds:
            line = f"**{line}**"
        lines.append(line)
    text = "\n".join(lines)
    if name == "📣 Bump Network":
        text += _BUMP_NOTE
    return discord.Embed(title=name, description=text, color=discord.Color.blurple())


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


class SearchModal(discord.ui.Modal, title="Search commands"):
    query = discord.ui.TextInput(
        label="Command name or keyword",
        placeholder="e.g. mute, leveling, bump...",
        max_length=100,
    )

    def __init__(self, view: "HelpView"):
        super().__init__()
        self.help_view = view

    async def on_submit(self, interaction: discord.Interaction):
        matches = search_commands(str(self.query.value))

        if not matches:
            embed = discord.Embed(
                title="🔍 No matching commands",
                description=(
                    f"Nothing found for **{self.query.value}**.\n"
                    "Try a different keyword, or browse a category below."
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.edit_message(embed=embed, view=self.help_view)
            return

        if len(matches) == 1:
            cmd, desc, category = matches[0]
            embed = build_category_embed(category, highlight=cmd)
            embed.set_footer(text=f"Jumped here from your search: \"{self.query.value}\"")
            await interaction.response.edit_message(embed=embed, view=self.help_view)
            return

        shown = matches[:MAX_SEARCH_RESULTS]
        lines = [f"`/{cmd}` — {desc}  _({category})_" for cmd, desc, category in shown]
        embed = discord.Embed(
            title=f"🔍 Results for \"{self.query.value}\"",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        if len(matches) > MAX_SEARCH_RESULTS:
            embed.set_footer(text=f"Showing first {MAX_SEARCH_RESULTS} of {len(matches)} matches — try a more specific keyword.")
        await interaction.response.edit_message(embed=embed, view=self.help_view)


class SearchButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Search", emoji="🔍", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal(self.view))


class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name.split(" ", 1)[1], emoji=name.split(" ", 1)[0], value=name)
            for name in CATEGORIES
        ]
        super().__init__(placeholder="Choose a category...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        embed = build_category_embed(name)
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(CategorySelect())
        self.add_item(SearchButton())
        # Plain link button — no custom_id needed, so it needs no
        # DynamicItem registration and isn't affected by restarts/timeouts
        # on its own. Same "empty means omit rather than send broken" rule
        # as the join DM's version (discord_bot/cogs/_views_join_dm.py).
        if DISCORD_SUPPORT_SERVER_INVITE:
            self.add_item(discord.ui.Button(
                label="Join our support server", style=discord.ButtonStyle.link,
                emoji="🆘", url=DISCORD_SUPPORT_SERVER_INVITE, row=1,
            ))


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="See everything this bot can do, by category")
    async def help(self, interaction: discord.Interaction):
        # interaction.client is whichever bot process actually answered this
        # (the main bot, or a specific clone's own process/token) — using
        # its real user name here instead of a hardcoded brand means /help
        # says the right thing on every clone, not just the main bot.
        bot_name = interaction.client.user.display_name if interaction.client.user else "this bot"
        embed = discord.Embed(
            title=f"✨ {bot_name} — Help",
            description=(
                "Pick a category below, or tap 🔍 **Search** to find a command by name or keyword.\n\n"
                + "\n".join(f"**{name}**" for name in CATEGORIES)
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=HelpView(), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HelpCog(bot))
