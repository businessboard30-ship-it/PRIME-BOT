# PRIME BOT control panel (web dashboard)

Static site for Cloudflare Pages (plain HTML/CSS/JS, no build step) that lets server owners
configure the bot. It talks to the Railway backend at `/api/dash` (`api/dash.py`).
Settings shown in the panel are defined in `utils/dash_schema.py`; the page renders its forms
from that catalogue, so **adding a setting is one entry there and nothing else**.

Deployed by `.github/workflows/deploy-dashboard.yml` to the Pages project `prime-bot-dash`
(`https://prime-bot-dash.pages.dev`). Uses the same two repo secrets as the other Pages sites.

## One-time setup
1. Discord Developer Portal -> PRIME BOT application -> OAuth2 -> Redirects: add
   `https://web-production-74667a.up.railway.app/api/dash`
2. Railway, `web` service variables:
   - `DASH_PAGES_URL` = `https://prime-bot-dash.pages.dev`
   - `DASH_OAUTH_REDIRECT_URI` = `https://web-production-74667a.up.railway.app/api/dash`
   - `DISCORD_OAUTH_CLIENT_SECRET` and `DISCORD_BOT_TOKEN` (already present)
3. Merge to `main`; the Action publishes the site and Railway redeploys the backend.

## How access works
Sign-in is Discord OAuth2 (`identify guilds`). The browser keeps an opaque session id and sends it as
a Bearer token. Every request is re-checked against Discord: the bot looks the user up in the server and
requires Administrator or Manage Server (or ownership). Only declared settings can be written; each value is
validated and channel/role ids must belong to that server. Premium-only settings are refused on free servers.

## Scope
Main PRIME BOT only (clone bots are not covered yet). Actions that need the bot to post messages
(for example the verify panel) are still done from Discord.
