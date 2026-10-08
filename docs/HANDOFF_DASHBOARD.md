# PRIME BOT web dashboard: handoff

Read this first if you are the next Claude picking up the dashboard. Everything below was verified against the code on branch `feat/web-dashboard-v1` (PR #113) unless marked **UNVERIFIED**.

## 1. What exists

| Piece | Where | Notes |
|---|---|---|
| Frontend | `dashboard/` (Cloudflare Pages, project `prime-bot-dash`) | Plain JS, no build step. `assets/dash.js` (router + views), `dash.css`, `bg.js` (particles), `config.js` (API base, invite and site URLs). All server data is rendered with `textContent`, never `innerHTML`. Keep it that way. |
| Backend | `api/dash.py`, mounted at `/api/dash` in `api_server.py` | One handler, action-based routes. Pure-Python `BaseHTTPRequestHandler`, `asyncio.run` per request. |
| Settings catalogue | `utils/dash_schema.py` | `MODULES` list. Each module maps to an existing `db.get_*_config` / `db.set_*_config` pair. The frontend renders forms from `GET ?action=schema`. **Adding a setting = one `F(...)` entry.** Pure functions, no I/O. |
| Sessions | `discord_login_sessions` table (reused) | Payload `kind: "dash"`. Opaque id sent as `Authorization: Bearer`. TTL `DASH_SESSION_MINUTES` (720). No cookies, so no CSRF surface. |
| Deploy | `.github/workflows/deploy-dashboard.yml` | Deploys `dashboard/` to Pages. |
| Tests | `tests/unit/test_dash_api.py`, `test_dash_schema.py`, `test_dash_dropbox.py` | 24 passing at time of writing. Run: `python3 -m pytest tests/unit/test_dash_*.py -q` |

### Security model (do not weaken)
1. Login is Discord OAuth2 (`identify guilds`). The OAuth token is used once and never stored.
2. Every guild request goes through `_authorised_guild`: guild is in the session's list, the bot is in the guild, and the user is **re-checked against Discord** (`_user_can_manage`, cached 60s) for Administrator / Manage Server / ownership.
3. Saves go through `S.validate_values`: only declared keys, typed and range-checked, channel and role ids must belong to that guild, Premium-only fields are rejected on free guilds.
4. Owner-only actions use `_is_owner`, which checks `config.DISCORD_OWNER_BROADCAST_IDS`. Never trust a client flag.
5. CORS allows only `config.DASH_PAGES_URL`.

### Env vars
`DASH_PAGES_URL`, `DASH_OAUTH_REDIRECT_URI`, `DASH_SESSION_MINUTES` (optional), plus existing `DISCORD_OAUTH_CLIENT_ID/SECRET` and `DISCORD_BOT_TOKEN`. The redirect `https://web-production-74667a.up.railway.app/api/dash` must be registered in the Discord Developer Portal.

## 2. Drop Box (new in this PR)

Owner writes a message once; every admin who signs into the dashboard sees it in **Drop box** (header button with unread badge). If "Also show as banner" is ticked, it also shows as a banner at the top of the panel until each admin dismisses it.

- Tables: `dash_dropbox_messages`, `dash_dropbox_reads` (per-user read state), created in `database.py` next to `discord_login_sessions`.
- DB methods: `dropbox_create / list / mark_read / delete / stats`.
- Routes: `GET ?action=dropbox`, `POST dropbox_read {id?}`, `POST dropbox_send` (owner), `POST dropbox_delete` (owner). `GET ?action=me` also returns `unread` and `is_owner`.
- Validation: `S.validate_dropbox` (title 100, body 2000, kinds info/update/warning/maintenance, expiry 1h to 90d).
- UI: `renderInbox`, `renderComposer`, `showAnnouncements` in `dash.js`. The composer only renders when `is_owner` is true; the server enforces it regardless.
- Limitation: "all admins" means everyone who has signed into the dashboard, since messages are read on demand. It does **not** DM people. For true push, see feature D below.

## 3. Not covered yet (known gaps)
- Clone bots (`CLONE_ID = None` is hardcoded in `api/dash.py`).
- Settings not in the schema: welcome card artwork, application forms, reaction roles, anti-raid review screens.
- Anything where the bot must post a message (verify panel, ticket panel, reaction-role message) still happens in Discord.
- Not yet tested in a real browser against real Discord (only a fake server). **First job: do a real sign-in with a second account.**

## 4. Deep features to build next (priority order)

Each has: goal, where it plugs in, and gotchas.

**A. Bot-action bridge ("Post it for me").**
Goal: buttons like "Post verify panel", "Post ticket panel", "Send test welcome" in the dashboard. Plug in: new `POST action=bot_action {guild_id, action, channel_id}` that calls Discord REST with the bot token (same pattern as `_bot_get`, add `_bot_post`). Whitelist actions server-side; reuse the embed/view builders from the existing cogs rather than duplicating them. Gotcha: component buttons need the bot's persistent views to be registered, so confirm with the cogs how `/setupverification` posts its message.

**B. Welcome card designer.**
Goal: live preview and artwork picker for welcome cards. Plug in: new field types `image_choice` and `preview` in the schema renderer; preview can call the existing card renderer (find it from the welcome cog) behind a rate-limited `GET action=preview`. Gotcha: render server-side, cache by settings hash, cap size.

**C. Audit log.**
Goal: "who changed what, when" per guild. Plug in: a `dash_audit` table written inside the `save` route (user id, guild, module, changed keys with old and new values; never log secrets). A new `GET action=audit&guild_id` and an Audit page. Gotcha: store only keys declared in the schema.

**D. Drop Box push + targeting.**
Goal: also DM guild owners, and target segments (Premium only, servers over N members, one guild). Plug in: reuse the queue-and-cron pattern in `api/cron_discord_owner_broadcast.py` (batching, rate limits). Add `audience` columns to `dash_dropbox_messages`. Gotcha: DMs fail for closed-DM users, so log and skip, never retry forever.

**E. Premium checkout inside the dashboard.**
Goal: upgrade button per guild. Plug in: existing payment modules (`payments.py`, `payments_manual.py`, `gumroad_payments.py`, `paystack_webhook.py`). Create a `POST action=checkout {guild_id, plan}` that returns a payment URL built by the existing code. Gotcha: the guild id must be bound server-side to the payment, never taken from a redirect parameter. Read how the existing flow does it first.

**F. Level tier gallery.**
Goal: browse and pick tier art. The `tier*.png` files live in the repo root. Plug in: an `image_choice` field type (shared with B) and a static manifest endpoint.

**G. Anti-raid review screen.**
Goal: list flagged joins, approve/kick/ban in one click. Plug in: read from the anti-raid tables, write actions through a whitelisted `bot_action`. Gotcha: needs the bot's permission and role-hierarchy checks; surface Discord's error text.

**H. Command and role permission manager**, **I. Scheduled messages**, **J. Ticket transcripts viewer**, **K. Per-module "reset to defaults"**, **L. Import/export of a module's settings as JSON** (validated through `validate_values`), **M. Clone bot support** (pass `clone_id` from the session after verifying ownership of the clone).

**N. Real-time status.** Replace the 45s cached guild stats with a small SSE or polling endpoint only if usage justifies it.

## 5. Conventions
- New setting: add to `dash_schema.py`, add a test in `test_dash_schema.py`. No frontend change needed for existing field types.
- New route: add it to `_route` and to the docstring at the top of `api/dash.py`. Add a test using the `env` and `call` helpers from `test_dash_api.py`.
- Never use `innerHTML` with server data. Never accept a guild id without `_authorised_guild`.
- Keep PRs small, one feature per PR. Merge order matters for the deploy workflow (website and captcha PRs first).

## 6. Housekeeping
- A GitHub personal access token was pasted into a chat to create this work. **Revoke it at github.com/settings/tokens and issue a new fine-grained one scoped to this repo only.**
