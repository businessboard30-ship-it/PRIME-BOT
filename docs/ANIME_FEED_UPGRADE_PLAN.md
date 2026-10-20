# Anime feed upgrade plan: big art, buttons for everyone, donghua, personal alerts, survival

Status: PLAN ONLY (no code). **Decisions from the owner are folded in (section 7).** Builds on slices A, B and C (PRs #405 to #407, merged) and the worker fix. Written against what is in `main` today:
`modules/anime_feed.py` (AniList provider, cache, follows), `modules/anime_feed_worker.py` (one shared posting worker),
`discord_bot/cogs/_views_anime_feed.py` (`build_card`, `_FollowButton`, setup wizard). Latest migration is `073`, schema `122`.

## 1. What you asked for

1. **Big anime pictures** (today the cover is a small thumbnail at the side).
2. Buttons **on the posted card itself** (the "spawn"): **Read more, Ping/Alert, Upgrade, Search, Categories** (plus the existing Follow and AniList link).
3. **Everyone can use them, not just admins.** Admin-only things (channel, on/off, ping role) stay in the setup wizard.
4. Each user can **choose to get an alert for a followed episode** (and how).
5. **More donghua** (Chinese animation).
6. **More V2** (Discord Components V2 layouts) and **survival = persistent buttons** (every button keeps working forever, see section 4).

> **Survival means persistent buttons** (confirmed). Section 4 is built around that; general outage handling is kept as a short secondary list.

## 2. The new card (all in Components V2, one persistent message)

```
Container (accent colour)
  [optional ping line: role + up to 10 follower mentions]
  MediaGallery: ONE large image, full width (banner, else big cover)
  TextDisplay: ## Title
               Episode N of M  airs <t:..:F> (<t:..:R>)
               -# Format, genres, origin flag (Anime / Donghua)
               Synopsis (short, ends with "..." when cut)
               -# Source: AniList, times shown in your timezone
  ActionRow 1:  [Read more] [Follow] [Alert me] [Upgrade]
  ActionRow 2:  [Search]    [Categories]        [Details on AniList (link)]
```

**Why this works:** every button is a `DynamicItem` whose `custom_id` holds only the AniList id (e.g. `anime_more:123`), so it keeps working after a restart,
needs nothing stored on the message, and any member can press it. Replies are **ephemeral** (only the presser sees them), so the channel is never spammed.
Component budget: about 12 of Discord's 40 per message, so there is room to grow. The 2-rows-of-4 layout stays inside the 5-buttons-per-row limit.

### 2.1 Big picture
- Add `bannerImage`, `coverImage { extraLarge color }` and `countryOfOrigin` to the AniList query (`_QUERY` in `modules/anime_feed.py`); extend `Preview` with `banner`, `cover_xl`, `origin`.
- Image choice (**wide banner confirmed**): banner if present, else `extraLarge` cover, else no image. Use a `MediaGallery` (full width) instead of the side `Section` + `Thumbnail`.
- A missing or broken image must **never** block a post (fall back, then post text only). Only `https://` URLs are accepted (already enforced).
- Accent colour from AniList's cover `color` when available (fallback to the current accent).

### 2.2 Read more
- Button `anime_more:<id>` replies ephemerally with the **full synopsis** (up to 4,000 chars, paged with Prev/Next if longer), genres, episode count, status, AniList link.
- Fetch by id with a small single-title query, cached 6 hours per id (LRU, bounded). The card keeps its short synopsis so posts stay light.

### 2.3 Follow and Alert me (the personal alerts)
Follow stays as is. **Alert me** opens an ephemeral panel (select menus, no typing) for *this* anime:

| Choice | Meaning |
|---|---|
| Mention me in the server (default) | today's behaviour: mentioned in the post |
| DM me | a DM when the episode airs, with Unfollow and Details buttons |
| Mute this anime | still followed, no alert |

Plus one account-wide setting: **quiet hours** (a timezone select and a start/end hour) so DMs and mentions wait until morning.

Rules that keep this safe:
- **Opt-in only.** Nobody is DMed unless they pressed Alert me and chose DM. Every DM carries a one-tap Unfollow/Mute.
- Closed DMs are counted; after 3 failures in a row the alert mode quietly falls back to mention-only and the user is told once the next time they press the panel.
- DMs use a **bounded queue** (max N per minute, 1 s spacing) so a popular episode with thousands of followers cannot flood Discord.
- Dedupe table `anime_alerts_sent (user_id, anime_id, episode)` so restarts never double-DM.
- Important lifecycle change: the worker's "no feeds on = fully idle" rule must also treat **"someone has a DM alert"** as work, otherwise DM-only followers would get nothing. It still sleeps until the next due episode and still makes **no provider request when there is nothing to do**.

### 2.4 Upgrade button (two separate "brains": server premium vs personal upgrade)
The rule:
- **Server is premium** -> every member of that server gets all the exclusive features. The Upgrade button says so ("This server is premium, you already have everything") and offers nothing to buy.
- **Server is not premium** -> each **user** can buy a **personal upgrade** to unlock the exclusive features **for themselves**. It reuses the **same payment system** (same gateway, same checkout/verify pattern) but it is a **different entitlement**: it never makes the server premium and never touches the guild premium tables.

How it is built:
- Check = `has_anime_plus(user_id, guild_id, clone_id)`: **true if the server is premium (`db.is_guild_premium_active`, same cached lookup as `modules/antiraid_pro.is_premium`) OR the user has an active personal entitlement.** One function, used by every gated button.
- New table `anime_plus_entitlements` (user id, clone id, source payment reference, status, started/expires) plus a payments table following the existing per-user unlock precedent (`discord_image_search_unlock_payments` and the `image_search_unlock` case in `api/paystack_webhook.py`): payment row created at checkout, **completed by the webhook server-to-server** so a restart between paying and tapping Verify never loses a paid upgrade.
- The personal upgrade follows the person **across servers** (it is theirs), but only applies to features that make sense per person (follow limit, DM alerts, quiet hours, digest). Server-level features (channel, ping role, content kinds) stay with the server's own premium or admin settings.
- Upgrade card (ephemeral): shows which case applies (server premium / personal active until X / not upgraded), the list of exclusive features, and a Pay button. Prices and duration are config values, not hard-coded.
- Lapse behaviour (same rule as anti-raid): saved choices are kept but paused; following beyond the free limit is not deleted, new follows are blocked until under the limit or upgraded again.
- Exclusive features to gate (initial proposal, change freely): more than 5 follows, DM alerts, quiet hours, "my follows" digest, Categories "Top picks" ordering. Free users keep Follow (5), mention alerts, Read more, Search and basic Categories.

### 2.5 Search button
- Opens a **modal** with one text field. Result: an ephemeral paged browser (same big card style, Prev/Next) with Follow / Alert me / Read more on each result.
- Provider: AniList `Page(media(search: $q, type: ANIME, isAdult: false, sort: SEARCH_MATCH))`, max 10 results.
- Protection: per-user cooldown (10 s), query length cap (60 chars), results cached 10 minutes per normalised query, shared by everyone. AniList allows about 90 requests/minute, so searches share one global limiter with the posting worker, and the **worker has priority**.

### 2.6 Categories button
- Button opens an ephemeral **category picker** (a select menu plus quick buttons), then a paged browser of big cards with Follow / Alert me / Read more.
- **Starter categories (curated, with emoji):**
  1. Love (Romance)
  2. Sci-Fi
  3. **Donghua** (all Chinese animation; the dedicated shortcut)
  4. Action
  5. Fantasy / Isekai
  6. Comedy
  7. Slice of Life
  8. Horror / Thriller
  9. Sports
  10. Mecha
  11. Mystery
  12. Cultivation / Xianxia (donghua-flavoured, tag search)
  13. New this season
  14. Movies
- Each category is a row in a small config table in code (`CATEGORIES = [(key, label, emoji, genre_in, tag_in, origin)]`), so adding "and more" later is a one-line change, no migration. Genre/tag names are AniList's own (`Romance`, `Sci-Fi`, ...), so no mapping is invented.
- Query: `media(status: RELEASING, genre_in / tag_in, countryOfOrigin, sort: POPULARITY_DESC)` with `nextAiringEpisode`; cached 15 minutes per category. Adult titles excluded. An empty result says so plainly.
- Free: all categories. Gated for personal/server upgrade (proposal): the "Top picks" sort and saving a favourite category for a weekly digest.

## 3. More donghua

Today the feed query has **no origin filter**, so donghua only appear by accident and can be crowded out. Plan:
1. `Preview.origin` from AniList `countryOfOrigin` (`CN` = donghua, `JP` = anime, `KR` = manhwa adaptations) shown on the card as a tag.
2. A second cached airing fetch for `countryOfOrigin: CN` so donghua are never crowded out by the larger Japanese schedule (the 50-item cap from PR #411 applies per fetch).
3. **Server setting (admin, in the wizard):** *Post: Anime / Donghua / Both*. **Donghua is ON by default, including for existing servers** (confirmed): the migration default is `both`, so every server already using the feed starts receiving donghua with no admin action; an admin can switch to Anime-only in the wizard.
4. **Per-user:** Follow and Alert me work identically for donghua. Categories has a Donghua origin option, and a dedicated **Donghua** shortcut in Search results.
5. Title display for CN titles: English, else romaji, with the native title in the "Read more" panel.
6. **Honest limit:** AniList's donghua coverage and airing times are partial and often lag Chinese platforms. The card only claims what AniList reports (`Source: AniList`). A second donghua source is **not** planned because there is no clean public schedule API; scraping streaming sites is out of scope (see non-goals).

## 4. Survival = persistent buttons

Goal: **a button on any card, however old, still works after any restart, deploy or crash**, for every user, with no state lost.

| Rule | How |
|---|---|
| No in-memory view state | Every button is a `DynamicItem` with a regex `custom_id` template, rebuilt from the `custom_id` alone (`anime_more:<id>`, `anime_follow:<id>`, `anime_alert:<id>`, `anime_up:<id>`, `anime_search`, `anime_cat:<key>`). Nothing is stored on the message or in RAM. |
| Registered at startup | All templates are in `DYNAMIC_ITEMS`; the cog registers them with `bot.add_dynamic_items(...)` in `setup_hook` for **every bot process, including each clone bot** (they have their own token and gateway). A test asserts every button class on a card is in `DYNAMIC_ITEMS`. |
| Views never time out | Posted cards use `LayoutView(timeout=None)`. |
| Ephemeral browsers survive restarts | Pages, selects and search results encode their state in the `custom_id` (for example `anime_page:<cat>:<n>`) or re-query the 15-minute cache, so pressing Next on a browser opened before a restart still works. Anything too big for 100 characters is stored in a small DB row keyed by a short token (TTL 24 hours). |
| Stable ids across versions | `custom_id` formats are **versioned and never reused**: new features add new prefixes; old prefixes keep a handler (or reply "this button is old, tap the card's new buttons" instead of "This interaction failed"). |
| Old cards after the layout upgrade | Cards posted before D1 only have Follow/AniList; those keep working. New buttons simply appear on new posts. Optional later: edit recent cards in place. |
| Always answer | Every callback defers first (3-second rule) and catches errors into a friendly ephemeral message. |
| Paid upgrades survive restarts | Personal upgrades are completed by the payment **webhook** (section 2.4), not by a button a user must remember to tap. |
| Guard rail tests | Restart simulation: build a card, throw away the view, rebuild items from `custom_id` only, press each one. |

Secondary resilience (kept short): worker survives crashing passes (already merged), 8 s provider timeout, stale-cache fallback when AniList is down, `Retry-After` handling with a shared limiter (posting has priority), per-user cooldowns on Search/Categories, bounded LRU caches, kill-switch flags `ANIME_FEED_ENABLED` / `ANIME_ALERTS_ENABLED`, and per-pass counters in the logs.

## 5. Data changes (all additive; numbers continue from `073` / schema `122`)

- `074_anime_alerts.sql`: `anime_follows.alert_mode TEXT NOT NULL DEFAULT 'mention'` (`mention` / `dm` / `off`); `anime_user_prefs (user_id PK, tz TEXT, quiet_start SMALLINT, quiet_end SMALLINT, dm_failures SMALLINT DEFAULT 0, updated_at)`; `anime_alerts_sent (user_id, anime_id, episode, sent_at, PRIMARY KEY (user_id, anime_id, episode))`.
- `075_anime_feed_kinds.sql`: `anime_feed_config.content_kinds TEXT NOT NULL DEFAULT 'both'` (donghua on for everyone by default).
- `076_anime_plus.sql`: `anime_plus_entitlements (user_id, clone_id, payment_reference, status, started_at, expires_at)` and `anime_plus_payments` (same shape as the existing per-user unlock payments table, `payment_reference` UNIQUE), with webhook case `anime_plus` added in `api/paystack_webhook.py`.
- Each migration bumps `SCHEMA_VERSION` (123, 124, 125) and re-pins the hash in `tests/unit/test_schema_version_guard.py` (the guard fails otherwise).
- Privacy: only user id, AniList id, title, chosen mode, timezone and payment reference. Unfollow deletes the row; a "Delete my anime data" button removes prefs and follows (payment records are kept as required for billing).

## 6. Delivery in slices (each its own small PR, merged in order)

| Slice | Content | Migration |
|---|---|---|
| **D1** | Big banner image (MediaGallery, cover fallback), accent colour, **Read more**, 2-row layout, origin tag, **persistence test** | no |
| **D2** | **Alert me** panel, per-user alert mode (mention / DM / mute), quiet hours, DM queue + dedupe, worker lifecycle change | 074 |
| **D3** | **Search** (modal + browser) and **Categories** (curated list above), shared limiter and caches | no |
| **D4** | **Donghua**: origin fetch, server setting (default Both), Donghua category | 075 |
| **D5** | **Upgrade**: `has_anime_plus()`, personal entitlement + payment + webhook, gating of exclusive features | 076 |
| **D6** | Persistence hardening and ops: old-button fallbacks, token store for big browser state, kill switches, counters | no |

D1 is the visible win with no data risk. D2 comes before D3 so the new entry points call finished alert code. D5 is last among features because it touches payments.

## 7. Decisions (answered)

1. **Survival** = persistent buttons. Done (section 4).
2. **Upgrade** = server premium means no upgrade needed; otherwise each user buys a personal upgrade through the same payment system, as a separate entitlement that never makes the server premium (section 2.4).
3. **DM alerts**: yes, the user chooses (mention / DM / mute) per anime.
4. **Image**: wide banner, cover as fallback.
5. **Donghua**: on by default, existing servers included.
6. **Categories**: Love, Sci-Fi, Donghua and more (list in 2.6).

**Still open (small, can be decided at D5):** personal upgrade price and duration (monthly vs one-time), the exact list of features that are exclusive, and whether the personal upgrade should also unlock anything on the server side (plan says no).

## 8. Tests (per slice)

- Pure functions (card text, image choice, quiet-hours window, follow limit, query building) are unit tested with no network, like `test_anime_feed_*`.
- Button callbacks: defers before any DB work; ephemeral reply; cooldown enforced; non-admins allowed, admin-only wizard still gated.
- Worker: DM-only follower wakes the worker; no followers and no feeds = no timer and no requests; dedupe across restart; closed-DM auto-pause; breaker opens and closes.
- Persistence: build the card, rebuild `DynamicItem`s from `custom_id` alone (restart simulation).
- Schema guard re-pinned in every migration PR. Full suite compared against main's known failures before each merge.

## 9. Non-goals

- No hosting, streaming or links to pirate/streaming sites; only AniList metadata and AniList links.
- No adult titles (filtered at the provider and re-checked in Search/Categories).
- No scraping of Chinese platforms; donghua coverage is whatever AniList has.
- No change to admin-only setup (channel, on/off, ping role): those stay in the wizard.
- No game or economy changes.
