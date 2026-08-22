# PRIME-BOT — Discord Edition

Production Discord bot: anime discovery, AI tools, moderation, leveling/economy,
a self-service Bots Archive & Directory, Discord bot-cloning, and an ads/AI
marketplace — all wired into a single `discord.py` application.

**Status:** Live | **Platform:** Discord (gateway) | **Host:** Railway (or any
always-on process host — NOT Vercel/serverless, see note below)

---

## Why not Vercel / serverless

Discord bots need a persistent WebSocket (gateway) connection — there's no
incoming HTTP request to "wake up" for, unlike Telegram's webhook model.
That means this bot must run as a **long-lived process**: a Railway service,
a VPS with systemd, or a Docker container with a restart policy. It will
**not** work on Vercel or any request-driven serverless platform.

(The `api/` folder still contains a few genuinely request-driven pieces —
the Paystack webhook, OAuth callback routes, etc. — which *can* run
serverless. The bot's gateway connection itself cannot.)

---

## Quick Start

### 1. Database (5 min)
```bash
# Supabase, Neon, or Railway Postgres — any managed Postgres works.
# Tables are created automatically on first boot (database.py and
# modules/archive_adapter.py each run their own CREATE TABLE IF NOT
# EXISTS statements) — no manual migration needed.
#
# NOTE: sql/schema.sql and sql/supabase_migration.sql are STALE partial
# snapshots from early in development. Do not run them by hand — they
# will leave you with an incomplete schema. Treat the Python modules'
# own table-creation code as the source of truth.
```

### 2. Environment variables (5 min)
Set these in Railway (or your host)'s environment settings — there's no
`.env.example` checked in, so use the list below.

**Required:**
- `DISCORD_BOT_TOKEN` — from the Developer Portal → Bot tab
- `DATABASE_URL` — Postgres connection string
- `DISCORD_CLONE_ADMIN_IDS` — comma-separated Discord user IDs with owner/admin powers (archive review, clone admin commands, etc.)

**Recommended:**
- `GROQ_API_KEY` — enables AI features (recommendations, scam-risk classifier, category suggestions). Everything degrades gracefully without it.
- `ENCRYPTION_KEY` — used by `utils/crypto.py` to encrypt stored clone bot tokens
- `PAYSTACK_SECRET_KEY` / `PAYSTACK_PUBLIC_KEY` — payments (premium, clone registration fees, boosts)
- `PUBLIC_BASE_URL` — base URL of your deployed API server, used to build OAuth redirect/webhook URLs

**Optional:**
- `DISCORD_DEV_GUILD_ID` — set during development for near-instant slash-command sync to one guild; leave unset for global sync (~1hr propagation)
- `DISCORD_OAUTH_CLIENT_ID` / `DISCORD_OAUTH_CLIENT_SECRET` — only needed for the Discord-login/dashboard OAuth flow (`api/discover_oauth_join.py`), separate from bot-invite OAuth
- `OWNER_GUILD_ID` / `OWNER_BROADCAST_CHANNEL_ID` — main bot's own support server broadcast target
- `TOPGG_WEBHOOK_AUTH` — top.gg vote webhook verification

See `config.py` for the full, current list — it's the single source of
truth for every variable this project reads.

### 3. Install & run
```bash
pip install -r requirements.txt
python -m discord_bot.bot
```

### 4. Deploy to Railway
```bash
git add . && git commit -m "deploy" && git push
# railway.app/new → deploy from repo → set env vars above → deploy
# Start command: python -m discord_bot.bot
```

### 5. Invite the bot
Use the OAuth2 URL Generator in the Developer Portal (scopes: `bot` +
`applications.commands`), or let `discord_clone_service.build_invite_url()`
generate one programmatically for any registered clone.

---

## Project Structure

```
PRIME-BOT/
├── discord_bot/
│   ├── bot.py                    # Entry point — gateway client, setup_hook loads every cog
│   ├── clone_manager.py          # Process supervisor for Discord clone bots (own subprocess per clone)
│   ├── i18n_helpers.py
│   └── cogs/                     # One cog per feature area — see Features below
│       ├── archive.py            # /archive — Bots Archive submission/review/voting/boosts
│       ├── archive_automation.py # Background loops: review expiry, dead-bot sweep, trending repost, etc.
│       ├── botstore.py           # /botstore — member-submitted bot directory
│       ├── clone_admin.py        # /registerclone, /myclones — Discord bot-cloning growth loop
│       ├── moderation.py / automod.py / reaction_roles.py
│       ├── leveling.py / economy.py / welcome.py
│       ├── ai_tools.py / ai_store.py / external_tools.py / crypto_alerts.py
│       ├── discover.py           # Anime discovery (trending/latest/ongoing/seasonal)
│       ├── ads_marketplace.py / referrals.py / bot_manager.py
│       └── ... (see discord_bot/bot.py's setup_hook for the full, current list)
│
├── modules/                      # Shared business logic — no Discord/Telegram-specific code
│   ├── archive_adapter.py        # Bots Archive DB layer, risk scoring, Discord RPC lookups
│   ├── botstore_adapter.py
│   ├── superbot_adapter.py       # Tier/premium checks
│   ├── ai_features.py            # Groq client wrapper
│   └── ...
│
├── handlers/                     # Legacy Telegram-era handlers — being phased out in favor of discord_bot/cogs/
├── api/                          # Request-driven endpoints (Paystack webhook, OAuth callbacks)
├── discord_clone_service.py      # Token validation + OAuth2 invite-link builder for clones
├── config.py                     # All environment variables — single source of truth
├── database.py                   # Core Postgres pool + primary schema
├── payments.py                   # Paystack integration
├── sql/                          # STALE — do not run by hand, see Quick Start note
└── requirements.txt
```

---

## Features

### Core / Community
- **Anime Discovery** — trending, latest, ongoing, seasonal, movies (`/discover`)
- **Bots Archive** (`/archive`) — Discord-application-verified bot submissions with automated risk
  scoring, human review queue for anything ambiguous or NSFW-flagged, voting, trending, boosts,
  dispute handling, and background automation (auto-expire stale reviews, dead-bot delisting,
  duplicate-card cleanup, webhook retry queue)
- **Bot Directory** (`/botstore`) — lighter-weight member-submitted bot listings
- **Moderation** — kick/ban/timeout, automod, reaction roles, welcome messages
- **Leveling & Economy** — XP, levels, currency

### AI
- AI-powered recommendations, summaries, and tools (`/ai`) — via Groq, degrades gracefully if unset
- AI-assisted risk/category classification for archive submissions

### Growth & Monetization
- **Discord Bot Cloning** — register your own bot token (`/registerclone`), gets its own
  always-on gateway process via `clone_manager.py`, own OAuth2 invite link, own premium groups
- **Referrals**, **Ads Marketplace**, **Premium subscriptions** (Paystack)

### Admin
- `/archive pending`, `/archive resolve` — manual review queue
- Clone management, broadcast tools, analytics

---

## Technology Stack

- **Language:** Python 3.13
- **Bot Framework:** discord.py 2.4+
- **Database:** PostgreSQL (asyncpg)
- **AI:** Groq API (optional)
- **Payments:** Paystack
- **Hosting:** Railway (or any always-on host — see note above)
- **APIs:** AniList (GraphQL) + Jikan (REST) for anime data; Discord's public RPC endpoint for bot-application lookups

---

## Database Schema

Tables are created automatically on first boot — `database.py`'s `init_db()`
handles the core schema, and each feature module (e.g.
`modules/archive_adapter.py`) creates and migrates its own tables the same
way. Do not run anything under `sql/` by hand; those files are stale
partial snapshots.

---

## Deployment Options

### Option 1: Railway (Recommended)
- Always-on process — required for Discord's gateway connection
- Free tier available, simple `git push` deploy

### Option 2: VPS + systemd / Docker
- Full control, same "always-on" requirement applies
- Use `discord_bot/clone_manager.py` as its own separate long-running service if you're running clone bots

### Option 3: Local (development)
```bash
python -m discord_bot.bot
```

---

## License

MIT
