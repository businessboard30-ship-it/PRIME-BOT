# Anime feed: handoff for the next Claude

Repo: `github.com/businessboard30-ship-it/PRIME-BOT` (Discord bot, Python, discord.py 2.7.1, Postgres via asyncpg).
Owner style: wants things built and merged, short plain updates, tells you decisions in chat. Owner writes in caps and informally; read intent, not typos.
Written 2026-10-20. Read `docs/ANIME_FEED_UPGRADE_PLAN.md` next: it is the source of truth for what to build.

## 0. Status update (2026-10-20, later) - read this first; sections 1 and 4 below are the older picture

- **D1 to D5 are all merged into `main`** (#411, #412, #413 and the D2-D5 PRs through #415). Schema is `125`.
- **Subscription (PR #417)**: the personal upgrade is now a monthly **subscription** (`anime_plus` is a 4th plan in `modules/user_subs.py`, same system as `card_plan` and Developer mode: Paystack recurring plan for Ghana, Gumroad membership elsewhere, existing webhooks do charge/renew/cancel/fail/end). The old one-off tables stay readable so already-bought days are honoured.
- **D6 (this PR)**: `ANIME_FEED_ENABLED` / `ANIME_ALERTS_ENABLED` kill switches (env, default on, read each pass), one INFO log line per busy pass (`servers/due/posted/dms/dm_backlog/took`), and a safety net that answers old or unmatched `anime_*` buttons privately instead of "interaction failed" (`RETIRED_FAMILIES` is where a removed feature's id family goes). The "DB token store" from the plan was **not needed**: every browser state already fits in a 100-character `custom_id`.
- **Still open for the owner**: set the real price (`ANIME_PLUS_FEE_USD`), confirm the Paystack plan was auto-created, create/adopt the Gumroad membership named "Anime Feed Upgrade - Monthly". Nothing here was tested against live Discord, AniList, Paystack or Gumroad.

## 1. Where things stand

**Already merged into `main`**
- #408 cat bulk care in one transaction, #409 League tap timeouts, #410 join verification (captcha default + premium extras; its migration was renumbered to `073`, schema `122`).
- #407 anime slices A+B+C (setup wizard, AniList previews, automatic posting worker, follow button, ping role). #405 and #406 were closed because #407 contained them. A fix was added on #407 before merge: `Worker.run()` now survives a crashing cycle (backs off instead of dying), with a test.

**Open PRs (none merged yet, owner has not said to merge them)**
| PR | Branch | What |
|---|---|---|
| #411 | `fix/anime-feed-cap` | `parse_airing` kept only 12 airings so the worker could miss just-aired episodes. Now keeps `MAX_FETCH = 50`; only previews are capped at 12 via `af.for_preview`. Has a test. Main already merged into it. |
| #412 | `docs/anime-upgrade-plan` | `docs/ANIME_FEED_UPGRADE_PLAN.md` (the plan, owner decisions folded in) plus this handoff. Docs only. |
| #413 | `feat/anime-d1-big-banner` | **D1**, the first slice of the plan (details in section 3). Full suite: 4973 passed, 4 failed (the known 4). |

#411 and #413 both edit `modules/anime_feed.py` (different hunks, should auto-merge; verify, then rerun anime tests). Suggested order: merge #411, #412, #413, then start D2. **Ask the owner before merging** (they asked for merges on the first review task, but #411-#413 are new work).

## 2. Owner decisions (all confirmed in chat)

1. "Survival" = **persistent buttons**: every button must keep working on any old card after restarts/deploys (DynamicItems, state only in the `custom_id`/DB).
2. **Upgrade model ("two brains")**: if the SERVER is premium, nobody needs to upgrade (all exclusive features on). If the server is not premium, each USER can buy a **personal monthly upgrade** using the same payment system, stored as a separate entitlement that never makes the server premium. One check: `has_anime_plus(user, guild, clone) = server premium OR user entitlement`.
3. **DM alerts**: yes, user chooses per anime: mention in server / DM / mute. Opt-in only.
4. **Image**: wide banner, big cover as fallback.
5. **Donghua on by default**, existing servers included (`content_kinds` default `both`).
6. **Categories** (curated list, one-line edits to add more): Love, Sci-Fi, Donghua, Action, Fantasy/Isekai, Comedy, Slice of Life, Horror, Sports, Mecha, Mystery, Cultivation/Xianxia, New this season, Movies.
7. **Personal upgrade is monthly.** Exclusive features accepted: more than 5 follows, DM alerts, quiet hours, "my follows" digest, Categories "Top picks" sort. Price is a config value, still to be set by the owner.
8. Buttons appear **on the posted card** and any member can use them; replies are ephemeral. Channel/on-off/ping role stay admin-only in the wizard.

## 3. What D1 (#413) contains

- `modules/anime_feed.py`: `Preview` gained `banner`, `cover_xl`, `color`, `origin` and an `image` property (banner, else big cover, else cover). Query now asks for `bannerImage`, `countryOfOrigin`, `coverImage{extraLarge color}`, `title.native`. New: `origin_label`, `parse_detail`, `get_detail` (6 h cache, max 300, serves an old copy if AniList fails), `cooldown_left` (bounded per-user cooldown).
- `discord_bot/cogs/_views_anime_feed.py`: `build_card` uses one `MediaGallery` (no Thumbnail/Section), accent from cover colour; new persistent `_MoreButton` (`anime_more:<id>`: defers ephemeral first, then followup with `detail_view`); `DYNAMIC_ITEMS = (_FollowButton, _MoreButton)`. Registration is automatic: `_views_join_dm.DYNAMIC_ITEMS` imports the anime list and `discord_bot/bot.py` registers it.
- Tests: `tests/unit/test_anime_feed_d1_big_banner.py` (includes the restart/persistence check).

## 4. What to build next (slices in order, see plan section 6)

- **D2 Alert me** (migration `074_anime_alerts.sql`, schema 123): `anime_follows.alert_mode`, `anime_user_prefs` (tz, quiet hours, dm_failures), `anime_alerts_sent` (dedupe). Button `anime_alert:<id>` opens an ephemeral panel with selects. DM queue is bounded (per-minute cap, 1 s spacing), 3 closed-DM failures falls back to mention. **Worker lifecycle change**: "no feeds on = idle" must also count "someone has a DM alert" (see `Worker.cycle` in `modules/anime_feed_worker.py`; `followers_of` currently feeds mentions only).
- **D3 Search + Categories** (no migration): modal search and curated category picker, shared AniList limiter (about 90 req/min; the posting worker has priority), caches, per-user cooldowns. Categories list lives in code.
- **D4 Donghua** (migration `075`, `content_kinds` default `both`): second airing fetch with `countryOfOrigin: CN`, admin wizard setting, Donghua category. AniList's donghua coverage is partial; do not scrape other sites.
- **D5 Upgrade** (migration `076`): `has_anime_plus`, `anime_plus_entitlements` + payments table, webhook case `anime_plus`. Copy the existing per-user unlock precedent: `discord_image_search_unlock_payments` and the `image_search_unlock` case in `api/paystack_webhook.py`; server premium check is `modules/antiraid_pro.is_premium(guild_id, clone_id)` (30 s cache, fails safe to False). Gate: follow limit via a `follow_limit(user)` function replacing the `FREE_FOLLOW_LIMIT` constant in `toggle_follow`.
- **D6 Persistence hardening**: old-button fallbacks, DB token store for state too big for 100-char custom_ids, kill switches (`ANIME_FEED_ENABLED`, `ANIME_ALERTS_ENABLED`), per-pass counters in logs.

Card layout target (when more buttons arrive): row 1 `Read more, Follow, Alert me, Upgrade`; row 2 `Search, Categories, Details on AniList (link)`. Today it is one row: `Read more, Follow, Details on AniList`.

## 5. Gotchas learned the hard way

- **Always `git fetch origin` before branching.** A stale local `origin/main` once produced a branch missing #410.
- **Migrations/schema**: latest is `073` / `SCHEMA_VERSION = "122"` in `database.py` (the version line and the `073` loader block). Every migration PR must bump the version, add a loader block in `_create_tables`, add a comment line, and re-pin `PINNED_VERSION` and `PINNED_HASH` in `tests/unit/test_schema_version_guard.py`. The hash is a sha256 (first 16 hex) of the whitespace-normalised, comment-stripped body of `_create_tables`; compute it with the same code as the test's `_current()`. Two PRs claimed `069` earlier: check the migrations folder and main before picking a number.
- `DynamicItem` wraps a `Button`: in tests use `k.item` for label/style; `custom_id` is on the DynamicItem.
- Components V2: all TextDisplays in one message share a 4,000-char limit; max 40 components; 5 buttons per row; a Container accent is `discord.Color`.
- Every button callback must defer or answer within 3 seconds, then edit/followup. Use `AllowedMentions` explicitly (never `@everyone`).
- `modules/anime_feed.py`: AniList only, adult titles filtered, https-only image URLs, times as `<t:..>` tags, nothing invented when data is missing.
- Merge method used on this repo: merge commits (`merge_method: merge`). Stacked-PR rule used before: merge the top one, close the lower ones.

## 6. Test baseline and how to run

- Main has **4 failing tests** (pre-existing, not caused by anime work): `test_ultra_trial_off::test_new_ultra_trials_are_off` and 3 in `test_uncached_member_fixes`. CI's `test` check is therefore red on every PR; compare failures against this list rather than expecting green. (`test_cat_primole_phase7` also failed until #410, which fixed it.)
- Setup: `pip install -r requirements.txt pytest pytest-asyncio --break-system-packages`.
- Full suite takes about 4 minutes alone (10 minutes when three run in parallel). The tool call limit is 300 s, so run it detached: `setsid nohup bash -c "python3 -m pytest tests -q -p no:cacheprovider > /tmp/full.log 2>&1; echo done > /tmp/done" >/dev/null 2>&1 &` in a `git worktree`, then poll. A plain background `nohup` died when the tool call ended.
- Fast loop: `pytest tests/unit/test_anime_feed_*.py tests/unit/test_schema_version_guard.py -q`.
- Some command output contains non-UTF-8 bytes; pipe through `iconv -c -t ascii` or redirect to a file if a tool call errors on encoding.

## 7. Credentials and process

- The owner pasted a GitHub personal access token in chat to work via the REST API (`api.github.com`) and `git clone https://x-access-token:<token>@github.com/...`. **It is not stored anywhere and must not be written into the repo, docs or memory.** It was flagged as exposed and the owner was asked to revoke it. In a new session, ask the owner for a fresh token (repo scope) before touching GitHub.
- Sandbox network allows `github.com`, `api.github.com`, `pypi.org`; AniList (`graphql.anilist.co`) is not reachable, so nothing has been tested live. Say so in PRs ("live Discord/AniList untested").
- Working style that worked: review, run the full suite and compare to the baseline, push fixes to the PR branch when needed (tell the owner), open small PRs, report plainly what was and was not verified.

## 8. Open items for the owner

1. Merge #411, #412, #413? (and then continue with D2.)
2. Monthly price for the personal upgrade (config value).
3. Revoke the exposed GitHub token.
