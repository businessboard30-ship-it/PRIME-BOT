# Card economy, collection & trading: handoff

Spec: `PRIME-BOT_Final_Economy_Cards_Prompt.md` (owner's brief). Rules that override everything: **no new commands, pure
Components V2, persistent views that survive restarts, no timeout on permanent screens.** Open a PR per slice; check
`git log` / open PRs first because more than one AI works on this.

## Built and merged
| PR | What |
|----|------|
| #496-#500 | Backend only: global store + limited stock + per-copy ownership + coin ledger (`modules/card_economy.py`), boosts + items + skills (`card_boost.py`), global/local trading + coin transfers (`card_trade.py`), coin flip / league flip / rob (`card_games.py`), profiles + browser + achievements + leaderboards (`card_profile.py`). Migrations 088-092. Real-Postgres tests. |
| #501 | Wired migrations 088-092 into `database._create_tables`. |
| economy wiring | Migration 093. Level-ups pay coins once per person per level, bot-wide (`card_rewards.py`, knobs in `config.py`, hooked in `cogs/leveling.py`). `cogs/card_hub.py` sweeps expired rentals + trade offers every 5 min (main bot only). |

## Not built yet (nothing below has any UI)
1. **Card Hub screens** (Components V2, persistent `DynamicItem`s, `timeout=None`): Store (paginated, stock / level / rental labels, confirm-buy), My collection + card detail + boost, Profile (privacy toggles, featured / signature card), Achievements, Collector leaderboards (local + global). Entry point: a "Cards" button on the XP leaderboard bottom row (next to XP & Clans / My card), no new command.
2. **Trading + coin transfer + games UI**: recipient entry reuses the Cat Game ID modal pattern (`_views_cat_adopt.TradeIdModal`, `modules/cat_ids.py`); trade accept/decline buttons that survive restarts; Coin Flip / League Flip / Rob buttons.
3. **Boost items**: `card_boost.grant_item` exists but nothing grants items yet (drops / quests / admin).
4. **Chief Seat purchase with coins** (spec section 2): not started.
5. **Admin tooling** for creating offers / limited stock / replenishing (`create_offer`, `set_limited_stock`, `replenish_stock` exist, no UI).
6. XP Options integration for per-server economy settings (today the level-up coin knobs live in `config.py`).
7. Restart-persistence and long-idle behaviour must be tested for real before claiming it works.

## Gotchas
* `cogs/cards.py` is the OLDER card system (Card Coins packs / marketplace, legacy embeds, 180 s view timeout). The new
  economy shares its `card_coins` balance and `card_catalog` table but is separate; do not break it.
* Changing `database._create_tables` needs a `SCHEMA_VERSION` bump AND new pins in `tests/unit/test_schema_version_guard.py`.
* There is no "sleep / inactivity" system in this repo; background work is kept to one cheap indexed sweep.
* Tests: `pip install pgserver asyncpg pytest-asyncio`, then `pytest tests/integration/test_card_*_postgres.py`.
  `tests/unit/test_premium_price_copy.py` already fails on main.
