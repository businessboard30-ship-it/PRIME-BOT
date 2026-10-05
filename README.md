# PRIME-BOT — Discord Edition
Production Discord bot: anime discovery, AI tools, moderation, leveling/economy,
a self-service Bots Archive & Directory, Discord bot-cloning, and an ads/A
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

**Top.gg vote boost (optional):**
- `TOPGG_WEBHOOK_SECRET` — v1 `whs_...` secret (or the v0 Authorization string) from Top.gg → your bot → Webhooks; webhook URL is `https://<api-host>/api/topgg_webhook`
- `TOPGG_BOT_ID` — your bot's Top.gg ID; enables the "Vote for us" line on the leaderboard (or set `TOPGG_VOTE_URL` directly)
- `TOPGG_VOTE_MULTIPLIER` (default `1.5`) / `TOPGG_VOTE_HOURS` (default `12`) — vote reward; applies in every server and clone, and never stacks with a paid boost (the larger one wins)
- `TOPGG_TOKEN` — Top.gg *project token* (Dashboard → API); makes the main bot post server/shard count every `TOPGG_POST_INTERVAL_MINUTES` (default 30) and push its slash-command list once per start, minus `TOPGG_HIDE_COMMANDS` (default `admin`). Main bot only — set it on the main bot's service, not the clone supervisor

**Optional:**
- `DISCORD_DEV_GUILD_ID` — set during development for near-instant slash-command sync to one guild; leave unset for global sync (~1hr propagation)
- `DISCORD_OAUTH_CLIENT_ID` / `DISCORD_OAUTH_CLIENT_SECRET` — only needed for the Discord-login/dashboard OAuth flow (`api/discord_login_oauth.py`), separate from bot-invite OAuth
- `OWNER_GUILD_ID` / `OWNER_BROADCAST_CHANNEL_ID` — main bot's own support server broadcast target
- `DISCORD_SUPPORT_SERVER_ID` — the support server's guild ID. While the creature-catching game is limited to the support server (the default, see [Catch game](#catch-game)), this is the only server where it works
- `CATCH_SUPPORT_SERVER_ONLY` (default `1`) — set to `0` / `false` / `no` / `off` to open the creature-catching game to every server. No code change or migration needed, just redeploy

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
│       ├── catch.py + _views_catch_*.py  # /catch — creature catching game (see Catch game below)
│       └── ... (see discord_bot/bot.py's setup_hook for the full, current list)
│
├── modules/                      # Shared business logic — no Discord/Telegram-specific code
│   ├── archive_adapter.py        # Bots Archive DB layer, risk scoring, Discord RPC lookups
│   ├── botstore_adapter.py
│   ├── superbot_adapter.py       # Tier/premium checks
│   ├── catch_*.py                # Creature catching game logic (schema, spawns, economy, gate, ...)
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
- **Creature catching** (`/catch`) — wild creatures spawn while members chat; catch, collect, sell and
  shop for them. Currently limited to the support server — see [Catch game](#catch-game)

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

---

## Project history

Past audit reports, handoff notes and phase write-ups live in
[`docs/archive/`](docs/archive/). They're kept for reference and are not
maintained; this README and `config.py` are the source of truth.

## Catch game

A creature-catching game played with `/catch`: wild creatures spawn while members chat in a
server's catch channels, and players throw capsules to catch them, fill a Dex, earn coins and
shop. It is built in phases; Phase 1 is live and the rest is being added screen by screen.

**Availability.** For now the game works **only in the support server**
(`DISCORD_SUPPORT_SERVER_ID`). Everywhere else, including DMs, `/catch` replies that the game is only
open in the support server, and the "Creature catching" button in the join DM and the owner setup
panel's "Turn on" are greyed out. To open it to every server later, set
`CATCH_SUPPORT_SERVER_ONLY=0` and redeploy. It fails closed: if the support server ID is not
configured, nobody can use it.

**What works today** (`/catch` hub, six categories):

| Category | Live | Not built yet |
|---|---|---|
| Play | Encounter, Daily, Wild zone | |
| Collect | Collection (creature detail, lock, nickname, buddy, evolve, trainer card), Dex, Inventory | |
| Economy | Shop, Sell, Wallet | |
| Info | Guide, Rules, Status | |
| Social | | Trade, Gifts, Leaderboard |
| Battle | | Team, Battle, Moves |

The not-built buttons reply "is ready for this server" and do nothing else. Owners configure the
game from `/catch` setup (spawn channels, speed, encounter channels, join DMs) or the join-DM
quickstart; spawns only appear in channels the owner picked.

**Where things live**
- `modules/catch_*.py` — logic: schema, database access, species, spawns, throws, items, shop, sell,
  wild zone, status, creature actions, evolution, trainer profile, emoji table (`catch_emoji.py`: every
  emoji the game shows, edit there to restyle), scheduling, reminders, localization, rendering and the
  feature gate. No Discord UI.
- `discord_bot/cogs/catch.py` — the `/catch` command, hub, setup panel and spawn trigger;
  `discord_bot/cogs/_views_catch_*.py` — one file per screen.
- `data/catch/` — species roster (48 species, five rarities), drop tables, theme. `locales/en.json` — all
  player-facing text under `catch.*` keys.
- Tables (`catch_*`) are created on first boot by `database.py` through `modules/catch_schema.py`. That step
  is isolated: if it ever fails it is logged (`Catch game schema failed`) and the rest of the bot still
  starts. After fixing the cause, bump `SCHEMA_VERSION` in `database.py` so it runs again.

**Safety rules the code follows**
- Every screen answers the interaction first, then checks the feature gate, then reads or writes.
- Anything that moves coins or creatures (daily, shop, sell, catching) is one database transaction with
  row locks and an audit row; the client never sends a price.
- The gate (`modules/catch_gate.py`) is checked before every action: global kill switch, per-server
  flags, and the support-server restriction above.
- Buttons use restart-safe custom IDs, so old hub messages keep working after a restart.

**Tests**
```bash
python -m pytest -q                      # unit tests; the real-database tests are skipped
# Real PostgreSQL tests: point at a DISPOSABLE database. They drop and recreate every catch_* table.
CATCH_TEST_DSN=postgresql://user:pass@localhost:5432/scratch \
  python -m pytest -q tests/integration/test_catch_postgres_smoke.py
```
CI also lints the catch files; add any new `_views_catch_*.py` to the `ruff` line in
`.github/workflows/ci.yml`.

**More detail:** [`docs/catch/ARCHITECTURE.md`](docs/catch/ARCHITECTURE.md) (module boundaries, restart
behavior), [`docs/catch/CATCH_GAME_PLAN.md`](docs/catch/CATCH_GAME_PLAN.md) (the full plan),
[`docs/catch/REVIEW_P1_FIXES.md`](docs/catch/REVIEW_P1_FIXES.md) (what each fix changed and how it was tested),
[`docs/catch/CATCH_AUDIT_HANDOFF.md`](docs/catch/CATCH_AUDIT_HANDOFF.md) (open audit items).
