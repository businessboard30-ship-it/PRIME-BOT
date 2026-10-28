# Card economy, collection & trading: handoff

Spec: `PRIME-BOT_Final_Economy_Cards_Prompt.md` (owner's brief). Rules that override everything: **no new commands, pure
Components V2, persistent views that survive restarts, no timeout on permanent screens.** Open a PR per slice; check
`git log` / open PRs first because more than one AI works on this.

## Built and merged
| PR | What |
|----|------|
| #496-#500 | Backend only: global store + limited stock + per-copy ownership + coin ledger (`modules/card_economy.py`), boosts + items + skills (`card_boost.py`), global/local trading + coin transfers (`card_trade.py`), coin flip / league flip / rob (`card_games.py`), profiles + browser + achievements + leaderboards (`card_profile.py`). Migrations 088-092. Real-Postgres tests. |
| #501 | Wired migrations 088-092 into `database._create_tables`. |
| #503 | Card Hub UI (Components V2, persistent): Home, Store, My cards + detail + boost, Profile, Achievements, Rankings, entry = "Cards" button on the XP leaderboard. `discord_bot/cogs/_views_card_hub.py`. |
| trade UI | Migration 094 (`card_trade_drafts`). `discord_bot/cogs/_views_card_trade.py`: hub screen "💱 Trade & games" = open offers (accept / decline / cancel), global + local trade builder (draft stored in DB), coin transfer with a confirm step, Coin Flip / League Flip / Rob. Recipient gets a DM with persistent Accept / Decline. Also fixed a backend exploit: wager games only checked funds on a loss, so a 0-coin player could win free coins (`card_games._wager_game` now checks the stake before the roll). |
| chief seats | Migration 095 + `card_chief.py` + hub screen, see item 4 below. |
| economy wiring | Migration 093. Level-ups pay coins once per person per level, bot-wide (`card_rewards.py`, knobs in `config.py`, hooked in `cogs/leveling.py`). `cogs/card_hub.py` sweeps expired rentals + trade offers every 5 min (main bot only). |

## Not built yet (nothing below has any UI)
1. ~~Card Hub screens~~ built (#503).
2. ~~Trading + coin transfer + games UI~~ built (see table). Recipient entry uses `resolve_user_ref` (same ID rules and lookup rate limit as the Cat Game box).
3. ~~Boost items~~ now drop at milestone levels (every 5th Shard, 25th Core, 50th Apex; once per person per level, in the same transaction as the coin payout; `card_rewards.item_for`, switch `CARD_LEVELUP_ITEMS_ENABLED`). Still no quests / admin grants. Catch-up caps at 20 levels per call, so a huge jump skips older drops (same as coins).
4. ~~Chief Seat purchase with coins~~ built: hub screen "👑 Chief Seats" (`_views_card_chief.py`, rules in `modules/card_chief.py`, migration 095). A purchase is a **7-day lease** (`CHIEF_SEAT_LEASE_DAYS`) at a per-seat price (`CHIEF_SEAT_PRICES`, `config.py`). It can displace an XP-earned chief but never someone else's running lease; one seat per person; renewals can't run past `CHIEF_SEAT_MAX_LEASE_DAYS` (28) from now; buyer needs `CHIEF_MIN_LEVEL` in that server. `database.recompute_clan_chiefs` skips leased seats and, when a lease ends, keeps the holder only if they are also in the XP top 5 (otherwise the seat is released and reported like any other loss, `new_user_id=None`). Expiry is processed lazily on the next XP message in that server (no new background task). Not covered: no refund/admin-revoke tool, prices not yet in XP Options (item 6).
5. ~~Admin tooling~~ built: hub screen "🛠️ Card admin" (`_views_card_admin.py`), owner-only (`DISCORD_CLONE_ADMIN_IDS`, re-checked server-side on every click, nav entry hidden for others). Create unlimited/limited cards, add/re-price permanent + rental offers, add stock, hide/show. Not covered: editing level gates / tradeable flag / image after creation, grant-item and grant-card tools, restock history view.
6. XP Options integration for per-server economy settings (today the level-up coin knobs live in `config.py`).
7. Restart-persistence and long-idle behaviour must be tested for real before claiming it works. Tests so far rebuild every control from its custom_id alone (what discord.py does after a restart) and check `timeout=None` / `is_persistent()`; nobody has restarted a real bot and clicked an old message yet.
8. Not built in the trade UI: card-for-card trades only list 25 cards per page and add one card per pick (max 5 per side); there is no "view card detail" inside a draft; the trade-restriction setting for limited editions is only the catalog `tradeable` flag.

## Gotchas
* `cogs/cards.py` is the OLDER card system (Card Coins packs / marketplace, legacy embeds, 180 s view timeout). The new
  economy shares its `card_coins` balance and `card_catalog` table but is separate; do not break it.
* Changing `database._create_tables` needs a `SCHEMA_VERSION` bump AND new pins in `tests/unit/test_schema_version_guard.py`.
* There is no "sleep / inactivity" system in this repo; background work is kept to one cheap indexed sweep.
* Tests: `pip install pgserver asyncpg pytest-asyncio`, then `pytest tests/integration/test_card_*_postgres.py`.
  `tests/unit/test_premium_price_copy.py` already fails on main.
