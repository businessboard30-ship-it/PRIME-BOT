"""Cat game help text. Numbers come from the real rules so this can never drift out of date (pure, no database)."""
from __future__ import annotations

from typing import List, Tuple

from modules import cat_adopt, cat_market, cat_rank, cat_wallet, cat_wishlist

FOOTER = "Just for fun: play coins only, no real money."


def sections() -> List[Tuple[str, str]]:
    return [
        ("🐾 Adopting",
         "Tap **Adopt me!** on a cat. The first person to tap gets it, and it comes with some coins "
         f"(sometimes a {cat_wallet.DIAMOND} diamond). You can keep up to {cat_adopt.MAX_CATS} cats."),
        ("🏠 My cats",
         "Rename, release, ⭐ rank up (spend coins, up to rank "
         f"{cat_rank.MAX_RANK}), trade or gift, and 🔍 filter or search your collection. "
         f"{cat_wallet.DIAMOND} 1 diamond converts into {cat_wallet.DIAMOND_COINS:,} {cat_wallet.COINS} coins."),
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
        ("📣 Showing off",
         "Tap **Share** on your profile, badges or care streak to post it in the channel. Anyone can look at a profile with "
         "`/cat profile:@player` (hidden profiles stay hidden). In **My profile** you can edit your bio, pin up to 3 cats and "
         "hide or show it. A wishlist post can also go to the channel, and the leaderboard has **Post to channel**. "
         "Server managers choose in `/catsetup` whether the bot posts all, milestones only, or nothing."),
    ]


def build_text() -> str:
    return "\n\n".join(f"**{title}**\n{body}" for title, body in sections())
