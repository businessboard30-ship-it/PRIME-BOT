# path: modules/premium_perks.py

"""One place that says what Free, Premium and Premium Plus include.

Why this exists: Premium used to be described in several places that could drift apart -- the pitch
text in discord_bot/cogs/_views_premium.py, the per-feature lock labels (a 💎 here, a 🔒 there), and
the numeric limits scattered across config.py, ai_features.py, scam_vision.py, applications.py ... The
Premium wizard (discord_bot/cogs/_views_premium_v2.py) reads everything it shows from here, and the
numbers are *read from the code that enforces them*, never retyped, so the wizard cannot promise a limit
the feature does not actually apply.

Pure: no Discord, no database. perks() / limit_rows() build their values at call time so a changed
config value (or a test's monkeypatch) is picked up.

Adding a perk: add one Perk() below (pick a category). The one-screen pitch text (_views_premium.py) is
generated from this list by pitch_lines(), so it can never fall behind; set in_pitch=False to keep a perk out
of that short text (it still shows in the wizard), and `pitch=` to give it a shorter pitch line.
"""

from dataclasses import dataclass
from typing import Tuple

import config

INFINITY = "∞"


@dataclass(frozen=True)
class Category:
    key: str
    emoji: str
    label: str


CATEGORIES: Tuple[Category, ...] = (
    Category("mod", "🛡️", "Moderation & safety"),
    Category("community", "🎉", "Community"),
    Category("fun", "🎮", "Fun & games"),
    Category("ai", "🤖", "AI"),
    Category("style", "🎨", "Style & cards"),
    Category("music", "🎵", "Music"),
    Category("brand", "✨", "Branding & extras"),
)
CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}


@dataclass(frozen=True)
class Perk:
    key: str
    category: str
    emoji: str
    title: str
    blurb: str                 # what Premium adds, one line
    free: str = ""             # what a Free server gets ("" = not included)
    premium: str = ""          # Premium-specific detail ("" = simply included)
    plus: str = ""             # Plus-specific detail ("" = same as Premium)
    in_pitch: bool = True      # also listed in the one-screen pitch text (pitch_lines())
    pitch: str = ""            # shorter text for that pitch line ("" = the blurb, lower-cased, no full stop)


def perks() -> Tuple[Perk, ...]:
    # Imported here, not at module level: these pull in the database layer and the AI stack, and the
    # registry should stay cheap to import (and importable by tests that stub them).
    from modules import ai_features as ai
    from modules import applications as apps
    from modules import scam_vision as sv
    from modules import birthdays as bd
    from modules import cat_setup as cs
    from modules.anime_feed import FREE_FOLLOW_LIMIT as anime_free
    from modules.anime_plus import PLUS_FOLLOW_LIMIT as anime_plus

    free_colours = sum(1 for c in apps.COLORS if not c[3])
    all_colours = len(apps.COLORS)
    plus_ai = config.PREMIUM_PLUS_SCAM_VISION_DAILY
    cat_free, cat_prem = cs.FREE, cs.PREMIUM

    return (
        # ── Moderation & safety ───────────────────────────────────────────
        Perk("anti_raid", "mod", "🛡️", "Anti-raid Pro",
             "Join-profile filter, raid reports and quarantine-and-review.",
             free="Core anti-raid protection",
             premium="Profile filter, raid reports, quarantine review"),
        Perk("scam_shield", "mod", "🔍", "Scam Shield AI",
             "AI image scam checks, Strict mode (look-alike links) and 30-day deep reports.",
             free=f"{sv.FREE_GUILD_DAILY} AI checks a day · core shield",
             premium=f"{sv.PREMIUM_GUILD_DAILY} AI checks a day · Strict mode · deep reports",
             plus=f"{plus_ai} AI checks a day",
             pitch=f"{sv.PREMIUM_GUILD_DAILY} AI image scam checks a day (free: {sv.FREE_GUILD_DAILY}), Strict mode & deep reports"),
        Perk("verification", "mod", "✅", "Verification Pro",
             "Custom verify panel, log channel, auto-kick for no-shows, minimum account age and a post-verify welcome.",
             free="Captcha or button check, roles and lockdown",
             premium="Panel, log channel, auto-kick, account age, welcome message",
             pitch="custom panel, log channel, auto-kick & minimum account age"),
        Perk("honeypot", "mod", "🍯", "Honeypot extras",
             "Choose ban, kick or a 28-day timeout, how much history is wiped, and a dedicated log channel.",
             free="Ban + wipe the last 24h (the trap itself is free)",
             premium="Your choice of action, wipe length and log channel",
             in_pitch=False),
        # ── Community ─────────────────────────────────────────────────────
        Perk("giveaways", "community", "🎉", "Giveaways",
             "Bonus entries, auto-reroll, scheduling and custom colors.",
             free="Basic giveaways",
             premium="Bonus entries, auto-reroll, scheduling, colors"),
        Perk("tickets", "community", "🎫", "Tickets",
             "Categories, custom buttons, transcripts and auto-close.",
             free="Basic tickets",
             premium="Categories, custom buttons, transcripts, auto-close"),
        Perk("applications", "community", "📝", "Application Forms",
             f"{apps.MAX_FORMS_PREMIUM} live forms, {apps.MAX_QUESTIONS_PREMIUM} questions, "
             f"{all_colours} colours and an auto-role on accept.",
             free=f"{apps.MAX_FORMS_FREE} form · {apps.MAX_QUESTIONS_FREE} questions · {free_colours} colours",
             premium=f"{apps.MAX_FORMS_PREMIUM} forms · {apps.MAX_QUESTIONS_PREMIUM} questions · "
                     f"{all_colours} colours · auto-role"),
        Perk("custom_roles", "community", "🎨", "Custom Roles",
             "Every member styles their own role."),
        Perk("feeds", "community", "🔔", "Notification Feeds",
             "YouTube uploads and Roblox game updates posted in your channels, with an optional ping role.",
             pitch="YouTube & Roblox game feeds in your channels"),
        Perk("auto_bump", "community", "📣", "Auto Bump",
             "Bumps your server listing for you, automatically.",
             free="Manual bump",
             pitch="bumps your server listing for you, automatically"),
        # ── Fun & games ───────────────────────────────────────────────────
        Perk("roast", "fun", "💀", "Hardcore Roast",
             "Unfiltered roast battles.",
             free="Standard roasts"),
        Perk("cats", "fun", "🐱", "Cats",
             f"Up to {cat_prem.max_channels} spawn channels, {cat_prem.daily_min}-{cat_prem.daily_max} cats a day "
             "and trading with anyone by ID.",
             free=f"{cat_free.max_channels} channel · {cat_free.daily_min}-{cat_free.daily_max} cats a day",
             premium=f"{cat_prem.max_channels} channels · {cat_prem.daily_min}-{cat_prem.daily_max} cats a day · trade by ID",
             pitch=f"{cat_prem.max_channels} spawn channels and {cat_prem.daily_min}-{cat_prem.daily_max} cats a day "
                   f"(free: {cat_free.max_channels} channel, {cat_free.daily_min}-{cat_free.daily_max}), trade by ID"),
        Perk("birthdays", "fun", "🎂", "Birthdays",
             f"Styled announcements, a birthday role and up to {bd.MAX_EXTRA_MESSAGES} random extra messages.",
             free="Plain announcement, one message",
             premium=f"Styled look · birthday role · {bd.MAX_EXTRA_MESSAGES} extra messages",
             pitch="styled announcement, birthday role & extra messages"),
        Perk("anime", "fun", "📺", "Anime Feed",
             f"{anime_plus} follows per person, DM alerts and quiet hours for everyone in the server.",
             free=f"{anime_free} follows per person",
             premium=f"{anime_plus} follows · DM alerts · quiet hours",
             pitch=f"{anime_plus} follows (free: {anime_free}), DM alerts & quiet hours for everyone"),
        # ── AI ────────────────────────────────────────────────────────────
        Perk("ai_chat", "ai", "🤖", "AI Chat",
             "A much higher daily AI chat limit for every person in the server.",
             free=f"{ai.REPLY_CAP_NORMAL} a day per person",
             premium=f"{ai.REPLY_CAP_PREMIUM} a day per person",
             plus=f"{config.PREMIUM_PLUS_REPLY_CAP} a day per person",
             pitch=f"{ai.REPLY_CAP_PREMIUM} a day per person (free: {ai.REPLY_CAP_NORMAL})"),
        # ── Style & cards ─────────────────────────────────────────────────
        Perk("welcome_cards", "style", "🎴", "Welcome Cards",
             "Premium themes, your own background and Welcome Studio (gallery, effects, frames, saved designs).",
             free="The original theme",
             pitch="premium themes, your own background & Welcome Studio"),
        Perk("fonts", "style", "🔤", "Fonts & Designs",
             "20+ fonts, frames and emoji tags.",
             free="Starter fonts and frames"),
        Perk("level_card", "style", "🏅", "Server Level Card",
             "Every member of your server uses your custom level-up card, no Level Pass needed.",
             free="Members need their own Level Pass",
             pitch="members use your custom level-up card, no Level Pass needed"),
        Perk("goodbye", "style", "👋", "Goodbye styling",
             "Custom title, embed colour, banner, footer and avatar on your goodbye post.",
             free="Plain goodbye (text, channel, on/off)",
             in_pitch=False),
        # ── Music ─────────────────────────────────────────────────────────
        Perk("music", "music", "🎵", "Music Pro",
             "Unlimited plays, uploads and downloads.",
             free=f"{config.MUSIC_FREE_DAILY_LISTENS} plays · {config.MUSIC_FREE_DAILY_UPLOADS} uploads · "
                  f"{config.MUSIC_FREE_DAILY_DOWNLOADS} downloads a day",
             premium="Unlimited"),
        # ── Branding & extras ─────────────────────────────────────────────
        Perk("branding", "brand", "🖼️", "Bot Branding",
             "Your own bot name, avatar and banner."),
        Perk("future", "brand", "🆕", "Every future feature",
             "Free, automatically — new Premium features unlock without paying again.",
             pitch="free, automatically"),
    )


def perks_in(category_key: str) -> Tuple[Perk, ...]:
    return tuple(p for p in perks() if p.category == category_key)


def limit_rows() -> Tuple[Tuple[str, str, str, str], ...]:
    """(label, free, premium, plus) for every perk that is a plain number -- the compare table.
    Each value is read from the module that enforces it."""
    from modules import ai_features as ai
    from modules import applications as apps
    from modules import scam_vision as sv

    from modules import cat_setup as cs
    from modules.anime_feed import FREE_FOLLOW_LIMIT as anime_free
    from modules.anime_plus import PLUS_FOLLOW_LIMIT as anime_plus

    free_colours = sum(1 for c in apps.COLORS if not c[3])
    all_colours = len(apps.COLORS)
    return (
        ("AI chat per day",     str(ai.REPLY_CAP_NORMAL), str(ai.REPLY_CAP_PREMIUM), str(config.PREMIUM_PLUS_REPLY_CAP)),
        ("AI scam checks/day",  str(sv.FREE_GUILD_DAILY), str(sv.PREMIUM_GUILD_DAILY), str(config.PREMIUM_PLUS_SCAM_VISION_DAILY)),
        ("Backup scam checks",  str(sv.OPENAI_FREE_DAILY), str(sv.OPENAI_PREMIUM_DAILY), str(config.PREMIUM_PLUS_SCAM_OPENAI_DAILY)),
        ("Application forms",   str(apps.MAX_FORMS_FREE), str(apps.MAX_FORMS_PREMIUM), str(apps.MAX_FORMS_PREMIUM)),
        ("Questions per form",  str(apps.MAX_QUESTIONS_FREE), str(apps.MAX_QUESTIONS_PREMIUM), str(apps.MAX_QUESTIONS_PREMIUM)),
        ("Form colours",        str(free_colours), str(all_colours), str(all_colours)),
        ("Cat channels",        str(cs.FREE.max_channels), str(cs.PREMIUM.max_channels), str(cs.PREMIUM.max_channels)),
        ("Anime follows",       str(anime_free), str(anime_plus), str(anime_plus)),
        ("Music plays/day",     str(config.MUSIC_FREE_DAILY_LISTENS), INFINITY, INFINITY),
        ("Music uploads/day",   str(config.MUSIC_FREE_DAILY_UPLOADS), INFINITY, INFINITY),
        ("Music downloads/day", str(config.MUSIC_FREE_DAILY_DOWNLOADS), INFINITY, INFINITY),
    )


def plus_extras() -> str:
    """The one-line "what Plus adds on top of Premium" used on the plan screen and the pay card."""
    from modules import ai_features as ai
    from modules import scam_vision as sv
    return (f"AI chat {config.PREMIUM_PLUS_REPLY_CAP}/day (Premium {ai.REPLY_CAP_PREMIUM}) and "
            f"{config.PREMIUM_PLUS_SCAM_VISION_DAILY} AI scam checks a day (Premium {sv.PREMIUM_GUILD_DAILY})")


def pitch_lines() -> Tuple[str, ...]:
    """One line per pitch perk, generated here so the pitch text can't fall behind the registry."""
    out = []
    for p in perks():
        if not p.in_pitch:
            continue
        text = p.pitch or (p.blurb[:1].lower() + p.blurb[1:]).rstrip(".")
        out.append(f"{p.emoji} **{p.title}** — {text}")
    return tuple(out)


def premium_only_titles() -> Tuple[str, ...]:
    """Perks a Free server does not get at all (free == "")."""
    return tuple(p.title for p in perks() if not p.free)


def yearly_savings_pct() -> int:
    full = config.PREMIUM_FEE_USD * 12
    return round((1 - config.PREMIUM_YEARLY_FEE_USD / full) * 100) if full else 0
