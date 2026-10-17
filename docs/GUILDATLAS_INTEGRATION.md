# GuildAtlas integration (bot side)

Connects this bot to the GuildAtlas directory site. Wire contract and site behavior: `docs/bot-integration.md` in the GuildAtlas repo.
Code: `modules/guildatlas.py` (logic), `discord_bot/cogs/guildatlas.py` (Discord), small hook in `modules/ai_features.py`.

## Environment variables (Railway, bot service)
| Name | Purpose |
|---|---|
| `GUILDATLAS_SITE_URL` | Site address, e.g. `https://yourdomain` |
| `GUILDATLAS_INTERNAL_SECRET` | Must equal `INTERNAL_API_SECRET` on the site (32+ random characters) |
| `GUILDATLAS_ALERT_CHANNEL_ID` | Optional private channel for "site is down / notifications stuck" alerts |

Without the URL and a 32+ character secret the integration is OFF (one log line at start-up) and nothing else in the bot changes.

## What it does
- **Review buttons**: Approve / Request changes / Reject on the site's submission messages. Request changes and Reject ask for a reason in a modal. The click only sends a signed request naming the clicking user; the site decides whether that user is a reviewer. Errors are shown privately to the clicker; success is never claimed unless the site said so.
- **Every minute** (main bot only, not clones): public health check (alert after 3 failures in a row, 15 min cooldown, recovery message) and a signed "retry queued notifications" call.
- **Every 5 minutes**: signed internal health; alerts on permanently failed or long-stuck review notifications.
- **AI**: for questions that look like GuildAtlas questions only, the site's public knowledge (`GET /api/knowledge`, ETag-cached, refreshed every 3 h) is added to the AI prompt with rules: answer only from it, never invent features/prices/status, never reveal reviewer identities or private notes, send people to their dashboard.
- `!ga_refresh` (owner only, no slash-command slot used) forces a knowledge refresh.

## Clones and server invites (Disguild Phase 17)
- **Presence (main bot, every 10 min):** sends the ids of every server any bot (main or clone, from `discord_guilds`) is in to `POST /api/internal/guild-presence`, in chunks of 300. Ids only. This is what lets servers that only have a clone be listed.
- **Permanent invites (main bot and every clone, every minute):** `GET /api/internal/invite-requests` lists servers whose manager submitted a listing without an invite. A process that is in that server claims it (atomic, one winner), creates an invite that never expires with unlimited uses (its own code, reason mentions Disguild; tries the system/rules channel first, then text channels the bot can create invites in), stores the code in `disguild_invites`, and reports it back. If no channel allows it, the failure is reported and the manager can resubmit. Submitting the listing is the manager's consent; nothing is created for servers nobody asked about.
- **"Via Disguild":** joins through that invite are recognised by the existing invite tracking (`InvitesCog`), and the join log says "joined via **Disguild**". This only appears in servers that turned invite tracking on and set its channel (Discord invites cannot carry custom text). The new invite is added to the tracking baseline so even the first join is attributed.
- Clones inherit `GUILDATLAS_SITE_URL` / `GUILDATLAS_INTERNAL_SECRET` from the environment of the process that starts them; if those are not set for clones, only the main bot creates invites (for servers it is in).
- Needs the Disguild site PR (businessboard30-ship-it/disguild #39) merged, migration 0016 applied, and a redeploy of both.
- Not verified against real Discord.

## Server and shard counts (Disguild Phase 25)
- **Every process (main bot and each clone), every 10 min:** sends its own application id, server count (`len(bot.guilds)`) and shard count (`bot.shard_count`, left out when the bot is not sharded) to `POST /api/internal/bot-stats`. Counts only. The site shows them as Servers / Shards on that bot's Disguild listing while the report is under 7 days old, and ignores application ids that have no listing (so unlisted clones are harmless).
- A clone only shows counts if its owner listed it on Disguild; the main bot cannot report for a clone because it only knows the clone's server ids, not its counts.
- Needs the Disguild site PR (businessboard30-ship-it/disguild #50) merged, migration 0020 applied, and a redeploy of both. Not verified against the live site.

## Tests
`python -m pytest tests/unit/test_guildatlas.py` (39 tests). `tests/fixtures/guildatlas_knowledge.json` is generated from the site's code; regenerate it when the site's knowledge changes. The "evals" test checks the facts are present in the prompt; it does not run the language model.

## Verified / not verified
Verified: signature identical to the site's reference signer; the bot's client against the real site code running locally (signed health/drain/review calls, wrong secret rejected, ETag 304, non-reviewer 403). Not verified: any live Discord interaction (button click, modal), alert delivery to a real channel, the AI answering well with the context, behavior on Railway.

## Disguild payments (boosts and donations)
Gumroad allows one Ping URL per account, and this bot owns it (`/api/gumroad_webhook`). Disguild sales therefore arrive here and are handed to the site:

- A ping whose order reference starts with `ga_` is not a bot order. `_process_gumroad_ping_inner` calls `modules.guildatlas.route_gumroad_ping` first. For a real (non-test) sale it checks the sale with Gumroad's API using this bot's `GUMROAD_ACCESS_TOKEN`, then POSTs the ping fields to the site's signed endpoint `/api/internal/payments/ping` (same `GUILDATLAS_SITE_URL` and `GUILDATLAS_INTERNAL_SECRET` as the rest of the integration). The site does the order, product, amount, claim and boost checks.
- The site's answer is returned to Gumroad. If the site is unreachable or answers badly, the ping gets a 503 so Gumroad retries, and the owner is DM'd ("disguild unavailable").
- Refunds and disputes are forwarded too (the site matches them by sale id). A refund with no Disguild reference is forwarded best effort and then handled by the bot as before.
- Test purchases: the bot skips its API check and the site decides (`GUMROAD_ACCEPT_TEST=true` on the site). The bot's own `GUMROAD_ACCEPT_TEST_PINGS` only affects bot orders.
- Nothing changes for the bot's own orders: pings without a `ga_` reference take the old path.
- Disguild products must be separate Gumroad products from this bot's. The site keeps their links in its own `GUMROAD_BOOST_7D_URL`, `GUMROAD_BOOST_30D_URL` and `GUMROAD_DONATE_URL`.
