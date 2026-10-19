"""Cat game help text. Numbers come from the real rules so this can never drift out of date (pure, no database)."""
from __future__ import annotations

import os
from typing import List, Tuple

from modules import cat_adopt, cat_arena, cat_care, cat_life, cat_market, cat_rank, cat_wallet, cat_wishlist

FOOTER = "Just for fun: play coins only, no real money."

# One place for the Arena rules, built from the real numbers so the help can never disagree with the game.
ARENA_LONG = (
    "Tap **Arena** on /cat and turn it on. Nobody can challenge you, and you can't challenge anyone, until you do. "
    "A fight only happens when **both** players agree: you send a challenge, the other player sees the odds and taps "
    f"**Accept** or **Decline** ({cat_arena.EXPIRE_MINUTES} minutes to answer). "
    f"Wagers: {', '.join(f'{w:,}' for w in cat_arena.LADDER)} {cat_wallet.COINS}. They only go in when the challenge is accepted, "
    f"and the winner gets both minus a {cat_arena.FEE_PERCENT}% fee. Nobody ever loses a cat in these fights: the loser's cat is "
    f"😴 tired for {cat_arena.TIRED_HOURS}h (and 🩹 hurt for {cat_arena.HURT_HOURS}h after a knock-out, fighting at -10% stats). "
    f"Limits: {cat_arena.FIGHTS_PER_DAY} fights a day, {cat_arena.DAILY_NET_LOSS_LIMIT:,} {cat_wallet.COINS} lost a day, "
    f"{cat_arena.PAIR_PER_DAY} fights a day against the same player. 🪩 Practice (in Stats & skills) is free fun against pretend cats.")


# The round PRIME BOT ring from the website, shown as the small picture on the Help card.
# Served by the site (website/assets/img/bot-ring.png); override with CAT_HELP_RING_URL if the site moves.
RING_URL = os.getenv("CAT_HELP_RING_URL", "https://prime-bot-site.pages.dev/assets/img/bot-ring.png")


def sections() -> List[Tuple[str, str]]:
    return [
        ("⏰ Spawns",
         "Cats appear by themselves in the channel(s) the server owner picked with **/catsetup** (spawn hours are in UTC). "
         "Every cat shows a **Next cat** time, in your own time zone, so you know when to come back."),
        ("🐾 Adopting",
         "Tap **Adopt me!** on a cat. The first person to tap gets it, and it comes with some coins "
         f"(sometimes a {cat_wallet.DIAMOND} diamond). You can keep up to {cat_adopt.MAX_CATS} cats. "
         f"A cat nobody adopts within {cat_adopt.SPAWN_EXPIRE_HOURS} hours wanders off."),
        ("🏠 My cats",
         "Rename, release, ⭐ rank up (spend coins, up to rank "
         f"{cat_rank.MAX_RANK}), trade or gift, and 🔍 filter or search your collection. "
         f"{cat_wallet.DIAMOND} 1 diamond converts into {cat_wallet.DIAMOND_COINS:,} {cat_wallet.COINS} coins."),
        ("🍖 Care",
         f"Your cats get hungry now and then. In **My cats**, tap {cat_care.FOOD} **Feed** ({cat_care.FOOD_COST} coins). "
         f"{cat_care.DROP} **Water** ({cat_care.WATER_COST}) and a {cat_care.BED} **Bed** ({cat_care.BED_COST}, rented for "
         f"{cat_care.COMFORT_HOURS}h) make a cat less likely to get hungry. A cat that stays hungry for "
         f"{cat_care.LEAVE_AFTER_HOURS} hours may wander off. It's safe and comes back in a spawn, so someone can adopt it again."),
        ("🌸 Cat Life",
         f"Tap **Cat Life** on /cat. 💰 Fed cats earn coins by the hour (pick them up at least every {cat_life.HOURS_CAP}h). "
         f"🏡 A cozy home costs {cat_life.COINS} {cat_life.RENT_BASE} + {cat_life.RENT_PER_CAT} per cat a day; without it your cats earn only "
         f"{cat_life.percent(cat_life.HOME_OFF_FACTOR)}. 🌷 Take a cat for a walk (costs coins, brings some back). "
         f"🎀 Go on a pretend kitty heist with your cat: win big or lose the fee ({cat_life.HEISTS_PER_DAY} a day). "
         f"🎰 Coin flip and lucky spin, with a daily loss limit of {cat_life.COINS} {cat_life.DAILY_LOSS_LIMIT:,}. "
         "Cats that are out can't be sold, traded or released until they're home. Heists only ever target pretend places, never real players."),
        ("⚔️ Arena",
         ARENA_LONG),
        ("🏷️ Market",
         f"Sell a cat for coins ({cat_market.MIN_PRICE:,} to {cat_market.MAX_PRICE:,}, up to "
         f"{cat_market.MAX_OPEN_PER_SELLER} for sale at once). Tap **Market** to browse and buy. "
         "A cat that is for sale can't be traded, ranked up, renamed or released until you take it off."),
        ("🤝 Trading",
         "In **My cats**, tap **Trade / gift**: pick a member, or use a trade ID. "
         "Nothing happens until the other person accepts."),
        ("🎁 Every day",
         "Use **/cat** for the full panel: 💖 Daily care (streak rewards), 🎯 Daily quests, 🏅 Badges, "
         f"✨ Cosmetics, 🔎 Wishlist (up to {cat_wishlist.MAX_OPEN} wants) and 📖 your profile."),
        ("📖 Profile",
         "Your profile shows your Discord picture, bio, collection size, rarest cat, showcase and badges. Edit your bio, "
         "📌 pin cats to show off, 📣 share it, or 🙈 hide it. Look at anyone's with **/cat profile:@player**."),
        ("📣 Showing off",
         "Tap **Share** on your profile, badges or care streak to post it in the channel. Anyone can look at a profile with "
         "`/cat profile:@player` (hidden profiles stay hidden). In **My profile** you can edit your bio, pin up to 3 cats and "
         "hide or show it. A wishlist post can also go to the channel, and the leaderboard has **Post to channel**. "
         "Server managers choose in `/catsetup` whether the bot posts all, milestones only, or nothing."),
        ("🏆 Leaderboard",
         "Top collectors and richest players, across all servers. Tap **Leaderboard** on /cat."),
        ("🛠️ For server owners",
         "**/catsetup** (needs Manage Server): pick the channel, the pace and the spawn hours, then turn it on. "
         "The bot needs Send Messages and Embed Links there. Premium servers get more channels and more cats a day."),
    ]


def build_text() -> str:
    return "\n\n".join(f"**{title}**\n{body}" for title, body in sections())


# ── Short version: one small card per topic, switched with a menu ─────────────────────────────────────────────
# Same real numbers as sections() above, but a few words each so a topic fits on one screen without scrolling.

def topics() -> List[Tuple[str, str, str, str]]:
    """[(key, emoji, label, short text)], in menu order. Labels are 1-2 words (menu options, max 100 chars)."""
    return [
        ("spawns", "⏰", "Spawns",
         "Cats show up by themselves in the channel(s) picked with **/catsetup**.\n"
         "Each cat shows a **Next cat** time in your time zone."),
        ("adopt", "🐾", "Adopt",
         f"Tap **Adopt me!** First tap wins and gets some coins (sometimes a {cat_wallet.DIAMOND}).\n"
         f"Keep up to **{cat_adopt.MAX_CATS}** cats. Nobody adopts one in {cat_adopt.SPAWN_EXPIRE_HOURS}h? It wanders off."),
        ("mine", "🏠", "My cats",
         f"Rename, release, trade, gift, search, and ⭐ rank up (max {cat_rank.MAX_RANK}).\n"
         f"{cat_wallet.DIAMOND} 1 diamond = {cat_wallet.DIAMOND_COINS:,} {cat_wallet.COINS} coins."),
        ("care", "🍖", "Care",
         f"In **My cats**: {cat_care.FOOD} Feed ({cat_care.FOOD_COST}), {cat_care.DROP} Water ({cat_care.WATER_COST}), "
         f"{cat_care.BED} Bed ({cat_care.BED_COST}, {cat_care.COMFORT_HOURS}h).\n"
         f"Hungry for {cat_care.LEAVE_AFTER_HOURS}h? The cat may wander off. It comes back in a spawn."),
        ("life", "🌸", "Cat Life",
         f"Tap **Cat Life** on /cat. Fed cats earn {cat_life.COINS} coins by the hour (collect within {cat_life.HOURS_CAP}h).\n"
         f"A cozy home keeps earnings up. Walks, heists, coin flip and spin live here too."),
        ("arena", "⚔️", "Arena",
         f"Turn **Arena** on, then challenge a player. They see the odds and must **Accept** first ({cat_arena.EXPIRE_MINUTES} min).\\n"
         f"Wagers {cat_arena.LADDER[0]:,} to {cat_arena.LADDER[-1]:,} {cat_wallet.COINS}, {cat_arena.FEE_PERCENT}% fee. Nobody loses a cat: the loser is just tired."),
        ("market", "🏷️", "Market",
         f"Sell a cat for {cat_market.MIN_PRICE:,} to {cat_market.MAX_PRICE:,} coins "
         f"({cat_market.MAX_OPEN_PER_SELLER} for sale at once). Tap **Market** to browse and buy."),
        ("trade", "🤝", "Trade",
         "In **My cats** tap **Trade / gift**. Pick a member or use a trade ID.\n"
         "Nothing moves until they accept."),
        ("daily", "🎁", "Daily",
         f"**/cat** has 💖 daily care, 🎯 quests, 🏅 badges, ✨ cosmetics and a 🔎 wishlist (up to {cat_wishlist.MAX_OPEN})."),
        ("profile", "📖", "Profile",
         "Shows your picture, bio, cats and badges. Edit it, 📌 pin up to 3 cats, 📣 share or 🙈 hide it.\n"
         "Look up anyone with `/cat profile:@player`."),
        ("board", "🏆", "Leaderboard",
         "Top collectors and richest players across all servers. Tap **Leaderboard** on /cat."),
        ("owners", "🛠️", "Server owners",
         "**/catsetup** (needs Manage Server): pick the channel, pace and spawn hours, then turn it on.\n"
         "The bot needs Send Messages and Embed Links there."),
    ]


def overview_text() -> str:
    """The first screen: three lines, then 'pick a topic'."""
    return ("🐾 Tap **Adopt me!** on a cat to make it yours.\n"
            "🏠 **My cats** is your collection. 🏷️ **Market** is where cats are bought and sold.\n\n"
            "Pick a topic below for the short version.")


def topic_text(key: str) -> str:
    for k, _emoji, _label, text in topics():
        if k == key:
            return text
    return overview_text()
