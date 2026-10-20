# Anime feed upgrade plan: big art, buttons for everyone, donghua, personal alerts, survival

Status: PLAN ONLY (no code). Builds on slices A, B and C (PRs #405 to #407, merged) and the worker fix. Written against what is in `main` today:
`modules/anime_feed.py` (AniList provider, cache, follows), `modules/anime_feed_worker.py` (one shared posting worker),
`discord_bot/cogs/_views_anime_feed.py` (`build_card`, `_FollowButton`, setup wizard). Latest migration is `073`, schema `122`.

## 1. What you asked for

1. **Big anime pictures** (today the cover is a small thumbnail at the side).
2. Buttons **on the posted card itself** (the "spawn"): **Read more, Ping/Alert, Upgrade, Search, Categories** (plus the existing Follow and AniList link).
3. **Everyone can use them, not just admins.** Admin-only things (channel, on/off, ping role) stay in the setup wizard.
4. Each user can **choose to get an alert for a followed episode** (and how).
5. **More donghua** (Chinese animation).
6. **More V2** (Discord Components V2 layouts) and **survival** (see assumption below).

> **Assumption to confirm:** "survival" is read as *resilience*: the feed and its buttons keep working through bot restarts, AniList outages, rate limits and bad data. If you meant a survival *game mode* (like the economy/cat games), tell me and this section is replaced.

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
- Image choice: banner if present, else `extraLarge` cover, else no image. Use a `MediaGallery` (full width) instead of the side `Section` + `Thumbnail`.
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

### 2.4 Upgrade button
- Ephemeral card: current plan, current limits (free: 5 followed anime), what premium adds, and a button to the existing premium flow (`discord_bot/cogs/guild_premium.py` and `_views_premium_v2.py`).
- Suggested premium extras (to decide, section 7): more follows (for example 50), DM alerts for more anime, custom quiet hours per anime, a "my follows" digest.
- `FREE_FOLLOW_LIMIT` becomes a lookup (`follow_limit(user)`), not a constant. The comment in `toggle_follow` already reserves this.

### 2.5 Search button
- Opens a **modal** with one text field. Result: an ephemeral paged browser (same big card style, Prev/Next) with Follow / Alert me / Read more on each result.
- Provider: AniList `Page(media(search: $q, type: ANIME, isAdult: false, sort: SEARCH_MATCH))`, max 10 results.
- Protection: per-user cooldown (10 s), query length cap (60 chars), results cached 10 minutes per normalised query, shared by everyone. AniList allows about 90 requests/minute, so searches share one global limiter with the posting worker, and the **worker has priority**.

### 2.6 Categories button
- Ephemeral select menus: **Genre**, **Format** (TV, Movie, ONA, OVA), **Origin** (Japan, China/donghua, Korea) and **When** (airing today, this week, new this season).
- Query: `media(status: RELEASING, genre_in, countryOfOrigin, format, sort: POPULARITY_DESC)` with `nextAiringEpisode`; paged browser with the same buttons.
- Cached 15 minutes per combination. Filters are combined as AND; an empty result says so plainly (never invents entries).

## 3. More donghua

Today the feed query has **no origin filter**, so donghua only appear by accident and can be crowded out. Plan:
1. `Preview.origin` from AniList `countryOfOrigin` (`CN` = donghua, `JP` = anime, `KR` = manhwa adaptations) shown on the card as a tag.
2. A second cached airing fetch for `countryOfOrigin: CN` so donghua are never crowded out by the larger Japanese schedule (the 50-item cap from PR #411 applies per fetch).
3. **Server setting (admin, in the wizard):** *Post: Anime / Donghua / Both* (default Both). Stored as `anime_feed_config.content_kinds` (additive column).
4. **Per-user:** Follow and Alert me work identically for donghua. Categories has a Donghua origin option, and a dedicated **Donghua** shortcut in Search results.
5. Title display for CN titles: English, else romaji, with the native title in the "Read more" panel.
6. **Honest limit:** AniList's donghua coverage and airing times are partial and often lag Chinese platforms. The card only claims what AniList reports (`Source: AniList`). A second donghua source is **not** planned because there is no clean public schedule API; scraping streaming sites is out of scope (see non-goals).

## 4. Survival (resilience)

| Risk | Plan |
|---|---|
| Bot restarts | Buttons are persistent `DynamicItem`s registered at startup (extend `DYNAMIC_ITEMS`); all state is in Postgres; claim-before-send already prevents double posts; DM alerts use the same pattern. |
| AniList down or slow | 8 s timeout, no retry storm (existing); serve **stale cache** with a "last updated" note; exponential backoff with jitter (existing); a **circuit breaker** stops hitting a failing provider for 5 minutes and posts a single log line. |
| Rate limit (HTTP 429) | Honour `Retry-After`; one global limiter shared by worker, Search and Categories; user-facing features degrade first (clear message), posting keeps priority. |
| Slow database | Every button callback **defers first** (3-second rule) then edits, the same fix as the League timeout PR. |
| One bad pass | Worker `run()` already survives a crashing cycle (backs off instead of dying). Add a **watchdog**: if the task is done/cancelled unexpectedly, the cog restarts it and logs. |
| Bad or missing data | Parser never raises (existing); missing image falls back, never blocks a post; component-limit guard trims text rather than failing the send. |
| Spam / abuse | Per-user cooldowns on Search, Categories, Alert changes; modal input sanitised; ephemeral replies; `AllowedMentions` explicit (existing). |
| Memory growth | All caches are bounded LRUs with TTLs (details, search, categories). |
| Runaway DMs | Bounded queue, global per-minute cap, per-user failure auto-pause. |
| Bad deploy | Migrations additive only; a **kill switch** (`ANIME_FEED_ENABLED` and `ANIME_ALERTS_ENABLED` config flags) turns posting or DM alerts off without a redeploy of code paths. |
| Visibility | Counters logged per pass (posted, skipped, provider errors, DM sent/failed, breaker state) and shown to the bot owner in the existing owner/admin status page. |

## 5. Data changes (all additive; numbers continue from `073` / schema `122`)

- `074_anime_alerts.sql`: `anime_follows.alert_mode TEXT NOT NULL DEFAULT 'mention'` (`mention` / `dm` / `off`); `anime_user_prefs (user_id PK, tz TEXT, quiet_start SMALLINT, quiet_end SMALLINT, dm_failures SMALLINT DEFAULT 0, updated_at)`; `anime_alerts_sent (user_id, anime_id, episode, sent_at, PRIMARY KEY (user_id, anime_id, episode))`.
- `075_anime_feed_kinds.sql`: `anime_feed_config.content_kinds TEXT NOT NULL DEFAULT 'both'`.
- Each migration bumps `SCHEMA_VERSION` (123, 124) and re-pins the hash in `tests/unit/test_schema_version_guard.py` (the guard fails otherwise).
- Privacy: still only user id, AniList id, title, chosen mode and timezone. Unfollow deletes the row; a "Delete my anime data" button in the Alert panel removes prefs and follows.

## 6. Delivery in slices (each its own small PR, merged in order)

| Slice | Content | Needs migration |
|---|---|---|
| **D1** | Big image (MediaGallery, banner/cover fallback), accent colour, **Read more**, 2-row button layout, origin tag | no |
| **D2** | **Alert me** panel, per-user alert mode, quiet hours, DM queue + dedupe, worker lifecycle change | 074 |
| **D3** | **Search** (modal + browser) and **Categories** (selects + browser), shared limiter and caches | no |
| **D4** | **Donghua**: origin fetch, server setting, Categories origin option | 075 |
| **D5** | **Upgrade** button + `follow_limit()` plans, premium extras | no (uses premium tables) |
| **D6** | **Survival** hardening: circuit breaker, stale-cache note, watchdog, kill switches, status counters | no |

Order rationale: D1 is the visible win and has no data risk; D2 is the largest behaviour change so it ships before more entry points (D3) call into it.

## 7. Decisions needed from you

1. **Survival** = resilience, as assumed? (Section 1.)
2. **Premium vs free:** which of these should be premium: more than 5 follows, DM alerts, quiet hours, digest? Is premium **per server** (like anti-raid today) or **per person**? Follow limits are per person, so a per-person plan is cleaner but needs a user-level premium lookup.
3. **DM alerts** at all, or mentions only? (DMs are opt-in and rate-limited, but they are the riskiest part.)
4. Image preference: **banner** (wide, 1900x400-ish, sometimes missing) or **tall cover** (always present, less wide)? The plan is banner-first with cover fallback.
5. Donghua default for existing servers: leave **Both** or keep current behaviour until an admin opts in?

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
