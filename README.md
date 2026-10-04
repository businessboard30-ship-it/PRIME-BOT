# PRIME-BOT — Discord Edition

Production Discord bot built on a single `discord.py` application: server setup
wizards, moderation and raid protection, leveling with illustrated level cards,
economy and games, AI tools, anime discovery, a Bots Archive & Directory,
Discord bot-cloning, and an ads/marketplace.

**Status:** Live | **Platform:** Discord (gateway) | **Host:** Railway (or any
always-on process host — NOT Vercel/serverless, see note below)

> **Setup walkthrough:** a screen-recorded tutorial (creating a server, adding the
> bot and configuring it) is being published on YouTube. The link will be added
> here and as a "Watch" button on the join DM.

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

**Top.gg vote boost (optional):**
- `TOPGG_WEBHOOK_SECRET` — v1 `whs_...` secret (or the v0 Authorization string) from Top.gg → your bot → Webhooks; webhook URL is `https://<api-host>/api/topgg_webhook`
- `TOPGG_BOT_ID` — your bot's Top.gg ID; enables the "Vote for us" line on the leaderboard (or set `TOPGG_VOTE_URL` directly)
- `TOPGG_VOTE_MULTIPLIER` (default `1.5`) / `TOPGG_VOTE_HOURS` (default `12`) — vote reward; applies in every server and clone, and never stacks with a paid boost (the larger one wins)
- `TOPGG_TOKEN` — Top.gg *project token* (Dashboard → API); makes the main bot post server/shard count every `TOPGG_POST_INTERVAL_MINUTES` (default 30) and push its slash-command list once per start, minus `TOPGG_HIDE_COMMANDS` (default `admin`). Main bot only — set it on the main bot's service, not the clone supervisor

**Optional:**
- `DISCORD_DEV_GUILD_ID` — set during development for near-instant slash-command sync to one guild; leave unset for global sync (~1hr propagation)
- `DISCORD_OAUTH_CLIENT_ID` / `DISCORD_OAUTH_CLIENT_SECRET` — only needed for the Discord-login/dashboard OAuth flow (`api/discord_login_oauth.py`), separate from bot-invite OAuth
- `OWNER_GUILD_ID` / `OWNER_BROADCAST_CHANNEL_ID` — main bot's own support server broadcast target
- `YTDLP_COOKIES_B64` (or `YTDLP_COOKIES_FILE`) — base64-encoded `cookies.txt` for `/download` on sites that need a logged-in session. Must be valid base64 (a bad value logs `Failed to decode YTDLP_COOKIES_B64` at startup and downloads fall back to no cookies)

See `config.py` for the full, current list — it's the single source of
truth for every variable this project reads.

### 3. Install & run
```bash
pip install -r requirements.txt
PYTHONPATH=. python -u discord_bot/bot.py      # the bot (gateway)
PYTHONPATH=. python -u api_server.py           # optional: webhooks / OAuth API
```
`ffmpeg` and `libopus` are needed for music/voice (see `nixpacks.toml` / `railpack.json`).

### 4. Deploy to Railway
```bash
git add . && git commit -m "deploy" && git push
# railway.app/new → deploy from repo → set env vars above → deploy
# Procfile defines two processes:
#   worker: PYTHONPATH=. python -u discord_bot/bot.py   (the bot)
#   web:    PYTHONPATH=. python -u api_server.py        (webhooks / OAuth)
```

### 5. Invite the bot
Use the OAuth2 URL Generator in the Developer Portal (scopes: `bot` +
`applications.commands`), or let `discord_clone_service.build_invite_url()`
generate one programmatically for any registered clone.

---

## First run: what happens when the bot joins a server

When PRIME-BOT is added to a server it sends the owner one **combined join DM**
(re-sendable via "Remind me later") with a one-tap **Turn on** button per feature:
welcome messages, support tickets, join verification, invite tracker, downloadhub,
custom role, leveling/XP, server analytics, channel names & fonts, suggested
channels, starboard, role setup, suggestions and auto-moderation, plus Partnership
(bump) and the Honeypot trap channel. Each button applies sane defaults — no slash command required — and
only the server owner or someone with **Manage Server** can use them.

Everything the DM does is also reachable in-server:
- `/serversetup` — guided setup wizard / Server Owners Panel (welcome, goodbye &
  auto-roles, verification, automod, mod-log, leveling, tickets, protection);
  raid protection is set up with `/antiraid`
- `/start` — quickstart pointer, `/help` — every feature by category
- `/setup channels` — suggest and create commonly useful channels

---

## Project Structure

```
PRIME-BOT/
├── discord_bot/
│   ├── bot.py                    # Entry point — gateway client, setup_hook loads every cog
│   ├── clone_manager.py          # Process supervisor for Discord clone bots (own subprocess per clone)
│   ├── perm_check.py             # Permission/hierarchy checks + "needs attention" notices
│   └── cogs/                     # One cog per feature area; _views_*.py hold the UI
│       ├── _views_join_dm.py     # Combined owner join DM + "Turn on" feature buttons
│       ├── _views_shared.py      # Shared access checks (owner / Manage Server, DM-safe)
│       ├── verification.py, welcome.py, welcome_extras.py, ticket.py, invites.py
│       ├── antiraid.py, honeypot.py, automod.py, scam_shield.py, join_gate.py, quarantine.py
│       ├── leveling.py, voice_xp.py, economy.py, cards.py, heist.py, starboard.py, giveaways.py
│       ├── ai_tools.py, ai_store.py, music.py, external_tools.py, crypto_alerts.py
│       ├── archive.py, botstore.py, discover.py, submissions.py
│       ├── clone_admin.py, bot_manager.py, referrals.py, ads_marketplace.py, bump.py
│       ├── admin.py, admin_panel.py  # Owner /admin console
│       └── ... (see discord_bot/bot.py's setup_hook for the full, current list)
│
├── modules/                      # Shared business logic — no Discord-specific UI code
│   ├── server_panel*.py          # Server Owners Panel data layer (audited settings changes)
│   ├── level_card.py, godhood_cards.py, clan_cards.py, welcome_card.py   # Pillow card renderers
│   ├── archive_adapter.py, botstore_adapter.py, superbot_adapter.py
│   ├── ai_features.py, ai_store_*.py                                      # Groq + AI Store
│   └── ...
│
├── api/, api_server.py           # Request-driven endpoints (Paystack webhook, OAuth, Top.gg webhook)
├── app/, redirector/             # Web dashboard / landing pages
├── discord_clone_service.py      # Token validation + OAuth2 invite-link builder for clones
├── config.py                     # All environment variables — single source of truth
├── database.py                   # Core Postgres pool + primary schema
├── payments.py, payments_manual.py, gumroad_payments.py   # Paystack / manual / Gumroad payments
├── tier*.png, godhood_*.png, clan_*.png   # Level-up card art
├── handlers/                     # Legacy Telegram-era handlers — being phased out
├── tests/                        # unit, integration, security
├── sql/                          # STALE — do not run by hand, see Quick Start note
└── requirements.txt
```

---

## Features

The bot registers roughly 87 global slash commands (Discord's cap is 100), so new
features are folded into wizards and hubs instead of adding top-level commands.

### Server setup & community
- **Join DM wizard** and **`/serversetup`** panel — one-tap setup, see above
- **Welcome cards** (`/welcome`) with themes, avatar shapes and stickers, plus a
  **goodbye message** (channel, text, test post) and member/bot **auto-roles**
- **Join verification** (`/setupverification`) — auto-creates and positions
  Unverified/Verified roles below the bot, locks channels, posts the verify panel
- **Tickets** (`/ticket`), **suggestions**, **starboard**, **reaction roles**,
  **role setup**, **invite tracker** (`/invites`), **custom roles**, **link buttons**,
  **scheduled messages**, **auto-responders**, **mod-log** (`/modlog`), **server analytics**
- **Channel styling** (`/style`) — fancy fonts and brackets for channel names

### Protection
- **Auto-moderation** (`/automod`) — filters, banned words, guided wizard
- **Anti-raid** (`/antiraid`) — spike detection, lockdown and staff alerts
- **Honeypot** (`/honeypot`) — free trap channel that auto-actions spam bots and
  hacked accounts (premium extras: action, history window, log channel)
- **Scam shield**, **join gate**, **quarantine**, **moderation** (kick/ban/timeout)

### Leveling, economy & games
- **Leveling** (`/rank`, `/leaderboard`) with **illustrated level-up cards** across
  many tiers, level-role rewards, **clans** and chiefs, **voice XP**
- **Economy** (`/economy`, `/shop`), **trading cards** (`/card`, `/daily`),
  **Heist Wars** (`/heist`), **giveaways** (`/giveaway`), roast battles and ship

### AI & tools
- **AI chat and images** (`/aichat`, `/aiimage`) via Groq — degrades gracefully if unset
- **AI Store** (`/aistore`) — paid AI personas; sellers can connect their own key
- **Music**, `/download`, `/news`, `/convert`, `/stock`, `/crypto`, **price alerts**,
  **reverse image search**, **Media Connect** (your own Jellyfin / Plex library)
- **Anime discovery** (`/discover`, `/animecategory`) and community submissions

### Growth & monetization
- **Bots Archive** (`/archive`) — verified bot submissions with automated risk
  scoring, human review queue, voting, trending, boosts and disputes
- **Bot Directory** (`/botstore`) and **Bump network** (`/bump`, `/bumpsetup`)
- **Discord bot cloning** (`/registerclone`) — your own bot token runs as a clone
  via `clone_manager.py`, with its own invite link and premium groups
- **Referrals**, **referral giveaways**, **ads marketplace**, **premium** (Paystack,
  Selar manual payments, Gumroad), **per-server bot profile** (name/avatar/banner)
- **Top.gg** vote boost and stats posting

### Owner admin
- `/admin` console — buttons, selects and forms for clones, subscribers, payments,
  referral giveaways, coupons and bot-wide controls; the original `/admin ...`
  slash commands remain as a fallback

---

## Technology Stack

- **Language:** Python 3.13
- **Bot Framework:** discord.py 2.6+
- **Database:** PostgreSQL (asyncpg)
- **AI:** Groq API (optional)
- **Payments:** Paystack (plus Selar manual and Gumroad flows)
- **Imaging:** Pillow-rendered welcome and level-up cards
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

## Tests

```bash
pip install pytest
pytest tests/
```

---

## License

MIT

---

## Project history

Past audit reports, handoff notes and phase write-ups live in
[`docs/archive/`](docs/archive/). They're kept for reference and are not
maintained; this README and `config.py` are the source of truth.
