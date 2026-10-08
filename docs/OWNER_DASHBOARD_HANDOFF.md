# PRIME BOT: Owner Dashboard Handoff

Written 8 Oct 2026. Read this first, then `OWNER_DASHBOARD_PLAN.md` (the plan this work follows).

## 1. Where everything is

| Thing | Value |
|---|---|
| Repo | https://github.com/businessboard30-ship-it/PRIME-BOT (clone URL ends `.git`) |
| Base | `main` at `fc5bcf8` (after PR #121) |
| Phase 0 branch | `feat/owner-dashboard-phase0` (commit `51b4195`), based on `main` |
| Phase 1a branch | `feat/owner-phase1-readonly` (commit `51d3d7c`), STACKED on Phase 0 (contains its commit) |
| PR links | `.../pull/new/feat/owner-dashboard-phase0` and `.../pull/new/feat/owner-phase1-readonly` |
| Status | Both pushed, **no PRs opened, nothing merged, nothing deployed** |
| Services | Railway `web` (`api_server.py`, serves `/api/dash`) and `worker` (`discord_bot/bot.py`). Dashboard front end is Cloudflare Pages (`dashboard/`). |
| Dashboard backend URL | `https://web-production-74667a.up.railway.app/api/dash` |
| Dashboard front end | `https://prime-bot-dash.pages.dev` |

### Credentials (IMPORTANT)
- **No token is stored in this doc, the repo, or `.git/config`.** Earlier chats pasted a classic `ghp_` token into the conversation. It must be **revoked** at github.com/settings/tokens.
- Replace it with a **fine-grained token scoped to PRIME-BOT only**, Contents (read/write) and Pull requests (read/write). Ask the owner for it per session and pass it **inline on the single git command** (`git push https://<token>@github.com/businessboard30-ship-it/PRIME-BOT.git <branch>`). Never write it to a file, git config, or memory. Clean remote after cloning: `git remote set-url origin https://github.com/businessboard30-ship-it/PRIME-BOT.git`.
- Pipe git output through `sed 's/ghp_[A-Za-z0-9]*/<token>/g'` so it never echoes.

## 2. Merge order (do this, in order)

1. Open and merge the Phase 0 PR. **It bumps `SCHEMA_VERSION` 59 to 60.**
2. After deploy, check Railway logs for `[db init] schema_version '59' != '60'` followed by the DDL pass. That confirms `dash_owner_audit` exists. If you don't see it, the schema did not apply.
3. Retarget/open the Phase 1a PR against `main` (it has no schema change; stays at 60).
4. Per the plan's conventions: merge website and captcha PRs first because of the deploy workflow; one feature per PR.
5. First sign-in after deploy: owners must sign in again (existing sessions have no `iat`, so owner routes return 401 "Owner sessions are short").

## 3. SCHEMA BUMP RULES (the thing that has bitten this project)

- `database.py` `SCHEMA_VERSION` gates the whole DDL pass. If `_create_tables` changes and the version doesn't, **the change silently never runs** (UndefinedTableError in production; this caused the PR #121 outage).
- Guard: `tests/unit/test_schema_version_guard.py` pins `(PINNED_VERSION, PINNED_HASH)` of the normalised `_create_tables` body. It fails if the body changes without a bump, and fails if the version changes with no schema change. Workflow when you add a table or column: (1) change DDL, (2) bump `SCHEMA_VERSION` + add a `# "N" -> "N+1" ...` comment line, (3) run the test, it prints the new hash, (4) set `PINNED_VERSION` and `PINNED_HASH` to the printed values in the same PR.
- Whole-line comments are ignored by the hash; edits inside SQL strings count.
- **Planned next bump: 60 to 61** for the worker status snapshot table (Phase 1b below). Do not bump for anything else.

## 4. What is built

### Phase 0 (foundation) on `feat/owner-dashboard-phase0`
- `modules/admin_access.py`: single source of truth for owner sections (`compute_sections`). The Discord `allowed_sections` now calls it; web calls it too. Parity test included. Owner-only (never grantable): `access`, `config`, `database`.
- `api/dash.py` helpers: `_owner_sections`, `_require_section` (403 if not allowed, 401 if owner session older than `DASH_OWNER_SESSION_MINUTES`, default 120), `_require_fresh` (step-up), `_require_confirm` (exact typed text), `_owner_rate` (in-memory per user/action), `_owner_write` (**fail-closed audit**: audit row first; if that write fails the action does not run, 503).
- Routes: `POST owner_stepup` (returns Discord authorize URL with `prompt=consent`; state `return_to="dash_stepup:<sid>"`; callback only marks the SAME session fresh for `DASH_STEPUP_MINUTES` (5) and rejects a different Discord account), `POST owner_signout_all` (needs fresh + typed `SIGN OUT`), `GET owner_audit`, and `me` now returns `owner_sections`.
- Sessions: `iat` added at creation. `_session()` now returns the payload plus `_sid`.
- DB: `dash_owner_audit` table, `owner_audit_add/set_result/list`, `update_login_session_payload`, `delete_login_sessions_for_user`. Discord panel `audit()` also writes to `dash_owner_audit` (best effort, source `discord`).
- Config: `DASH_OWNER_IDS` env (comma/space separated IDs) is UNIONED with the hardcoded `DISCORD_CLONE_ADMIN_IDS`, never replaces it. `DASH_OWNER_SESSION_MINUTES`, `DASH_STEPUP_MINUTES`.
- Front end: lazy `dashboard/assets/owner.js`, route `#/owner/<page>`, "Owner" header button only if `owner_sections` non-empty. Pages: Overview, Audit log, Security (step-up, sign out everywhere).
- v1 is **owner-only on the web**. Helpers get nothing there (plan open decision 1).

### Phase 1a on `feat/owner-phase1-readonly`
- `api/dash_owner.py`: read-only handlers, `ROUTES` table dispatched from `_route`. Routes (all GET, section-gated, 60/min rate limit): `owner_health` (health), `owner_servers` (servers; search, paging, clone filter), `owner_server` (inspect), `owner_user` (inspect), `owner_payments` view=`pending|failures|revenue` (money), `owner_expiries` (money).
- `modules/admin_inspect.py`: new `server_list` (literal LIKE escaping, validated on a real Postgres 16) and `guild_counts`.
- `jsonable()`: ids (any key ending `id`) and ints beyond 2^53 become **strings**; datetimes ISO; Decimals floats. Discord snowflakes lose precision as JS numbers.
- Pages: Health, Servers (search + inspect by id), Users, Payments (pending, failed, revenue, expiries). All server data via `textContent`.

### Tests
- Run: `python3 -m pytest tests/unit -q` (full suite: 1927 passed at `51d3d7c`, about 2.5 min) or `python3 -m pytest tests/unit/test_dash_*.py tests/unit/test_schema_version_guard.py -q`.
- New: `test_dash_owner.py` (permission matrix, step-up, fail-closed audit, session age, parity), `test_dash_owner_phase1.py`, `test_schema_version_guard.py`.
- Setup if starting fresh: `pip install -r requirements.txt pytest --break-system-packages`.
- **Test gotcha:** other test modules drop and re-import `modules.admin_*`. In tests, patch those modules via `importlib.import_module("modules.admin_inspect")` inside the fixture, not a module-level import, or tests pass alone and fail in the full run.

## 5. Not built yet (next work)

### Phase 1b (needs schema 60 to 61)
Live bot facts are NOT visible to the web process (separate Railway service). Health currently reports only DB-derived facts and returns `live_bot: null`. Remaining Phase 1 items need a worker snapshot:
1. New table, e.g. `bot_status_snapshots (key TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`. **Bump to 61 and update the guard test.**
2. Worker task (every ~60 s) upserts key `main`: `admin_inspect.bot_snapshot(bot)` (latency, uptime, memory, guild count, cogs), `loop_report(bot)`, `error_counts()`; and key `logs`: last ~200 lines from `admin_ops.recent_logs`, **already passed through `admin_ops.mask_secrets`**.
3. Web: `owner_health` merges the snapshot with an "as of N s ago" and a stale warning. Add `owner_logs` (section `logs`, masked) and `owner_config` (section `config`, owner-only, `admin_ops.config_entries`, masked).
4. **Config viewer caveat:** the web process reads its own env, which may differ from the worker's. Either publish the masked config from the worker too, or label the page "web service config".

### Later phases (see plan)
Phase 2 safe controls (kill switches, blacklist, premium, audit viewer, announcements, feedback), Phase 3 money (step-up + typed confirm; **do a real small `/pay` + Gumroad/Paystack test and a real-browser test with a second Discord account first**), Phase 4 safety, Phase 5 helpers/clones/DB cleanups (clone start/stop and mass DMs need an `owner_jobs` table polled by the worker, same pattern as `api/cron_discord_owner_broadcast.py`), Phase 6 polish.
Every owner **write** must use `_owner_write`, a permission matrix test, a step-up test where destructive, and an audit test.

## 6. Open decisions (owner has not answered)
1. Helpers on the web? Recommended: owner-only v1, add helpers (audit, blacklist, premium, logs) in Phase 2.
2. Money actions on the web? Recommended: yes with step-up; otherwise keep Phase 3 read-only.
3. Database tools on web? Recommended: Discord-only.
4. More than one owner? Phase 0 already supports extra IDs via `DASH_OWNER_IDS`, but the hardcoded ID remains the fallback. Moving fully to env/DB-backed is still open.
5. Should owner **reads** (e.g. user inspector shows payment totals) be audited? Currently reads are rate-limited and not audited; only writes and step-up start are.
6. Should the Discord-side audit write fail closed too? Currently best effort.

## 7. Gotchas and conventions
- Never `innerHTML` with server data; `textContent`/`h(...,{text})` only. CSP is `script-src 'self'; style-src 'self'` (no inline styles/scripts), connect-src limited to the Railway backend.
- CORS is limited to `DASH_PAGES_URL`. Bearer-token sessions, no cookies.
- Never accept a guild id without `_authorised_guild` on guild routes; owner routes use `_require_section`.
- New routes: add to `_route` and the docstring at the top of `api/dash.py`, plus tests using the `env`/`call` helpers from `tests/unit/test_dash_api.py`.
- `parse_snowflake` requires 10 to 20 digits; tests must use realistic ids.
- `get_login_session` takes a TTL arg; owner TTL is enforced separately through `iat` in `_require_section`.
- Env vars to know: `DASH_PAGES_URL`, `DASH_OAUTH_REDIRECT_URI`, `DISCORD_OAUTH_CLIENT_ID/SECRET`, `DASH_SESSION_MINUTES` (720), `DASH_OWNER_SESSION_MINUTES` (120), `DASH_STEPUP_MINUTES` (5), `DASH_OWNER_IDS` (optional extra owners).
- Hardcoded owner Discord id lives in `config.py` (`DISCORD_CLONE_ADMIN_IDS`, `DISCORD_OWNER_BROADCAST_IDS`).
- Still unverified end to end: real Discord sign-in, step-up round trip on a real browser, and a real payment. The step-up OAuth flow is unit tested with a faked Discord only.
- Postgres can be installed in the sandbox for real SQL checks (`apt-get update && apt-get install -y postgresql`, then `initdb` as the `postgres` user on a non-default port). Used to validate `server_list`.

## 8. Suggested first message for the next session
"Read docs/OWNER_DASHBOARD_HANDOFF.md and OWNER_DASHBOARD_PLAN.md in github.com/businessboard30-ship-it/PRIME-BOT. Phase 0 and Phase 1a are pushed on two branches, unmerged. Start Phase 1b (worker status snapshot, schema bump to 61 with the guard test). I will give you a fine-grained token inline for pushes only."

---

## 9. Update: Phase 1b + Phase 2 (branch `feat/owner-phase1b-phase2`, STACKED on Phase 1a)

Merge order is now: Phase 0 -> Phase 1a -> this branch. **This branch bumps `SCHEMA_VERSION` 60 -> 61** (new table `bot_status_snapshots`). After deploy, check Railway logs for `[db init] schema_version '60' != '61'` and the DDL pass. Guard test is pinned to 61 / `37a7b502bb7fcc7e`.

### Phase 1b (worker snapshot)
- `modules/admin_snapshot.py`: the **main bot only** (not clones) publishes every 60 s: key `main` (latency, uptime, memory, servers, cogs, loops, error counts), `logs` (last 200 WARNING+ lines), `config` (worker config via `config_entries`). Everything is passed through `mask_secrets` before the write. One failing key never blocks the others. Loop: `_owner_snapshot_loop` in `discord_bot/bot.py`.
- Web: `owner_health` merges `live_bot` with `age_s` and `stale` (older than 180 s). New `owner_logs` (section `logs`) and `owner_config` (section `config`). Config comes from the **worker**, so the old "web env differs" caveat is gone; pages say "bot worker".
- DB: `bot_snapshot_put` / `bot_snapshot_get`. Validated on real Postgres 16.

### Phase 2 (safe controls), all through `dash_owner.WRITES` and the generic POST route in `dash.py`
`WRITES[action] = (section, per-minute limit, prepare)`. `prepare(sess, body)` validates and returns a plan; `dash.py` enforces section, rate limit, step-up (`fresh`), typed confirm, and runs it via `_owner_write` (fail-closed audit). To add a write, add a `prepare_*` and a `WRITES` entry, plus tests.
| Action | Section | Extra protection |
|---|---|---|
| `owner_switch` (kill switches) | controls | engaging `maintenance`: step-up + type `MAINTENANCE`; engaging opt-in `build_bot_public`: step-up + type `OPEN`; feature switches and all releases: audit only |
| `owner_blacklist_add/remove` | blacklist | cannot blacklist an owner id (user kind); reason sanitised, 300 chars; blacklisting a SERVER needs step-up + type `BLOCK` (users: audit only) |
| `owner_premium_revoke` | premium | step-up + type `REVOKE` |
| `owner_premium_grant` (grant / extend, adds days on top, 1 to 3650) | premium | step-up + type `GRANT`; shared logic is `admin_controls.grant_premium` |
| `owner_announce` | broadcast | dashboard-only message: audit only; with `push_dm` (mass DM): step-up + type `SEND` |
| `owner_announce_delete` | broadcast | audit only |
Reads: `owner_controls`, `owner_blacklist`, `owner_premium`, `owner_feedback` (messages cut to 1000 chars), `owner_botaudit` (the bot's own `admin_panel_audit` with filters `what`, `guild_id`, `admin_id`, `before`; shown under the Audit log page). The web owner trail (`owner_audit`) shipped in Phase 0.
- **Gotcha 2:** query filters must not be named `action` (the router's own key) or `clone_id`. `owner_botaudit` uses `what`.
- Not changed: the Discord panel's own grant/extend buttons still call `db.activate_guild_premium` directly (same underlying function). Owner list is still `DASH_OWNER_IDS` unioned with the hardcoded ID (plan wanted it fully out of config; still open).
- **Gotcha:** a body field named `clone_id` is swallowed by the dashboard's global clone-context handling (404 "That bot isn't available"). Owner writes use `clone`.
- Kill-switch and blacklist changes reach the bot within its 20 s cache TTL (the web process can't invalidate the worker's cache).
- Announcements on the web reuse `validate_dropbox` and the existing dropbox DB functions; the old `dropbox_send` / `dropbox_delete` routes still exist and are NOT audited by `_owner_write`. Consider removing them from the owner UI path or auditing them (open decision).
- Front end: new pages Controls, Blacklist, Premium, Announcements, Feedback, Logs, Config. Typed confirmations use `window.prompt` (no inline scripts, CSP-safe). A `stepup_required` reply tells the owner to use the Security page.
- Tests: `tests/unit/test_dash_owner_phase1b_2.py` (40 tests: permission matrix for every read and write, step-up, typed confirm case-sensitivity, fail-closed audit, validation, owner-lockout guard, snapshot staleness, masking). Full suite: see last commit message.

### Still unverified end to end
Real Discord sign-in, the step-up round trip in a real browser, a live worker publishing a snapshot to the Railway DB, and the front-end pages (only syntax-checked, not exercised in a browser).

---

## 10. Phase 3 (part 1): database-only money actions (branch `feat/owner-phase3-money`, STACKED on `feat/owner-phase1b-phase2`)

No schema change (stays at 61). All reuse `modules/admin_money.py`, so web and Discord behave the same.
| Action | Protection |
|---|---|
| `owner_payment_reverse` (by reference) | step-up + type `REVERSE`; only completed `premium` payments (`reversal_problem`); takes PREMIUM_DAYS back off the server; **does not refund at the gateway** |
| `owner_coupon_create` | audit only below 50% off; 50% or more: step-up + type `CREATE`. Code 3-24 chars, 1-100%, max uses and days optional |
| `owner_coupon_toggle` | audit only |
| `owner_failure_dismiss` | audit only |
| `owner_pending_clear` (expire checkouts older than 6 h; rows kept) | step-up + type `CLEAR` |
Reads: `owner_payment` (lookup by reference; returns only a whitelisted field set, never the raw row), `owner_coupons`. UI: Payments page gained Reverse and Coupons tabs, a Clear button on Pending, Dismiss buttons on Failed. Tests: `tests/unit/test_dash_owner_phase3.py` (38).

### Phase 3 items NOT built yet, and why
- **Approve / reject pending payment:** Discord's `/admin payments approve` runs `resolve_manual_payment_approval(bot, ...)`, which runs unlock handlers and DMs the buyer through the live bot. The web process has no bot client, so this needs the `owner_jobs` queue (worker polls it; schema 61 -> 62) and must not ship before the real small `/pay` + Gumroad/Paystack test the plan requires.
- **Referral giveaway, Ads/Marketplace listings, Bump network:** not started; each is its own page/PR and needs its Discord view read first.
- The plan's gate still stands: do a real small purchase and a real-browser test with a second Discord account before merging anything in this phase.

---

## 11. Phase 3 remainder + Phase 4 (branch `feat/owner-phase3b-phase4`, STACKED on `feat/owner-phase3-money`)

No schema change (stays at 61). Two new modules, merged into the router by `dash._merged` (raises on a duplicate action name): `api/dash_owner_growth.py` and `api/dash_owner_safety.py`. All writes reuse the exact functions the Discord panel calls (`modules/ads_marketplace.py`, `modules/admin_safety.py`, `modules/scam_shield.py`).

**Left out on purpose (owner's instruction):** approve / reject PAYMENT, Referral giveaway. **Also not built, needs the `owner_jobs` queue (live bot):** Bump review (approve/reject a bot listing), adding a Scam Shield IMAGE rule (Discord hashes the attachment via the live bot).

| Action | Section | Protection |
|---|---|---|
| `owner_ad_approve` / `_deactivate` / `_reactivate` | ads | audit only; state checked first (409 if the ad is not in the right state) |
| `owner_ad_reject` | ads | audit only; reason required (200 chars); DM to submitter over Discord REST with the bot token, best effort, never undoes the rejection |
| `owner_listing_remove` | ads | step-up + type `REMOVE` (no undo exists); uses the real seller id like Discord |
| `owner_bump_cooldown` | bump | audit only; 1 to 10080 minutes |
| `owner_report_resolve` | reports | audit only; reviewed / dismissed; double press is a no-op |
| `owner_status_add` / `_remove` / `owner_presence_set` | status | audit only |
| `owner_status_reset` | status | step-up + type `RESET` |
| `owner_scam_toggle` | scamshield | turning OFF: step-up + type `DISABLE`; turning ON: audit only |
| `owner_scam_add` (word / domain) / `owner_scam_remove` | scamshield | audit only; min 4 chars like Discord |
Reads: `owner_ads`, `owner_ad`, `owner_market`, `owner_bump`, `owner_watchlist`, `owner_reports`, `owner_status`, `owner_honeypot`, `owner_scamshield` (watchlist and honeypot are read-only).

**Cache latency (shown on the pages):** the web service cannot clear the worker's memory. Scam Shield rules/toggle reach the bot within `scam_shield.CACHE_SECONDS` (5 min); custom statuses within 60 s. Discord's panel is instant because it runs inside the worker.

**Safety of displayed text:** ad, listing and report text is written by strangers. Server side it is clipped and control characters removed; the front end uses `textContent` only, and an ad's link is shown as plain text, never as an `href`.

Tests: `tests/unit/test_dash_owner_phase3b_4.py` (74): permission matrix for every route (anonymous 401, guild admin 403, stale owner session 401), helper grants do not open these sections, step-up + typed-confirm case sensitivity, validation, wrong-state 409s, DM failure does not undo a rejection, fail-closed audit, whitelisted output. Mutation spot-check done: removing the step-up on `DISABLE` or `REMOVE` makes a test fail.

**Still gated by the plan before MERGING anything from Phase 3/4:** a real small `/pay` + Gumroad/Paystack test, and a real-browser run with a second Discord account. None of the new pages has been opened in a browser (JS is syntax-checked only).

---

## Phase 5 (owner-only and dangerous): helpers, clones, named database cleanup

No schema change (stays at 61). New: `api/dash_owner_ops.py` (merged by `dash._merged`), `modules/admin_clones.py`, owner.js pages **Helpers**, **Clones**, **Database**, tests in `tests/unit/test_dash_owner_phase5.py` (41).

**Deviation from the plan: no `owner_jobs` queue.** `discord_bot/clone_manager.py` already polls `discord_cloned_bots.status` every 30 s and starts/stops the matching process, so stop, register and relink are database writes plus Discord REST calls made with the clone's own token. Nothing needs the live bot's memory. (Approve/reject payment and Scam Shield image rules still do, and are still not built.)

| Action | Section | Protection |
|---|---|---|
| `owner_helper_set` | access | audit only (as in the plan: only removal needs step-up). Sections limited to `admin_controls.GRANTABLE`, so `access`/`config`/`database` can never be granted. Owners cannot be made helpers. Bot sees it within about 20 s |
| `owner_helper_remove` | access | step-up + type `REMOVE` |
| `owner_clone_register` | servers | step-up + type `REGISTER`. Free for the signed-in owner. Refused if the owner already has a clone (use relink, so history carries over) |
| `owner_clone_relink` | servers | step-up + type `RELINK`. Only a clone the signed-in owner owns |
| `owner_clone_stop` | servers | step-up + type `STOP`. Sets `inactive`; 404 unknown, 409 already stopped |
| `owner_db_cleanup_stale` | database | step-up + type `CLEANUP`. The one named cleanup Discord has. Rows are kept, status becomes `expired` |
Reads: `owner_helpers`, `owner_clones` (never selects the token column), `owner_database` (counts only).

**Token handling:** the bot token is only passed in, shape-checked, validated against Discord, encrypted and stored. It is not in the response, the audit row (`target` is `owner:<id>` / `clone:<n>`), or the logs. A test asserts this.

**Gotcha found while building:** `dash.py` reserves a POST body field called `clone_id` as the dashboard's own clone-scope selector (`_enter_clone`). Owner routes must name the field `clone`.

**Not built:** starting a stopped clone (not in the plan; relinking brings a clone back), sub-clone registration, Discord's `register_clone_token` was NOT refactored onto `modules/admin_clones.py` (it carries the payment path; a parity refactor is a separate PR), no raw SQL, no other cleanups.

**Before merging:** real-browser run with a second Discord account (JS is syntax-checked only), and register/relink with a real throwaway bot token.

## Phase 6 (polish) - built on `feat/owner-phase6`

- **Live status:** the Health page re-fetches every 15 s while the tab is visible (checkbox to pause). Polling only, no SSE: the bot worker publishes a snapshot every ~60 s, so a push channel would add nothing.
- **Charts:** inline SVG bar charts (no library, `textContent` only): revenue per day on Payments > Revenue, server joins/leaves on Servers (`owner_growth`, from `discord_guilds.joined_at/left_at`).
- **Global search:** box at the top of every owner page (`owner_search`): server name/id, user id, payment reference. Results jump to the existing Servers / Users / Payments pages pre-filled. Each source is only searched if the caller holds its section.
- **Notifications:** Alerts bell (`owner_alerts`, polled every 60 s while visible): stale worker snapshot, quiet clones, open payment failures, checkouts pending > 6 h, report backlog (>= 5). Computed on read from existing data; nothing is stored.
- **No schema change**, so `SCHEMA_VERSION` stays at 61.
- Router: a read handler marked `wants_sections` also receives the caller's sections (`api/dash_owner_insights.py`).
- Tests: `tests/unit/test_dash_owner_phase6.py`.
