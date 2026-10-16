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

## Tests
`python -m pytest tests/unit/test_guildatlas.py` (35 tests). `tests/fixtures/guildatlas_knowledge.json` is generated from the site's code; regenerate it when the site's knowledge changes. The "evals" test checks the facts are present in the prompt; it does not run the language model.

## Verified / not verified
Verified: signature identical to the site's reference signer; the bot's client against the real site code running locally (signed health/drain/review calls, wrong secret rejected, ETag 304, non-reviewer 403). Not verified: any live Discord interaction (button click, modal), alert delivery to a real channel, the AI answering well with the context, behavior on Railway.
