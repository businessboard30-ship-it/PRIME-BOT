# path: config.py

import os
from dotenv import load_dotenv

load_dotenv()

# Bot Configuration
BOT_TOKEN = os.getenv("SINOBANED2_BOT_TOKEN", "your_token_here")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Shared secret that protects the cron-triggered endpoints (api/cron_autopost.py,
# api/cron_broadcast.py) from being called by anyone but your scheduler. Sent by
# the caller either as header "Authorization: Bearer <CRON_SECRET>" (Vercel Cron
# does this automatically when this env var is set) or as query param ?secret=.
CRON_SECRET = os.getenv("CRON_SECRET", "")

# Temporary data directory (only for ephemeral cache, NOT production data)
# All persistent data MUST go to DATABASE_URL (Postgres)
DATA_DIR = os.getenv("DATA_DIR", "/tmp/data")

# Database — must be a Postgres connection string (Supabase, Neon, or Railway Postgres).
# SQLite is NOT usable here: this bot runs in a serverless/ephemeral container with
# no writable, persistent disk.
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it to your Postgres connection string "
        "(e.g. postgresql://user:password@host:5432/dbname)."
    )

# API Configuration
ANILIST_ENDPOINT = "https://graphql.anilist.co"
JIKAN_ENDPOINT = "https://api.jikan.moe/v4"

# Payment Configuration
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
CLONE_BOT_FEE_GHS = 50  # 50 GHS in pesewas = 5000

# --- Shared AI Chat / Download paywall ---------------------------------------
# Both "🤖 AI Chat" and "⬇️ Download" give every user UTILITY_FREE_USES free
# goes each (tracked separately per feature), then ONE 25 GHS / 2-month
# subscription unlocks BOTH features together (handlers/utility_paywall.py).
UTILITY_FREE_USES = 2
UTILITY_SUB_FEE_GHS = 25
UTILITY_SUB_DAYS = 60  # ~2 months

# --- yt-dlp media download (modules/external_apis.py) ------------------
# Optional path to a Netscape-format cookies.txt file. Several extractors
# (YouTube especially) increasingly require a signed-in session to avoid
# "Sign in to confirm you're not a bot" / age-restriction errors — without
# this, a large fraction of otherwise-valid links will fail. Leave unset to
# download cookie-free (works for many non-YouTube sources, and for some
# YouTube videos, but expect intermittent bot-check failures on YouTube).
#
# Two ways to provide it:
#   1. YTDLP_COOKIES_FILE — direct path to a cookies.txt already present on
#      disk. Only useful on a host with a persistent/mounted filesystem;
#      Railway's default filesystem is ephemeral and wiped on every
#      redeploy, so this alone won't survive there.
#   2. YTDLP_COOKIES_B64 — the cookies.txt file, base64-encoded, stored
#      directly as an env var (Railway env vars persist across deploys even
#      though the filesystem doesn't). If set, it's decoded to disk once at
#      startup below and YTDLP_COOKIES_FILE is set automatically to that
#      path — you don't need to set both.
#
# Export cookies.txt from a real, logged-in browser session (e.g. via a
# "Get cookies.txt LOCALLY" browser extension). Treat this file/value like a
# password — it's a live login session for whatever account exported it —
# and never commit it to source control.
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "")

_ytdlp_cookies_b64 = os.getenv("YTDLP_COOKIES_B64", "")
if _ytdlp_cookies_b64 and not YTDLP_COOKIES_FILE:
    import base64

    _cookies_decode_path = os.path.join(DATA_DIR, "ytdlp_cookies.txt")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _decoded = base64.b64decode(_ytdlp_cookies_b64, validate=True)
        with open(_cookies_decode_path, "wb") as _f:
            _f.write(_decoded)
        YTDLP_COOKIES_FILE = _cookies_decode_path
        # Confirms on every boot whether the cookies file actually made it
        # to disk — there was previously NO success-path log here, so a
        # silently-broken decode (bad paste, wrong var, stripped padding)
        # looked identical to "everything's fine" in the deploy logs.
        # Printed (not logger.info) since this module runs at import time,
        # before discord_bot's logging.basicConfig has configured handlers.
        print(f"[config] YTDLP_COOKIES_B64 decoded OK -> {_cookies_decode_path} ({len(_decoded)} bytes)")
    except Exception as _e:
        # Don't crash bot startup over this — download just falls back to
        # cookie-free.
        print(f"[config] Failed to decode YTDLP_COOKIES_B64: {_e}")
elif _ytdlp_cookies_b64 and YTDLP_COOKIES_FILE:
    print("[config] Both YTDLP_COOKIES_FILE and YTDLP_COOKIES_B64 are set — using YTDLP_COOKIES_FILE as-is, B64 ignored.")
elif not _ytdlp_cookies_b64 and not YTDLP_COOKIES_FILE:
    print("[config] No YTDLP_COOKIES_FILE / YTDLP_COOKIES_B64 set — yt-dlp will run cookie-free (expect YouTube bot-check failures).")


# Discord premium groups are configured per-guild via /premium admin
# commands and stored in the database (discord_premium_groups table) —
# nothing here needs a fixed fee/invite-link/chat-id env var anymore.

# --- AI Store (discord_bot/cogs/ai_store.py) ---------------------------------
# Buyers spend credits (bought with GHS via Paystack) to chat with Claude,
# GPT, or Gemini — powered by the PLATFORM'S OWN API keys (ANTHROPIC_API_KEY /
# OPENAI_API_KEY / GEMINI_API_KEY env vars), never a buyer's or seller's
# personal subscription. Sellers list "personas" (name + system prompt) for
# visibility/placement; the underlying AI calls always run on the platform's
# key. No revenue share — sellers get exposure/traffic, not a cut of spend.
AI_STORE_CREDIT_RATE_PER_GHS = 20  # buyers get 20 credits per 1 GHS spent
AI_STORE_FEATURED_FEE_GHS = 30  # 30-day featured placement boost
AI_STORE_TOP_FEE_GHS = 80  # 30-day top-of-store placement boost
AI_STORE_MIN_TOPUP_GHS = 5

# --- Discord port (discord_bot/) ----------------------------------------------
# Reuses database.py/payments.py as-is. Unlike the Telegram bot's single
# global Premium Group, the Discord port supports any number of
# independently-priced premium groups per guild — see database.py's
# discord_premium_groups table and discord_bot/cogs/premium.py's
# /createpremium — that's why has_paid()/log_payment() calls from
# discord_bot/ always pass guild_id + group_id explicitly, unlike the
# Telegram call sites above.
#
# It also supports clone owners running their own separate Discord bot
# (their own token, their own gateway process, their own premium groups) —
# see discord_clone_service.py, discord_bot/clone_manager.py, and
# discord_bot/cogs/clone_admin.py's /registerclone.
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")

# Optional: restrict slash-command sync to one guild for instant propagation
# during development. Leave blank for global sync (can take up to 1hr to
# propagate on Discord's side, but works across every guild the bot is in).
DISCORD_DEV_GUILD_ID = int(os.getenv("DISCORD_DEV_GUILD_ID", "0") or "0")

# Base URL of the Next.js site (app/ dir) this repo also deploys — used to
# build the /automod dashboard link. Defaults to the marketing site's own
# domain convention; override if the dashboard is deployed separately.
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "https://yourbot.example.com")

# --- Discover Players: category cap upgrade tiers -----------------------
# (member_cap_from, member_cap_to, price_usd) — checked in order, so the
# first tier whose member_cap_from matches the category's current cap is
# the one offered. USD is the base price; utils/currency.py converts to
# the payer's currency (from /currency set, or a Discord-locale guess)
# live at checkout time via convert_from_usd — no more hardcoded GHS here.
DISCOVER_CAP_TIERS = [
    (15, 65, 5),
    (65, 200, 5),
]

# --- Discover Players: Discord OAuth2 (click-to-join invite links) ------
# Separate application registration from the bot's own token — this is a
# standard OAuth2 "Authorization Code" web flow (identify scope only, no
# bot scope), used solely to learn who clicked an invite link so
# api/discover_oauth_join.py can join them server-side. Get these from the
# same Discord Developer Portal application as the bot, under OAuth2.
DISCORD_OAUTH_CLIENT_ID = os.getenv("DISCORD_OAUTH_CLIENT_ID", "")
DISCORD_OAUTH_CLIENT_SECRET = os.getenv("DISCORD_OAUTH_CLIENT_SECRET", "")
# Must exactly match a Redirect URI configured in the Developer Portal.
# api/discover_oauth_join.py is served by api_server.py (PUBLIC_BASE_URL's
# domain), NOT the Next.js dashboard site (DASHBOARD_BASE_URL) — those are
# two different Railway services/domains in this deployment.
DISCORD_OAUTH_REDIRECT_URI = os.getenv(
    "DISCORD_OAUTH_REDIRECT_URI",
    f"{os.getenv('PUBLIC_BASE_URL', '').rstrip('/')}/api/discover_oauth_join"
)

# Separate Redirect URI for /bump bot's ownership-verification flow (see
# api/bump_oauth.py). Discord requires each redirect URI used by an OAuth
# app to be registered individually in the Developer Portal — this one
# must be added there alongside DISCORD_OAUTH_REDIRECT_URI above, same
# application, same client id/secret.
BUMP_OAUTH_REDIRECT_URI = os.getenv(
    "BUMP_OAUTH_REDIRECT_URI",
    f"{os.getenv('PUBLIC_BASE_URL', '').rstrip('/')}/api/bump_oauth"
)


# --- Discord clone registration fee ------------------------------------------
# /registerclone (discord_bot/cogs/clone_admin.py) charges this fee before a
# submitted token is turned into a running clone — mirrors CLONE_BOT_FEE_GHS
# above for the Telegram flow, kept separate since the two aren't required to
# move together. Every DISCORD_CLONE_FREE_EVERY_NTH clone an owner registers
# is free (e.g. 3 -> their 3rd, 6th, 9th... clone skips payment); set to 0 to
# disable the free-clone perk entirely.
DISCORD_CLONE_FEE_GHS = 101

# Media Connect subscription (Jellyfin/Plex/Google Drive movie-search
# feature) — priced at $2/month. GHS conversion is approximate and drifts
# with FX rates; check current USD->GHS rate periodically and adjust.
MEDIA_CONNECT_FEE_GHS = 30
DISCORD_CLONE_FREE_EVERY_NTH = 3

# Premium welcome-card pack (discord_bot/views_card_pack.py, welcome.py's
# /welcome buypack): one-time, whole-guild unlock for the extra template
# themes in modules/welcome_card.py's PREMIUM_THEMES (any admin can pay,
# unlocks it for every future join, not per-user).
# USD is the base price — utils/currency.py converts to the payer's
# currency (from /currency set, or a Discord-locale guess) live at
# checkout time via convert_from_usd/usd_to_minor_units, same pattern as
# config.DISCOVER_CAP_TIERS. No more hardcoded GHS here.
WELCOME_CARD_PACK_FEE_USD = 5

# Comma-separated Discord user IDs that bypass the /registerclone payment gate
# entirely, same as ADMIN_ID does for the Telegram flow's owner bypass. The
# main bot's ADMIN_ID is NOT automatically included here — Telegram and
# Discord user IDs are different ID spaces, so add yours explicitly if you
# want the bypass on Discord too.
DISCORD_CLONE_ADMIN_IDS = {
    int(uid) for uid in os.getenv("DISCORD_CLONE_ADMIN_IDS", "").split(",") if uid.strip().isdigit()
}

# Who's allowed to run /ownerbroadcast (DM every user across the main bot
# and every Discord clone). Deliberately separate from DISCORD_CLONE_ADMIN_IDS
# even though it'll usually be the same person(s) — that set is about who
# skips the clone registration fee, this one is about who can mass-DM every
# user of every clone, and a deployment may want those to differ.
DISCORD_OWNER_BROADCAST_IDS = {
    int(uid) for uid in os.getenv("DISCORD_OWNER_BROADCAST_IDS", "").split(",") if uid.strip().isdigit()
} or DISCORD_CLONE_ADMIN_IDS

# How the owner broadcast signs itself, e.g. "Announcement from PrimeBot HQ".
# Shown as a header line above the message body in every DM sent by
# /ownerbroadcast (see discord_bot/cogs/clone_admin.py and
# api/cron_discord_owner_broadcast.py).
DISCORD_OWNER_BRAND_NAME = os.getenv("DISCORD_OWNER_BRAND_NAME", "the bot owner")

# Invite link to your own support server, shown as a link button on the
# owner join DM wizard (discord_bot/cogs/_views_join_dm.py). Empty string
# means the button is simply omitted rather than sent broken.
DISCORD_SUPPORT_SERVER_INVITE = os.getenv("DISCORD_SUPPORT_SERVER_INVITE", "https://discord.gg/DYfajXrP9B")


# --- Owner-server autopost (feature broadcast) auto-enable -------------------
# Optional: your OWN server's guild + channel IDs. If both are set, the main
# bot auto-enables /autopost in this exact server/channel on every startup —
# no need to manually run /autopost setup there. Every OTHER server the bot
# is in still requires an admin to opt in via /autopost setup, same as
# before. Does nothing on clone processes (clone_id is not None) — a clone
# has its own separate guild(s) and owner, so this only applies to the main
# bot. Leave both unset (0) to skip auto-enable entirely.
OWNER_GUILD_ID = int(os.getenv("OWNER_GUILD_ID", "0") or "0")
OWNER_BROADCAST_CHANNEL_ID = int(os.getenv("OWNER_BROADCAST_CHANNEL_ID", "0") or "0")
# How often the owner server's auto-enabled autopost fires, in hours.
OWNER_BROADCAST_INTERVAL_HOURS = int(os.getenv("OWNER_BROADCAST_INTERVAL_HOURS", "6") or "6")

# --- Vote-bonus webhook (economy.py /vote, spec §4 open question #1) --------
# Real vote verification from top.gg / discordbotlist.com: both send a
# server-to-server POST when someone votes for a bot, carrying an
# `Authorization` header that must match a secret YOU configure on the
# listing site's webhook settings page. Set the same value here. Leave
# blank and the webhook endpoint (api/vote_webhook.py) will reject every
# request with 401 — /vote in economy.py keeps working as an honor-system
# command either way, this only controls the extra verified path.
TOPGG_WEBHOOK_AUTH = os.getenv("TOPGG_WEBHOOK_AUTH", "")

# The main bot's own Discord user/application ID (NOT the bot token) —
# needed so the vote webhook can tell "this vote was for the main bot"
# apart from "this vote was for clone N", since vote payloads identify the
# bot that was voted for by its Discord user ID, not by our internal
# clone_id. Find it under Discord Developer Portal -> your app -> General
# Information -> Application ID. Clones are resolved automatically via
# discord_cloned_bots.bot_user_id, no config needed per clone.
DISCORD_BOT_USER_ID = int(os.getenv("DISCORD_BOT_USER_ID", "0") or "0")

# --- Clone monetization gate --------------------------------------------------
# A clone owner can (a) connect their own Paystack/Stripe key instead of
# routing through the main bot's account, and (b) set their own price for
# every paywalled feature their clone runs — but both are gated behind this
# recurring activation fee (handlers/clone_bot.py's "💰 Monetization" menu).
# While inactive, a clone runs on registry-default pricing and the main
# bot's gateway account only. Auto-reverts on lapse (see
# database.py's expire_monetization_subscriptions(), run by
# api/cron_expire_monetization.py).
CLONE_MONETIZATION_FEE_GHS = 20
CLONE_MONETIZATION_DAYS = 30

# --- Yandex direct-search subscription --------------------------------------
# Reverse-search previews (thumbnails, source-link unlock) stay pay-per-use
# via IMAGE_SEARCH_FEE_GHS (handlers/image_search_handler.py). Jumping
# straight to Yandex's own reverse-image-search results page with the
# image pre-loaded is a separate, recurring perk gated behind its own
# monthly fee.
IMAGE_SEARCH_YANDEX_FEE_GHS = 20
IMAGE_SEARCH_YANDEX_DAYS = 30

# Registry of every price a clone owner can override once their
# monetization subscription is active. key -> {label, default GHS}.
# database.py's get_clone_price()/get_clone_prices() resolve against this,
# and handlers/clone_bot.py's price-editing menu is generated from it — add
# a new paywalled feature here and it's automatically editable, no other
# wiring needed beyond having that feature's handler call get_clone_price().
#
# NOTE: superbot tier pricing (Pro/Elite) and botstore listing pricing
# (Featured/Premium) are intentionally NOT in this registry yet. Their
# current grants (modules/superbot_adapter.set_user_tier,
# db.set_premium_tier) aren't clone-scoped at all — a user's Pro tier on the
# main bot currently also shows as Pro on every clone. Giving clone owners
# their own price for those without first fixing that scoping bug would let
# a clone owner sell a tier whose access the main bot (or another clone)
# secretly shares. That's a separate fix — flag before extending this
# registry to cover them.
PRICE_REGISTRY = {
    "ai_subscription":     {"label": "AI Chat/Image subscription (per month)",     "default": 10},
    "image_search_unlock": {"label": "Image search source-link unlock",           "default": 10},
    "premium_group_fee":   {"label": "Premium group join fee",                    "default": 20},
    "utility_sub_fee":     {"label": "AI Chat + Download subscription (2 months)", "default": 25},
}

# --- Real clone-bot system (Part 3 of the master brief) ---------------------
# Rollback flag (3.5): keep the OLD fake-token behavior available behind this
# flag during rollout. Default is OFF (real system) once this ships — flip to
# "false" instantly if the multi-tenant router ever misroutes a message.
CLONE_BOT_REAL_ENABLED = os.getenv("CLONE_BOT_REAL_ENABLED", "true").lower() == "true"

# The public HTTPS base URL this app is deployed at (e.g. your Vercel domain,
# no trailing slash). Required to register per-clone webhooks
# (https://<PUBLIC_BASE_URL>/api/bot?clone_id=N). Clone creation fails loudly
# if this isn't set, rather than silently registering a broken webhook URL.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Max number of per-clone Application instances kept warm in memory at once
# (Part 3.1 "Per-clone Application instances" — bounded LRU, not unbounded).
CLONE_APP_CACHE_SIZE = int(os.getenv("CLONE_APP_CACHE_SIZE", "20"))

# Username (no @) of the MAIN bot, e.g. "AnimeCrunchBot". Used so every clone
# carries a visible trace back to the main bot — a "Powered by" line plus a
# deep-link button that lets clone users jump to the main bot and start their
# own clone (growth loop). Leave unset to hide this entirely.
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME", "")

# Features
MAX_BUTTONS_PER_ROW = 2
PAGINATION_SIZE = 5
RATE_LIMIT_SEARCHES = 10
RATE_LIMIT_SUBMISSIONS = 5

# Emoji Color Codes for UI
EMOJI_COLORS = {
    "trending": "🔥",      # Red/Hot
    "latest": "✨",        # Sparkle/New
    "ongoing": "🔄",       # Cycle/Ongoing
    "season": "📅",        # Calendar
    "movies": "🎬",        # Movie
    "search": "🔍",        # Search
    "submit": "📤",        # Upload
    "admin": "⚙️",         # Settings
    "clone": "🤖",         # Robot
    "categories": "📚",    # Books/Library
    "success": "✅",       # Check
    "error": "❌",         # Cross
    "loading": "⏳",       # Hourglass
    "back": "⬅️",          # Back
    "next": "➡️",          # Next
}

# Animation frames
LOADING_ANIMATION = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Messages
MESSAGES = {
    "welcome": "Welcome to the Anime & Movies Bot! Choose what you want to explore.",
    "trending_title": "Trending Anime Right Now",
    "latest_title": "Latest Anime Releases",
    "ongoing_title": "Ongoing Series",
    "season_title": "This Season's Anime",
    "movies_title": "Anime Movies",
    "no_results": "No results found. Try another search.",
    "submission_received": "Thank you! Your submission has been received and is under review.",
    "clone_prompt": "Clone this bot for 50 GHS and customize it for yourself!",
    "payment_success": "Payment successful! Setting up your new bot...",
}
