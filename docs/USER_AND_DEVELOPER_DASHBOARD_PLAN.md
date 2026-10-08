# PRIME BOT: Server Panel, Member Dashboard, Developer Mode, Member Messaging and Light Theme Plan

Status: proposed, version 3 (updated with the owner's answers; decisions 1 to 4 below are now settled). Written 8 Oct 2026 against `main` (owner dashboard Phases 0 to 6 merged or in PR #129).
Repository: https://github.com/businessboard30-ship-it/PRIME-BOT (branch `main`)
Scope: additions to the existing web dashboard (`dashboard/` + `api/dash.py`):

- **A. Server panel**: new admin pages for existing bot features.
- **B. Member dashboard**: a signed-in view for ordinary members, with a paid custom level-up card.
- **C. Developer mode**: a paid section (visible but locked without a subscription) with AI chat, export to the storage channel, bring-your-own AI keys and GitHub connect.
- **D. Member Drop Box and friends**: members message other members of the same server after a friend request, only between people who have used the web.
- **E. Light theme** for the whole dashboard.

Non-goals for v1: collections and inventory pages (deferred by decision), reselling anyone's personal AI subscription, running user code, storing secrets in readable form, keeping user files or notes in the database.

---

## 1. What already exists (verified in the repo)

- **Server panel today:** 14 settings modules (welcome, verification, anti-raid, auto-mod, server logs, honeypot, join gate, leveling, voice XP, starboard, tickets, suggestions, invite tracker, economy) plus pages for audit, raid review, schedules, ticket history, billing, inbox, tiers; export, import and reset per module.
- **Permissions:** guild routes authorise against Discord itself (`_authorised_guild`). Bearer-token sessions, no cookies, `textContent` only for server data.
- **Billing today is per server** (`PLANS` in `api/dash.py`, `checkout` binds to a guild) through the existing `/pay` redirect, which already handles Paystack and Gumroad.
- **AI today:** `modules/ai_store_providers.py` uses the bot's own keys and documents that personal subscriptions are never used. `modules/ai_prefs.py` holds per-user character choice.
- **Storage channel pattern:** the media wizard creates or reuses a storage channel (`storage_channel_id`, `db.set_media_storage_channel`) and the bot uploads files there instead of the database.
- **Drop Box today:** owner writes, server admins read (`dash_dropbox_messages`, `dash_dropbox_reads`, `dash_dropbox_dm`, DM cron).
- **Secrets pattern to reuse:** `secret_manager.encrypt` (clone bot tokens) and the encrypted OAuth token in `modules/gdrive_client.py`.
- **Theme today:** one dark palette in `dash.css` `:root`, `<meta name="color-scheme" content="dark">`, animated background in `bg.js`.
- **Reusable owner-area pieces:** SVG `barChart` helper and `every()` polling in `owner.js`, fail-closed audit, step-up auth.

---

## 2. Part A: Server panel additions

Each page reuses the Discord-side logic (move logic out of views into `modules/` first, then web and Discord call the same function, with a parity test).

| # | Page | Reuses | Notes | Effort |
|---|---|---|---|---|
| A1 | **Moderation** (cases, warns, search by user, timeline, remove a warn) | `moderation_logs`, `user_warns`, `modules/moderation_*` | Read-only first, then remove/edit warn (audited). | Low |
| A2 | **Giveaways** (create, list, end, reroll, cancel) | `discord_giveaways`, drafts, `giveaways` cog | Posting needs the bot: use Discord REST for the post, same pattern as `bot_action`. | Medium |
| A3 | **Auto-post and announcements** | `discord_autopost_config/content`, `discord_scheduled_announcements` | Extends the existing Schedules page; share its validators. | Low |
| A4 | **Server analytics** (joins, leaves, messages, active members, top channels) | `analytics` cog, `discord_guilds`, `discord_xp` | Charts via `barChart`. Needs a daily rollup table if live queries are too heavy. | Medium |
| A5 | **Language** | `language` cog | Small schema module. | Low |
| A6 | **Custom role** | `custom_role` cog | Small schema module. | Low |
| A7 | **Bump settings** | `bump_setup` | Small schema module; respect the kill switch for `bump`. | Low |

Rules for every page: add to `utils/dash_schema.py` (or a page handler for non-settings pages), `_authorised_guild` on every route, per-route rate limit, guild audit row on every write, plus the `_route` docstring and a test using the `env` and `call` helpers.

---

## 3. Part B: Member dashboard

A separate area at `#/me`, never mixed with admin permissions. Any signed-in Discord user can open it; it only ever shows that user's own data.

### B0. Foundation
- [ ] `_require_member(sess)`: any valid session; every query is keyed to `sess.user.id`, never to an id from the client.
- [ ] `GET ?action=me` also returns `member: true` and the user's servers where the bot is present.
- [ ] Rate limits on all member routes. No member route accepts a user id.
- [ ] **Per-user entitlements** (new table `user_entitlements(user_id, product, status, expires_at, source, subscription_id, cancel_at_period_end, created_at)`), products `card_plan`, `dev_monthly`, `dev_yearly`. Status: `active | past_due | cancelled | expired`.
- [ ] **Web-user registry** (new table `dash_web_users(user_id, first_seen, last_seen)`), written on every successful sign-in. Part D depends on it.
- [ ] Bump `SCHEMA_VERSION` 61 to 62 and update the pin in the schema-guard test.
- [ ] Payments: user-bound checkout (`checkout_user`) through the existing `/pay` redirect, **Gumroad (USD) and Paystack (local currency)** both supported. The webhook, not the browser, grants the entitlement. Prices come from the server. **Real small-purchase test on both gateways before shipping.**
- [ ] **Plans renew automatically (decided).** They are real subscriptions, not one-off purchases:
  - **Gumroad:** native subscriptions. The webhook handles the first sale, renewals, cancellation and ended subscriptions; each one moves `expires_at` forward or sets `cancel_at_period_end`.
  - **Paystack:** recurring plans (a Paystack plan per product, with the card authorisation saved by Paystack). Handle `charge.success`, `subscription.create`, `subscription.disable` and failed-invoice events in the existing Paystack webhook.
  - Reuse the `subscription_id` / `auto_renews` approach that server premium already has (`discord_guild_subscriptions`).
  - **Cancel anytime** from My purchases (`cancel_at_period_end`): access continues until `expires_at`, then stops. The cancel call goes to the gateway; the webhook confirms it.
  - **Failed renewal:** status `past_due` with a 3-day grace period, a banner on the page and a Discord DM reminder (only for people who opted in to DMs), then `expired`.
  - Show the next renewal date and price on the page before and after subscribing, and send a receipt for every charge.
  - Duplicate or out-of-order webhooks must not double-extend a plan (idempotent by gateway event id).

### B1. My servers
- [ ] Per server: level, XP, rank, next-level progress, coin balance. Leaderboard preview (top 10).
- [ ] Hide cards for servers that disabled leveling or economy.

### B2. My preferences (one page)
- [ ] Level-up ping opt-out (`leveling_ping_optout`), currency preference (`user_currency_prefs`), AI character and voice-note preference (`ai_prefs`), theme (Part E).
- [ ] Each toggle goes through the same module function the Discord flow uses.

### B3. My purchases
- [ ] Receipts for premium, card plan and developer plan (own `payment_logs` rows only). Referral stats and giveaway entries (read-only).

### B4. Custom level-up card (paid)
- **Price:** **$2 per month**, includes **10 free AI chats per week**.
- [ ] Editor: background, accent colour, font, shape, bio line; live preview via the existing card renderer (`level_card.py`).
- [ ] Gate on the entitlement server-side. Without it the editor opens in preview mode and Save returns 402 with the checkout link.
- [ ] Expiry: the saved design is kept but the bot falls back to the default card; renewing restores it.
- [ ] Validation: colours from a fixed pattern, fonts from an allowlist, backgrounds only from built-in tiers (no uploads in v1).
- [ ] **Free weekly AI chats:** 10 per calendar week (resets Monday 00:00 UTC), counted in a tiny table `user_ai_usage(user_id, week_start, used)`. The 11th chat is refused with the reset time. **They work on the website only (decided)**: no Discord command uses this allowance. They use the bot-provided AI and do not unlock Developer mode.

### Deferred (decided): My collections (Catch, Heist) and My inventory.

---

## 4. Part C: Developer mode

### Access and pricing
- **$5 per month** or **$30 per year**, paid by Gumroad or Paystack.
- **Visible but locked.** The Developer menu item and `#/dev` page always open, for every signed-in user. Without an active subscription the page shows a locked state: what Developer mode includes (read-only preview text, no live widgets), the two prices, and Subscribe buttons. Nothing inside can be used.
- **Enforcement is server-side only.** `GET ?action=dev_status` returns `{unlocked, expires_at, plans}` and works for everyone. Every other `dev_*` route returns `402 {code: "subscription_required"}` without an active entitlement. The locked screen is just UI; it grants nothing.
- Grace on expiry: **export** stays available for 7 days so people can retrieve their files. Chat, connections and GitHub stop immediately; stored keys and tokens are kept (still encrypted) for 30 days in case the person renews, then deleted.

### C1. AI chat
- [ ] Chat page with streaming replies and a model picker.
- [ ] **Chats are not kept in the database.** The conversation lives in the browser while the page is open. "Save chat" sends it as a note file to the storage channel (C2). This keeps the database small and means there is no chat history to leak.
- [ ] Two sources of AI, shown clearly:
  1. **Bot-provided** (the bot's own keys via `ai_store_providers`): **50 messages per week for Developer subscribers (decided)**, same Monday 00:00 UTC reset and same `user_ai_usage` counter (a separate `source` value so the card plan's 10 and the Developer plan's 50 never mix). Website only. Show a usage meter and the reset time.
  2. **Bring your own key** (C3): billed by the provider to the user, not counted against the allowance.
- [ ] Per-user rate limit and daily cost cap; the owner kill switch `ai` (in `admin_controls.FEATURES`) also stops web chat.
- [ ] Reuse bot safety rules (`BOT_RULES` in `ai_features.py`) and the command allowlist/guard modules for any tool use.
- [ ] Render output with `textContent` or a safe markdown renderer; never `innerHTML` on raw text.

### C2. Export and files go to the storage channel, not the database
- [ ] "Export" covers whatever the person makes with their own AI: saved chats and notes (Markdown/JSON/text), generated files, and a list of their connections (provider names only, never keys).
- [ ] **Flow:** the web service builds the file in memory, the bot uploads it to the **storage channel**, and the person gets it back by direct message from the bot and a link on the Export page. Nothing is written to a database table except a minimal receipt row (file name, size, Discord message id, time) so the Export page can list past exports and fetch a fresh link. If you prefer zero rows, the page simply won't list history (open decision).
- [ ] Which channel: one **private storage channel owned by the bot** (config `DEV_STORAGE_CHANNEL_ID`), using the same upload code path as the media wizard. Files are named by an opaque id, not the user's name.
- [ ] **Encrypt before upload** (recommended) with a server-side key so anyone with access to the channel sees only unreadable files; the bot decrypts when delivering to the owner of the file. If you skip encryption, say so on the privacy page.
- [ ] Limits: Discord's per-file upload limit applies (check the current value at build time), a per-user daily export count and size cap, filename sanitising, and the owner kill switch.
- [ ] Discord links expire, so exports are delivered by the bot, not by a permanent public URL.
- [ ] Audit each export (who, name, size).

### C3. Connect your own AI (Claude, Groq, ChatGPT)
- [ ] User pastes an **API key** per provider (Anthropic, Groq, OpenAI). Validate with one cheap test call, then encrypt with `secret_manager.encrypt` and store in `dev_connections(user_id, provider, key_encrypted, last4, created_at)`.
- [ ] **Never return a key.** The UI shows provider, last 4 characters and date. Replace and Remove only. No key in logs, errors, audit rows or exports.
- [ ] Wording: this connects an **API key**, not a Claude.ai, ChatGPT Plus or Groq login. Personal subscriptions are never used.
- [ ] Keys are decrypted only on the server at call time; calls go from our server to the provider, never from the browser.
- [ ] Step-up (fresh Discord sign-in) to add, replace or remove a key. Per-user cap on stored connections and calls per minute.

### C4. Connect GitHub
- [ ] GitHub OAuth App (or GitHub App for finer permissions) with the narrowest scope; read-only first.
- [ ] Token stored encrypted (same pattern as `gdrive_client`), never returned, revocable from the page and at GitHub.
- [ ] v1: pick a repo and attach files or a diff to a chat for review; export a saved chat as a file the person chooses to commit (explicit confirm before any write).
- [ ] No code execution, no deploys, no webhooks in v1. State parameter and PKCE; redirect only to `DASH_PAGES_URL`.

---

## 5. Part D: Member Drop Box and friends

Members can message other members of the same server, but only after a friend request is accepted, and only between people who have signed in to the web dashboard.

### Rules
- **Both people must have used the web** (a row in `dash_web_users`). People who never signed in cannot be found, requested or messaged, and the UI never reveals whether a given Discord user has a web account.
- **Same server:** the sender and recipient must share a server the bot is in. Verified with Discord itself (bot-token member lookup), never from the client. Re-checked when sending a friend request and when sending a message; if they no longer share a server, messaging pauses.
- **Friend request first.** Flow: find a person (search within a shared server, results limited to people who have used the web and allow requests) → send request → recipient accepts or declines in the Drop Box → only then can either side message.
- **Text only** in v1: length cap, no attachments, links shown as plain text and blocked if they match Scam Shield rules.
- **Controls for the recipient:** accept, decline, block, report. Block ends the friendship and prevents new requests. A setting "Who can send me requests" (everyone in my servers / nobody). **Default is on: everyone in my servers (decided)**, and people can switch it off at any time. Because requests are on by default, the abuse limits and block/report tools below are mandatory at launch.
- **Abuse limits:** new requests per day, messages per minute and per day, and a cap on total pending requests per user. Blacklisted users (`admin_controls` blacklist) cannot send or request.
- **Reports** go to a new owner Report queue section with a snapshot of the last messages; the owner can block the sender and remove the friendship.
- **Notifications:** unread count in the dashboard header (like the existing inbox). A Discord DM notification is **opt-in, off by default**, to avoid the bot DM-spamming people.
- **Retention:** messages deleted after 30 days; delete conversation and unfriend at any time. Messages are the one place that must live in the database (they are needed for the inbox); they are small text only.
- **Owner kill switch** `messaging` in `admin_controls.FEATURES`; owner can disable messaging for everyone instantly.

### Tables (one PR, bump `SCHEMA_VERSION`)
- `dash_friends(user_a, user_b, requested_by, status, created_at)` with `user_a < user_b`, unique pair; status `pending | accepted | blocked`.
- `dash_member_messages(id, sender_id, recipient_id, body, created_at, read_at)`.
- `dash_member_reports(id, reporter_id, reported_id, snapshot, status, created_at)`.

### Routes (all member routes; ids come from the session, other party id validated as a snowflake and checked)
`friends_search`, `friend_request`, `friend_respond`, `friend_remove`, `friend_block`, `friends_list`, `messages_thread`, `message_send`, `message_read`, `message_report`.

---

## 6. Part E: Light theme

- [ ] Add a light palette as CSS variables: `:root[data-theme="light"]` plus `@media (prefers-color-scheme: light)` for **Auto**, with a theme control (Auto / Light / Dark) in the header and under My preferences.
- [ ] The choice is stored in `localStorage` under `primebot.dash.theme`. This is a harmless display preference, not a secret; it is set in `dash.js` and applied before first paint to avoid a dark flash. (`owner.js` keeps its "no browser storage" test.)
- [ ] Update `<meta name="color-scheme">` to `light dark` and set it from the chosen theme.
- [ ] Rework the glow effects (`text-shadow`, `box-shadow`, card corner marks) for light backgrounds, and the status colours (`--ok`, `--bad`, `--warn`) for contrast on white. Target WCAG AA for body text.
- [ ] Make the animated background in `bg.js` follow the theme (or turn it into a subtle static tint in light mode).
- [ ] Charts (`owner-chart`) and the owner area read the same variables so they switch with the theme.
- [ ] Image previews rendered by the bot (welcome card, level card) keep their own design; only the page chrome changes.
- [ ] Scope: the dashboard only. The marketing website is a separate decision.

---

## 7. Security requirements (all parts)

- Entitlements are checked **server-side on every request**; only the payment webhook grants or extends them. The locked Developer screen is cosmetic.
- **Secrets:** API keys and GitHub tokens are encrypted at rest, never returned, never logged, never exported; a test asserts no route response, audit row or export contains a stored secret.
- **Member isolation tests:** user A can never read or change user B's connections, preferences, card, messages or friendships.
- **Messaging safety:** same-server and web-user checks on the server, no discovery leak, rate limits, block and report, Scam Shield on message text, owner kill switch.
- **Storage channel:** private channel, opaque file names, encrypted files (recommended), no user-controlled paths, size and count caps. Access to the channel is limited to the bot and the owner; the privacy page says so.
- **Audit:** developer-mode writes (connect, replace, remove, export, GitHub write) and messaging reports go to an audit table; fail closed for key and token changes.
- **Abuse and cost:** per-user and global AI caps, a visible usage meter, owner kill switches (`ai`, `messaging`).
- **Privacy:** update the privacy page before launch (chats are not stored by us unless the person saves them; exports live in a private storage channel; messages deleted after 30 days).
- Keep existing rules: bearer-token sessions, `textContent` only for server data, CORS limited to `DASH_PAGES_URL`.

---

## 8. Phases

### Phase 0: Theme and foundation (can run in parallel)
- [ ] E: light theme (visual only, no schema change)
- [ ] B0: member shell, entitlements table, web-user registry, `SCHEMA_VERSION` 61 to 62, user checkout and webhook grant

### Phase 1: Server panel, low-risk pages (one PR per page)
- [ ] A1 Moderation (read-only), then edit/remove warn
- [ ] A5 Language, A6 Custom role, A7 Bump settings
- [ ] A3 Auto-post and announcements

### Phase 2: Server panel, heavier pages
- [ ] A2 Giveaways
- [ ] A4 Server analytics (rollup table and schema bump if needed)

### Phase 3: Member pages
- [ ] B1 My servers, B2 My preferences, B3 My purchases

### Phase 4: Paid custom level-up card
- [ ] B4 editor, renderer hook, entitlement gate, expiry fallback, 10 free weekly AI chats counter
- [ ] Real small purchase of the $2 plan on Gumroad and on Paystack before release

### Phase 5: Member Drop Box and friends
- [ ] D tables, `messaging` kill switch, requests/accept/block, message send and inbox, reports and the owner Report queue section

### Phase 6: Developer mode core
- [ ] `#/dev` locked page, `dev_status`, `_require_dev`, $5 / $30 checkout
- [ ] C1 AI chat (bot-provided allowance), C2 export to the storage channel

### Phase 7: Developer mode connections
- [ ] C3 bring-your-own keys, C4 GitHub connect

### Phase 8: Polish
- [ ] Usage meters, renewal reminders, delete-my-data, privacy and pricing page updates

---

## 9. Testing

- Permission matrix per route: anonymous 401; not a member of the server 403 (Part A); no entitlement 402 (B4, C); active entitlement 200; expired plan behaves as specified.
- **Locked Developer page:** `dev_status` returns `unlocked: false` for everyone without a plan; every other `dev_*` route returns 402; subscribing flips it only after the webhook.
- Parity tests per feature (Discord and web logic give the same result).
- Member isolation and no-secret-in-responses tests.
- Messaging: cannot message without an accepted friendship; cannot request a person who never used the web; same-server check fails closed; block and rate limits; no leak of whether a user has a web account.
- Entitlement tests: webhook grants, extends and expires; the browser cannot grant. Subscription tests on both gateways: first sale, renewal, failed renewal (past due then expired), cancel at period end, duplicate and out-of-order events do not double-extend.
- Weekly chats: exactly 10 per calendar week on the card plan and exactly 50 on the Developer plan, the next one is refused with the reset time, the counters are separate, and they reset at the week boundary. No Discord command can spend them.
- Export: file goes to the storage channel, nothing but the receipt row is stored, file name is sanitised, size cap enforced.
- Theme: static test that every colour used by components comes from variables, and a contrast check of the light palette.
- Schema guard: every PR adding a table bumps `SCHEMA_VERSION` and the pin in `test_schema_version_guard.py`.
- Static test: no `innerHTML` in new JS; no secrets in browser storage.
- Real-browser test with two Discord accounts and real small purchases before Phases 4, 5 and 6 ship.
- Run: `python3 -m pytest tests/unit/test_dash_*.py tests/unit/test_schema_version_guard.py -q`.

---

## 10. Decisions

**Settled**
1. Developer plan bot-provided AI: **50 messages per week**.
2. Plans **renew automatically** (Gumroad subscriptions and Paystack recurring plans), cancel anytime.
3. Friend requests are **on by default** for people in a shared server who have used the web.
4. The 10 free weekly chats (card plan) work **on the website only**.

**Still open** (the plan states the recommended default)
1. **Export receipts:** keep a minimal row so the page can list past exports (recommended), or no rows at all?
2. **Encrypt exports** before they go to the storage channel (recommended)?
3. **Storage channel:** one central bot-owned private channel (assumed) or a channel per user/server?
4. **Messaging after leaving the shared server:** pause (recommended) or keep going?
5. **GitHub permissions:** read-only first (assumed), or commit/gist writes in v1?
6. **Does the card plan include Developer mode?** Assumed no, separate products.
7. **Refund and proration policy** for the yearly plan and for cancelling mid-period.
8. **Light theme scope:** dashboard only (assumed), or the marketing website too?
9. **Paystack currency:** show the local-currency price converted from USD at checkout (assumed), or fixed local prices?

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Stolen session exposes saved AI keys or a GitHub token | Keys never returned, step-up to change them, encrypted at rest, short sessions |
| Member messaging used for spam, scams or harassment | Friend request first, same-server check, rate limits, block and report, Scam Shield, kill switch, 30-day retention |
| Messaging reveals who uses the web dashboard | Search only returns people who allow requests; identical responses for unknown and unavailable users |
| Files in the storage channel readable by anyone with channel access | Private channel, opaque names, encryption before upload, privacy page disclosure |
| Storage channel hits Discord limits or channel deleted | Size/count caps, owner health alert if uploads fail, config check at startup |
| AI cost overrun | Allowance, per-user daily cap, weekly free limit, global kill switch, usage meter |
| Users believe Claude/ChatGPT subscriptions can be linked | UI and docs say "API key"; subscription logins are never used |
| Entitlement bug gives free access or blocks a paying user | Webhook-only grants, idempotent events, expiry tests, real small purchase on both gateways before launch |
| Renewal charges the wrong amount, double-charges or cannot be cancelled | Cancel button wired to the gateway and confirmed by webhook, receipts for every charge, past-due grace, test renewals and cancellations on both gateways in test mode first |
| Open-by-default friend requests attract spam | Request and message rate limits, block and report, Scam Shield, kill switch, easy opt-out |
| Light theme breaks contrast or leaves dark remnants | Variables only, contrast test, visual pass on every page |
| Schema change not applied in production | Version-bump guard test; check Railway logs after each deploy |
