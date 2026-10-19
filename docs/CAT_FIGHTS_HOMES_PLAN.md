# PRIME-BOT — CAT FIGHTS, LEAGUE, HOMES, ITEMS & PRIMOLE: PLAN (v2)

Repo: `businessboard30-ship-it/PRIME-BOT` (Python, `discord.py` 2.7.x). Status (19 Oct 2026): **Phases 1 to 7 built** (Phases 1 to 6 merged to main; Phase 7 is on branch `feat/cat-primole-phase7`, awaiting owner review and merge). **Phases 8 to 13 not built.** Builds on the Components V2 cat game.
Everything is **virtual play currency**. No real money, ever.

## Build status (updated 19 Oct 2026)

| Phase | Status | Notes |
|---|---|---|
| 0 | Open | Owner answers (section 16) and the balance sheet are still needed **before Phase 8** |
| 1 Stats and skills | **Done, merged** | PRs #373, #378 |
| 2 Battle engine, practice, odds, replay | **Done, merged** | PRs #374, #375 |
| 3 Arena opt-in, local PvP, escrow, limits, help text | **Done, merged** | PR #377 |
| 4 Global Request Board, setup toggles, report/block | **Done, merged** | 4A to 4D: PRs #382, #384, #385 |
| 5 League | **Done, merged** | 5A data, 5B points, season close, promotion/relegation, League screen (PR #387) |
| 6 Market Challenge / Duel | **Done, merged** | PR #395. Shipped as **"Duel the seller"**: opens the Arena challenge with the seller preselected and a symmetric wager. The market is now the general marketplace (`market_listings`) |
| 7 Primole | **Built, PR open, not merged** | Migration `068_cat_primole.sql`, `modules/cat_primole.py`, one-way diamond conversion with a second-tap confirm in My cats, wallet display, help text, 15 new tests, schema version 116 to 117 |
| 8 Homes | Not built | Needs open item 5 (tiers, images) and the balance sheet |
| 9 Upgrades, selling, exposure, raids | Not built | |
| 10 Items shop | Not built | Primole spend helper is ready (`cat_primole.spend`) |
| 11 Distress system | **ON HOLD** | Owner decision pending |
| 12 Cosmetics, badges, final help | Not built | |
| 13 Optional | Not built | |

**Phase 7 as built:** 1 💎 = 100 🔮 (my proposal, used as is). `convert_diamonds` is one atomic UPDATE with a `diamonds >= n` guard, so a double tap cannot spend a diamond twice. `spend(conn, owner, amount)` runs inside the caller's transaction. `grant(conn, owner, amount)` exists only for league season rewards and top-tier quests and is **not wired to anything yet**. There is no function that turns Primole back into coins or diamonds, and tests check that the market, trade, arena, requests and life modules never mention Primole (account-bound). The wallet line shows Primole only when a player owns some. Only **one** diamond converts per confirmed tap (coins keep Convert 1 and Convert all).

**Still to decide or wire for Primole:** league season reward amounts (Phase 5 rewards currently do not pay Primole), the final name and emoji (open item 2), and shop prices (Phase 10).

---

## What changed in v2 (owner decisions)

| # | Owner decision | Where it landed |
|---|---|---|
| 1 | A hurt cat is just part of a virtual game. PvP and a **league** are wanted, **both only by accepting**. A **Challenge button on the market**: challenge the owner for their cat; if accepted and you win, **you get the cat plus coins**. | Sections 1, 5, 6 |
| 2 | Forced-sale system: **owner will look at it again.** | Section 10 is **ON HOLD** (not built until the owner decides) |
| 3 | Exposure = how visible and attackable a home is. | Section 7.4 |
| 4 | **Cartoon sci-fi toys**, not real weapons. | Section 8 |
| 5 | **Anyone** may post on the global board, **not Premium-only** (for now). | Section 6.3 |
| 6 | Make most things **expensive**, and add a currency (**Primole**) used for some purchases. Diamonds convert to it, **one way only**. | Sections 7, 8, 9, 11 |

---

## 0. What this adds

Cats get **stats and skills** the owner allocates. Players can **fight**: practice, local, server to server through a public **Global Request Board**, in an **opt-in league**, and through a **Challenge button on market listings** where the winner takes the cat. Players can **buy a home** (artwork coming), upgrade it and sell it within boundaries, and a home has an **exposure** level that decides how attackable it is. A **shop** sells shields, elixirs, potions and cartoon sci-fi gadgets, and cosmetics. A second currency, **Primole**, pays for premium items and top-tier homes.

## 1. Rules updates this plan makes (the old text must change)

Today the help pages and code say: heists only target pretend places, "a cat is never hurt or lost", unpaid rent only lowers earnings. After this plan:

- **Cats can be Hurt after a fight.** It is a game status, not real harm (wording stays light and cartoonish). Hurt cats fight at −10% stats until healed or rested.
- **A cat can change owner through a Market Challenge or a "Duel for keeps"**, only when the owner has accepted. No fight ever moves a cat without an explicit accept from both sides.
- **PvP is opt-in:** a player turns "Arena" on once. No one is challenged, raided, or listed on any board without it.
- The help pages (`cat_help`), the heist text and the rent text are updated in the same PR that ships each feature (Phase 3 first), so the rules the player reads always match the game.

## 2. Rules that never change (from the existing handoff)

- No new top-level slash commands. New screens open from the **Cat menu** (`/cat`) as new entries (**Arena, Home, Shop**), plus a **Challenge** button on market listings. All use the in-place navigation with **Back to menu** (`vis.send` / `vis.edit`).
- Do not touch the creature-catching game.
- Visual layer in Components V2 (40 components, 4000 characters). Every screen gets a budget test.
- Every spend is ONE database transaction with rows locked (same as `cat_care`, `cat_life`).
- Database changes are additive only (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`). Keep `test_schema_version_guard.py` green.
- One branch and one PR per phase, tests run and reported, **never merge unless the owner says so for that PR**.
- Report honestly that live Discord cannot be seen by me; the owner tests.
- GitHub token inline only, never written anywhere; the owner revokes it afterwards.

## 3. What already exists and gets reused

| Existing | Used for |
|---|---|
| `cat_adopt.Cat` (rarity, trait, `cat_rank` 0-25, care columns) | the fighter; stats attach by cat id |
| `cat_adopt.trade(...)` | the ownership transfer when a duel is won (keeps traded count, atomic) |
| `cat_rank` (rank costs grow 18% per rank) | stat points per rank |
| `cat_wallet` (coins 🪙, diamonds 💎, one wallet across all bots, 1 💎 = 10,000 🪙) | prices, wagers, escrow; Primole is added here |
| `cat_care` (hungry, water, bed; Feed all / Water all / Sleep all) | Hurt/Tired recovery follows the same pattern |
| `cat_life` (home rent, outings, daily loss limit 20,000) | rent stays as the starter option; fight loss limit follows the same idea |
| `cat_market` (price bands, listings, `BuyButton`, `UnlistButton`) | fair-value bands for duels; the Challenge button |
| `cat_wishlist` (public paged board) | pattern for the Global Request Board |
| `cat_announce`, `cat_share` | public results and milestones, per server setting |
| `cat_setup` wizard | server toggles (cross-server fights, show server name) |
| `cat_reminder`, `cat_quests` | reminders; quests and badges for fights and homes |
| cosmetics (hats, collars, backgrounds) | stay purely visual |

## 4. Stats and skills (allocated to each cat)

### 4.1 Stats
Four stats: **Strength** (damage), **Guard** (damage reduction and HP), **Speed** (who acts first, dodge), **Spirit** (crit and skill power).
- **Points pool** = rarity base + 2 per rank. Base: common 8, uncommon 10, rare 13, legendary 16. A rank-25 legendary has 16 + 50 = 66 points.
- Trait bonus (free +1): Brave +Strength, Chonky +Guard, Gentle +Guard, Zoomies +Speed, Dreamy +Spirit, Sassy +Spirit, others none. A data table, easy to tune.
- **Cap per stat:** no single stat above 60% of the pool.
- Points sit **unspent until the owner allocates them** in a V2 screen (+/- buttons per stat, Apply). Ranking up gives more points.
- **Respec:** free once, then `10,000 + 2,000 × rank` coins, or a Respec Potion (Primole).

### 4.2 Skills
- Slots: 1 at rank 0, 2 at rank 5, 3 at rank 12 (max 3). Unlock by rank or quest (no new rules for existing badges).
- Starter list (data, not code): **Pounce** (big hit, slow), **Hiss** (lowers enemy Strength 1 round), **Fluff Guard** (blocks a hit), **Nap Heal** (small heal, once), **Zoomies Dodge** (next hit misses), **Lucky Paw** (extra crit chance), **Yarn Trap** (enemy skips a turn, long cooldown), **Counter Claw** (return part of the damage).
- Each skill has a cooldown in rounds and a rank requirement.

## 5. Battle logic (pure, deterministic, testable)

`modules/cat_battle.py` is **pure**: no database, no Discord. Input: two fighter snapshots and a seed. Output: the round-by-round log and the winner. The seed is stored with the fight so any result can be replayed and audited.

```
HP          = 60 + 3 * (Guard + rank)
Power Rating (PR) = Strength + Guard + Speed + Spirit + 2 * skills + item_bonus   (item bonus capped at +10% of the rest)

Round order : higher Speed first; ties by seeded coin
Hit chance  = clamp(0.55 + 0.01 * (Speed_att - Speed_def), 0.15, 0.90)
Damage      = max(1, Strength_att + skill_bonus - 0.6 * Guard_def) * uniform(0.85, 1.15)
Crit chance = min(0.25, 0.005 * Spirit_att);  crit multiplier 1.5
Skills      : each round a ready skill fires if its condition is met (fixed priority list, no player input)
Fight ends  : KO, or after 8 rounds -> higher remaining HP % wins; exact tie -> defender wins
```

- **Auto-resolve with a replay:** the battle runs instantly and the screen plays it as a short sequence of edits (like the heist reveal), so nobody can stall a fight.
- **Odds preview** before accepting: a band from 200 quick simulations (*Long shot / Even / Favoured / Heavy favourite*). Bands, not exact percentages, but honest.
- **Matchmaking:** open board requests and league fights only match within **±30% Power Rating**. Challenging a named player is allowed at any gap but warns, and the weaker side gets a **rating shield** (a loss costs no rating).
- **Teams:** version 1 is 1 cat vs 1 cat. 3v3 squads later (the engine takes a list).
- **Balance harness:** a script simulating 100,000 fights across rarity/rank pairs. Rule: no stat build wins more than 60% against a balanced build of equal PR. It runs in CI with a tolerance.

### 5.1 Stakes and outcomes (coin fights)
- Wager ladder (expensive economy): **500, 2,500, 10,000, 50,000** coins. Both wagers are **escrowed** at acceptance, in one transaction.
- Winner gets both wagers minus an **8% fee** (a coin sink). Loser keeps their cat.
- **Loser's cat becomes Tired** (2 hours, cannot fight). If knocked out it becomes **Hurt** (6 hours, or a Heal Potion), −10% stats while hurt.
- Limits: **10 fights a day**, a **daily net-loss limit of 100,000 coins**, a pair cooldown (same two players max 3 fights a day, shrinking rating gain per repeat).
- Rating: Elo-style points per bot, shown on the Arena screen and a leaderboard tab. Rating has no coin value.

## 6. Fight modes (all require an accept)

### 6.1 Practice
Versus an NPC cat, no stakes. Short cooldown, a small coin reward and quest progress only. Teaches the stats.

### 6.2 Local fight (same server)
Pick a cat, pick a member who has Arena on, send a challenge. They get a private V2 card with the odds band and **Accept / Decline**. Expires in 10 minutes. Wager from the ladder.

### 6.3 Global Request Board (server to server)
- **Anyone** can post and anyone can accept (**not Premium-gated for now**; a Premium rule can be added later without changing the design).
- Everyone with Arena on can **browse** it from the Arena screen: paged, filter by wager and power band.
- Post an open request: wager, cat, power band, expiry (1 to 24 hours), max 2 open per player.
- Lifecycle: `open → accepted → locked (both wagers in escrow) → fought → settled`, or `open → cancelled / expired` (instant refund, one transaction). Accepting locks the row so two people cannot take the same request.
- The poster's cat is **locked** while a request is open or fighting (cannot be sold, traded, released, or staked elsewhere).
- **Server control** in the `/catsetup` wizard: "Allow cross-server fights" (default **off** so a server must choose it) and "Show our server name on the board".
- Public board shows: cat name, rarity, rank, PR band, wager, poster display name, optional server name. Never user IDs or DMs.
- **Anti-abuse:** no fighting your own alts (same owner blocked; repeated pairs flagged; new accounts get a wager cap; shrinking rating gain per pair); a Report button on every fight card; a per-player block list; every fight logged with its seed.

### 6.4 League (opt-in, accept-only)
- A player **joins the league** deliberately (separate from Arena on/off). Fights are by challenge or by **league matchmaking offers** (the bot proposes an opponent in the same band; **both must accept**).
- **Seasons** of 28 days, five divisions: **Whisker → Claw → Fang → Royal → Legend**. Win/loss earns league points; promotion and relegation at season end.
- Coin wagers only inside the league (no cat stakes). Season rewards: **Primole**, exclusive cosmetics and a badge. A small entry fee per season (coins) keeps it from being free spam.
- League fights follow all rules above (limits, rating shield, anti-abuse).

### 6.5 Market Challenge and Duel for keeps (the winner takes the cat)
**A new button on every market listing: "⚔️ Challenge".**
1. A challenger (Arena on) taps Challenge on a listed cat.
2. They choose **one of their own cats as the stake** and a coin wager. The stake must be in the **same fair-value band** as the challenged cat (within ±30% of the `cat_market.price_range` midpoint), so nobody stakes a throwaway for a legendary.
3. The **owner gets a private card**: the challenger's staked cat, both cats' stats, the odds band, the wager. They can **Accept or Decline**, no penalty. Pending challenges expire in 12 hours.
4. On **accept**: both cats and both wagers are **locked in escrow** (the listing is frozen). The fight runs.
5. **Winner takes both cats and both wagers (minus 8% fee).** The transfer reuses `cat_adopt.trade`. The challenged listing is closed.
6. **Duel for keeps** is the same thing without the market: anyone can offer a duel to a named player for a named cat, same stake rules, same accept requirement.

Boundaries:
- **Both sides consent** (the owner accepts; the challenger chose the stake).
- A player **cannot stake their last cat**, a **pinned/profile cat**, or a cat that is locked (listed, in a request, in a fight). The owner's cat is protected the same way: you cannot challenge an owner's only cat.
- Stakes within the same value band; **max 3 duels a day** each way; a cooldown per pair; new-account wager/stake caps; league and Arena rules apply.
- A transferred cat keeps its name, rank and stats (stats stay with the cat), and **resets to Hurt-free, Tired**.
- Result announcements are optional per server; the owner can choose a private result.

## 7. Homes: purchase, upgrades, selling, exposure

> Home images come from the owner. Until they arrive, screens use the existing home emoji and a placeholder slot.

### 7.1 Purchase (expensive, long-term goals)
Six tiers (names are proposals): **Cardboard Box → Cozy Flat → Cottage → Townhouse → Manor → Castle.** Each has a price, cat capacity, earnings multiplier, base defence and vault size. One home per player per bot. Renting stays as the cheap starter; buying is the upgrade path.

| Tier | Coins | Primole |
|---|---|---|
| Cardboard Box | 20,000 | none |
| Cozy Flat | 150,000 | none |
| Cottage | 1,000,000 | none |
| Townhouse | 8,000,000 | 50 |
| Manor | 60,000,000 | 300 |
| Castle | 400,000,000 | 1,500 |

All numbers are **starting points**, tuned with the balance sheet (section 11) before Phase 8. Image slot per tier: `assets/images/cat_homes/<tier>.png`.

### 7.2 Upgrades
- Slots per tier (2 to 8). Types: **Extra beds** (capacity), **Vault** (safe coin storage), **Alarm** (raid warning and defence), **Cat door** (earnings), **Garden** (cosmetic plus small earnings), **Watchtower** (defence).
- Levels 1 to 5. Cost = `10% of the tier price × 1.7^level`; **level 5 costs part Primole**. Tier caps (a Cottage cannot have a level 5 Watchtower).
- Boundaries: one upgrade in progress at a time, 1 to 12 hours to build, none while a raid is resolving.

### 7.3 Selling (with boundaries)
- **To the bank:** instant, returns **50%** of everything invested in coins (Primole spent is **not refunded**). A coin sink.
- **To a player:** later phase, price band like `cat_market.price_range`, 8% fee, coins only.
- Boundaries: not while cats are housed (they move to the starter box), not during an open raid, not within 24 hours of buying, at most one sale per 7 days. The vault is paid out to the wallet first.

### 7.4 Exposure
A **0 to 100 score** the owner can see and partly control: how visible and attackable the home is.
- Raised by: high home value, a big vault, being on a public board, being listed for sale, recent wins.
- Lowered by: a **Shield**, Alarm and Watchtower upgrades, private mode.
- Bands: **Hidden (0-24), Known (25-59), Famous (60-100).** Only Known and Famous homes can be raided; Famous raids pay more.
- **Raids** (Arena on only): the raider fights the home's defender cat with the home's defence bonus. A win steals at most `min(10% of the vault, a per-tier cap)`, **vault only, never the wallet and never Primole**. A loss costs the raider a fee.
- Boundaries: 7-day new-player protection, **12-hour protection** after being raided, max 3 raids per home per day, no raids on homes with a single cat, a Shield blocks raids until it expires.
- Raid results go to the owner privately; a public post only if the owner opts in.

## 8. Items shop (cartoon sci-fi toys, shields, elixirs, potions) and cosmetics

Menu entry **Shop** (V2, paged, Back to menu). Prices are deliberately high; tune with the balance sheet.

| Item | Effect | Limits | Coins | Primole |
|---|---|---|---|---|
| 🛡️ **Force-field Shield (6h)** | blocks raids; fight losses cost no rating | cooldown equal to its length before the next; max 3 a week | 5,000 | 25 |
| 🛡️ **Force-field Shield (24h)** | same, longer | same | 15,000 | 70 |
| 🧪 **Strength / Guard / Speed Elixir** | +10% to one stat for 1 hour | one elixir active per cat | 2,500 | 12 |
| 🧪 **Mega Elixir** | +10% to one stat for 6 hours | one elixir active per cat | none | 60 |
| 💖 **Heal Potion** | cures Hurt | one per cat per day | 3,000 | 15 |
| ⚡ **Energy Potion** | clears Tired | one per cat per day | 1,500 | 8 |
| 🔄 **Respec Potion** | free stat respec | none | none | 50 |
| 💧 **Bubble Blaster** (toy gadget) | +3 damage, 10 uses | one gadget per cat | 10,000 | 50 |
| 🧶 **Plasma Yarn Launcher** | small chance the enemy skips a turn, 10 uses | one gadget per cat | 25,000 | 120 |
| 🔦 **Hologram Laser Pointer** | +hit chance, 10 uses | one gadget per cat | 40,000 | 190 |
| ❄️ **Cryo Snowball Cannon** | slows enemy Speed, 10 uses | one gadget per cat | 60,000 | 280 |

- Gadgets are **toys** (cartoon, sci-fi, no blood, no killing); they have **uses** so they are a sink and not permanent power.
- **Item cap:** total item bonus never exceeds +10% of a cat's stats, so spending cannot buy a win outright.
- **Cosmetics stay cosmetic:** hats, collars, card backgrounds (shipped) get fight-themed skins and victory auras. **No stats.** Some premium cosmetics cost Primole. A cosmetic can theme a gadget (a gold Bubble Blaster) without changing its numbers.

## 9. Primole (new currency)

- **Emoji and name:** 🔮 **Primole** (name is the owner's idea; emoji is a proposal).
- **Where it lives:** the shared wallet (`cat_wallets`), new column `primole BIGINT NOT NULL DEFAULT 0`, same shared key (-1) so it is the same across the main bot and every clone, like coins and diamonds.
- **How to get it:**
  - **Convert diamonds:** **1 💎 = 100 🔮** (proposed; equal in value to the 10,000-coin conversion, since 1 💎 = 10,000 🪙). **One way only: Primole can never be turned back into diamonds or coins.** Done as one atomic statement like `cat_wallet.convert`.
  - League season rewards and a few top-tier quests/badges (small amounts, set per season).
  - **Not** from fights, raids or the market, so it cannot be farmed.
- **Where it is spent:** premium shop items (section 8), top-tier homes and level-5 upgrades (section 7), premium cosmetics, respec potions.
- **Diamonds keep both uses:** each diamond can still become 10,000 coins **or** 100 Primole; the player chooses, both are final. Screens show both options with a clear "this cannot be undone" line and a second-tap confirm (like Rank up).
- **Rules that stop abuse:** Primole **cannot be traded, gifted, listed on the market or paid in fights** (account-bound). The market stays coins-only. Primole-only items are account-bound too.
- **Safety note:** this is play currency with no real-money purchase path. If anyone ever wants to sell Primole for money, that is a different decision and I would advise against it.

## 10. Distress system (forced sale): **ON HOLD**

The owner is reconsidering this. **Do not build Phase 11 until the owner decides.** Kept here only so the idea is not lost:
- Trigger: upkeep due and the wallet cannot cover it, after a 48-hour grace.
- Soft lock (recommended): fights, raids, walks, heists, lucky games and upgrades pause until the player resolves it by paying, selling something they pick, or letting the home go off. Nothing is taken automatically.
- Guardrails if ever built: never the last cat, never pinned cats, a sale floor of 60% of fair value, at most one event per 7 days, a fixed (non-compounding) bill, hardship relief, and never for players without Arena on.
- A hard version (automatic listing) is not recommended; it would also need the owner's explicit approval.
- The separate **Hurt cat** status (section 5.1) already adds some pressure and does not need this system.

## 11. Economy sanity (expensive on purpose)

- **Direction:** most things cost a lot, so progress is a long game and coins stay worth something.
- **Sinks:** 8% fight fee, 50% bank sale loss, item uses, shields, elixirs, respec, upgrades, gadget wear, league entry fee, Primole purchases.
- **Sources stay as they are** (earning cats, walks, heists, quests); the only new faucets are small practice rewards and league season rewards.
- **Limits:** 10 fights a day, 100,000 net fight loss a day, raid caps, vault caps, duel limits (3 a day), wager ladder.
- A **balance sheet** (spreadsheet) projects coins per day per player level, then checks that a mid-game player can afford a shield, an elixir and upkeep without feeling stuck. **This is done before Phase 8** and may change the numbers in sections 7 and 8.
- Reality check on the numbers: today a fed common cat earns about 10 coins an hour, capped at 12 hours between pickups, with rank bonuses; rank costs reach into the millions. Prices above are set so the first home tiers are reachable and the top tiers are real long-term goals. They will be adjusted once the balance sheet exists.

## 12. Data model (all additive)

| Table | Purpose |
|---|---|
| `cat_wallets` (+column) | `primole BIGINT NOT NULL DEFAULT 0` |
| `cat_stats` | `cat_id`, STR, GRD, SPD, SPI, unspent points, respec count |
| `cat_skills` | `cat_id`, slot, skill key |
| `cat_fights` | id, mode, both players, both cats, wagers, seed, compact log, winner, status, timestamps |
| `cat_fight_requests` | board posts: poster, cat, wager, PR band, expiry, status, server visibility flags |
| `cat_duels` | market challenges and duels for keeps: challenger, challenged cat, staked cat, wagers, status, expiry, listing id |
| `cat_arena` | per owner: arena on, rating, daily counters, blocks |
| `cat_league` | per owner per season: joined, division, points, entry fee paid; `cat_league_seasons` for dates and rewards |
| `cat_homes` | owner, tier, bought_at, vault coins, exposure, protection_until, off_until |
| `cat_home_upgrades` | home id, type, level, finishes_at |
| `cat_inventory` | owner, item key, quantity, uses left |
| `cat_effects` | cat id, effect key, ends_at (elixirs, shields, hurt, tired) |
| setup config (+columns) | `cross_server_fights`, `show_server_name` |

Indexes on owner and on `status` where polled. Migrations follow the repo's style and the schema version guard.

## 13. Modules and screens

- `modules/`: `cat_stats.py` (pure), `cat_battle.py` (pure), `cat_fight.py` (requests, escrow, settle), `cat_duel.py`, `cat_arena.py`, `cat_league.py`, `cat_home.py`, `cat_items.py`, `cat_primole.py` (conversion, spend).
- `discord_bot/cogs/`: `_views_cat_stats.py`, `_views_cat_fight.py` (Arena, board, challenge cards, replay), `_views_cat_league.py`, `_views_cat_home.py`, `_views_cat_shop.py`. One `Container` per screen, budget test, Back to menu.
- The market listing gets a **Challenge** button via a new persistent DynamicItem (`cat_duel:`); existing `cat_buy:` / `cat_unlist:` posts keep working. The V2 listing layout must have room (budget test for both old and new posts).
- Menu entries added to `cat_hub.MENU`: **Arena, Home, Shop**. No new slash commands. New persistent prefixes: `cat_fight:`, `cat_req:`, `cat_duel:`.

### Image specs (for the owner)
- Homes: one PNG per tier, 1024×1024, **transparent background if possible** (the badge set came on black, which made cutting harder), one consistent style. File names: `box.png, flat.png, cottage.png, townhouse.png, manor.png, castle.png`.
- Optional: arena background (16:9), item icons (512×512), skill icons (256×256), a Primole icon (256×256).
- Say whether the art is neon like the badges; the accent colours will follow.

## 14. Roadmap (one branch and PR per phase; the owner merges). Phases 1 to 6 merged, 7 built, see Build status at the top

| Phase | Scope | Done when |
|---|---|---|
| 0 | Owner answers the open items below; images arrive; balance sheet | decisions recorded |
| 1 | Stats and skills: tables, allocation screen, respec, My cats shows stats | allocation/respec tests, budget tests |
| 2 | Battle engine, balance harness, Practice vs NPC, odds bands | engine tests, harness within tolerance, replay screen |
| 3 | Arena opt-in, local PvP, challenge card, escrow, results, limits, rating; **help text and rules updated** | atomic escrow test, double-accept test, help matches game |
| 4 | Global Request Board (anyone can post), server toggles, anti-abuse, reports | board paging test, cross-server opt-out test |
| 5 | League: join, seasons, divisions, matchmaking offers, rewards | season rollover test, accept-required test |
| 6 | **Market Challenge and Duel for keeps** (button, stake rules, escrow, transfer) | transfer atomicity, last-cat/pinned protection, band check, expiry refund |
| 7 | **Primole**: wallet column, diamond conversion (one way), spend helper, wallet display | one-way conversion test, no revert path, account-bound checks. **BUILT, PR open** |
| 8 | Homes: purchase (rent kept), tiers, images, vault | purchase transaction tests |
| 9 | Upgrades, selling, exposure bands, raids with boundaries | raid cap/protection tests |
| 10 | Items shop: shields, elixirs, potions, gadgets (+10% cap), coins and Primole prices | item cap and cooldown tests |
| 11 | Distress system | **ON HOLD until the owner decides** |
| 12 | Cosmetic fight skins, badges/quests, final help pages, polish | help text matches the rules |
| 13 (optional) | 3v3 squads, player-to-player home market | owner decision |

Per-phase deliverable (same as the handoff): files, component counts for important screens, tests run with real results, what stayed unchanged, what is unverified (live Discord), branch and PR link, ready for review or not.

## 15. Test plan

- **Pure unit tests:** stat allocation caps, point pools, respec cost; battle determinism (same seed, same log), formulas, tie rules, skill cooldowns; PR band matching; item cap; exposure bands; stake value-band check.
- **Transaction tests:** escrow take/return, double-accept race, cancel/expire refund, duel transfer (both cats and both wagers, one transaction), bank sale payout, home purchase with insufficient coins, diamond to Primole (atomic, no revert).
- **Balance simulation:** 100k fights; no build above 60% against an equal-PR balanced build.
- **Screen tests:** budget ≤ 40 components and text < 4000 on every new screen, with and without the Back button; the market listing with the new Challenge button.
- **Old messages:** existing public posts and their custom IDs keep working.
- **Live check:** the owner tests Arena, board, league, Challenge, home, shop and Primole in a real server.

## 16. Open items still needed from the owner

1. **Forced-sale system** (section 10): stays on hold until you decide.
2. **Primole rate and name:** Phase 7 is built with 1 💎 = 100 🔮 and the name Primole. Still to confirm: is 🔮 OK, and is "Primole" final? (Changing either is a one-line constant change.)
3. **Cat duel stake rule:** I made it symmetric (both sides stake a cat of similar value). If you want only the challenger to risk a cat, or only coins from the challenger, say so; it changes fairness a lot.
4. **League:** season length (28 days) and five divisions OK? Entry fee amount?
5. **Home tiers and images:** how many tiers (I proposed six); send the images when ready.
6. **Price check:** the "expensive" numbers are starting points. After the balance sheet in Phase 0 I will show you a table and you can push them up or down.

## 17. Risks

- **A cat can change owner** in a duel. Mitigation: both accept, stake in the same value band, last cat and pinned cats can never be staked, daily limits, an audit log with the seed.
- **Player-hostile mechanics** (raids) can cause churn: opt-in, 12-hour protection, vault-only theft.
- **Cross-server abuse** (alt farming, win trading): section 6.3, and the server toggle defaults to **off**.
- **Pay-to-win perception:** item cap +10%, cosmetics give no stats, Primole is not earnable from fights.
- **Economy inflation or a dead economy:** sinks, balance sheet before homes, and prices tuned afterward.
- **Scope:** about thirteen phases; Phases 1 to 7 are built and already give a playable fight, league, duel and Primole game. Homes, upgrades, shop and the rest remain.
