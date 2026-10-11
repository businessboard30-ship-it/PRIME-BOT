"""Cat game help text. Numbers come from the real rules so this can never drift out of date (pure, no database)."""
from __future__ import annotations

from typing import List, Tuple

from modules import cat_adopt, cat_care, cat_market, cat_rank, cat_wallet, cat_wishlist

FOOTER = "Just for fun: play coins only, no real money."


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
