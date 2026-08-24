# Heist Wars v1

A turn-based Discord heist minigame. This doc is the implementation
decision record + reference for everything in `game/`,
`database/migrations/001_heist_wars.sql`, and `discord_bot/cogs/heist.py`.

## Architecture

```
discord_bot/cogs/heist.py   Discord-only: slash command, embeds, buttons/views.
                             Never computes success chance or rewards.

game/heist_service.py       Orchestration: DB transactions, ownership checks,
                             cooldowns, state transitions, idempotent payout.
game/heist_engine.py        Pure: success-chance formula, clamping, outcome roll.
game/decision_engine.py     Pure-ish: event selection (uses random.Random,
                             injectable for tests), choice resolution.
game/reward_engine.py       Pure: cash/xp/intel/reputation calculation.
game/models.py              Dataclasses, enums, the state machine's
                             transition table.
game/locations.py           Static v1 content (4 locations, 6 events).

database/migrations/
  001_heist_wars.sql        Schema + constraints + indexes + seed locations.
                             Also executed idempotently by database.py's
                             _create_tables() on cold start, same as every
                             other table in this project.
```

`heist_engine` / `decision_engine` / `reward_engine` import nothing from
Discord or the database — they're plain functions over dataclasses, unit
tested without a live bot or Postgres connection.

## Database schema

- `heist_locations` — reference copy of `game/locations.py` content, for
  admin SQL/joins. Game logic reads the Python dict, not this table.
- `heist_players` — per (guild, clone, user): level, xp, intel, reputation.
- `heist_runs` — one row per attempt. `status` is the state machine.
  A **partial unique index** on `(guild, clone, user) WHERE status NOT IN
  (terminal states)` is the server-side guarantee that a player can never
  have two simultaneous active runs, even if application logic is bypassed.
- `heist_participants` — crew membership. v1 content is solo-only
  (`max_crew = 1` everywhere) but the table exists so multi-crew is a
  content change, not a schema change.
- `heist_events_log` — append-only audit trail (`heist_started`,
  `approach_selected`, `phase_changed`, `event_presented`,
  `choice_selected`, `outcome_resolved`, `reward_granted`,
  `heist_completed` / `heist_failed`, `heist_expired`).
- `heist_reward_ledger` — one row per completed run, `run_id UNIQUE`. This
  is what makes duplicate payouts structurally impossible, not just
  logically unlikely (see Idempotency below).

All numeric constraints from the brief (percentages 0–100, rewards ≥ 0,
max ≥ min, XP/Intel/reputation ≥ 0, required_level ≥ 1, crew ranges) are
`CHECK` constraints in the migration, not just Python-side validation.

## Heist lifecycle / state machine

```
PLANNING → INFILTRATION → OBJECTIVE → LOOT → ESCAPE → COMPLETED
   ↓             ↓             ↓         ↓       ↓
 FAILED/EXPIRED (any active phase can fail or expire)
```

`game/models.py::VALID_TRANSITIONS` is the single source of truth; every
transition in `heist_service` goes through a conditional
`UPDATE ... WHERE status = <expected current>` so a stale/concurrent
caller can never push an invalid transition.

- **PLANNING**: player picks location, then approach (`/heist`'s two
  select/button steps). No event.
- **INFILTRATION / OBJECTIVE / LOOT**: exactly one server-selected event
  each (3 events per run total). Events never repeat within a run. A
  choice is permanent — once submitted it's logged and the phase advances;
  there's no "undo."
- A choice flagged `instant_fail` ends the run as FAILED immediately,
  bypassing the success roll entirely.
- **ESCAPE**: no event — this is where the final success roll happens
  (`heist_service.complete_heist`).
- **COMPLETED / FAILED / EXPIRED** are terminal; no further transitions
  are ever accepted out of them.

Abandoned active runs (no update in over an hour — bot restart, user
vanished) are auto-expired the next time that player touches `/heist`,
so they don't block a new attempt (`_expire_abandoned_run`).

## Success formula

```
raw_chance    = base_chance + level_bonus + approach_modifier
              + decision_modifier - difficulty_penalty
final_chance  = clamp(raw_chance, 0, 100)      # persisted
success       = random_modifier < final_chance  # random_modifier ∈ [0, 99]
```

- `base_chance` / `difficulty_penalty` — per location (`game/locations.py`).
- `level_bonus` — `min(level, 25) * 1`. Capped at level 25 so no amount of
  grinding alone guarantees success.
- `approach_modifier` — fixed per run: stealth +8, technical +4, loud −5.
- `decision_modifier` — running sum of every choice's `success_modifier`
  across all 3 events. Not clamped individually, only the final
  probability is.
- `random_modifier` — generated **exactly once**, at the moment
  `complete_heist` wins its atomic state transition, and persisted. A
  retried/duplicated completion request reads the same stored value back
  instead of re-rolling.

## Reward formula (v1 — no partial success)

A run is binary: COMPLETED (success) or FAILED. On success, cash/xp are
`randint(min, max)` from the location's range, plus 1 intel and 1
reputation. On failure, cash/xp are a small `randint(0, 15% of location
minimum)` consolation amount, intel/reputation are 0. This was a scope
decision: the original spec didn't define partial success, so v1 doesn't
invent one.

## Progression

`level = min(50, 1 + total_xp // 100)`, re-derived from cumulative XP on
every reward grant (never reset on level-up, so there's no XP-overflow
loss). This is a deliberately simple v1 formula per the brief's
"don't over-engineer progression" instruction — documented here so it can
be revisited without spelunking the code.

## Idempotency & concurrency

`complete_heist`:
1. `UPDATE heist_runs SET status = 'COMPLETED'/'FAILED', random_modifier = ...
   WHERE id = $1 AND status = 'ESCAPE' RETURNING *` — only the caller that
   wins this conditional update proceeds to roll rewards; everyone else
   (including a retried request) reads the row back as-is.
2. `INSERT INTO heist_reward_ledger (run_id, ...)` — `run_id` is
   `UNIQUE`, so even a race that somehow got past step 1 twice (it can't,
   but defense in depth) cannot insert two ledger rows; the second insert's
   unique-violation is caught and treated as "someone else already paid."
3. Currency is credited via `balance = balance + $amount` (atomic
   increment), never read-then-write.

This is exercised directly by
`tests/security/test_heist_security.py::TestSimultaneousCompletion`,
which fires 10 concurrent `complete_heist` calls at the same run and
asserts exactly one reward ledger row results.

## Cooldowns

Enforced in `heist_service.start_heist` against
`heist_runs.completed_at` for that `(guild, clone, user, location)`, not
by the Discord UI. `idx_heist_runs_cooldown_lookup` indexes exactly this
lookup.

## Local development

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://user:pass@localhost:5432/dbname
export TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/heist_test  # for integration/security tests
```

The heist migration applies itself automatically on bot cold start
(`database.py::_create_tables`) — no manual step needed for a normal
deployment. To apply it by hand against an existing database:

```bash
psql "$DATABASE_URL" -f database/migrations/001_heist_wars.sql
```

## Running tests

```bash
pytest tests/unit -q                       # pure logic, no DB required
TEST_DATABASE_URL=... pytest tests/integration tests/security -q   # needs real Postgres
```

Integration/security tests are automatically skipped if
`TEST_DATABASE_URL` isn't set to a real Postgres instance — they exercise
actual transaction/locking semantics and are not meaningful against a mock.

## Known v1 limitations

- Solo runs only; `heist_participants`/crew plumbing exists but nothing
  populates a second participant yet.
- No partial success, no marketplace/seasons/premium systems — out of
  scope per the brief.
- 4 locations / 6 events; adding more is a `game/locations.py` content
  change only.
- `_grant_cash` writes into the existing `discord_economy_balances` table
  (shared currency with `/daily`, `/work`, etc.) rather than a
  heist-specific currency — this was the simplest integration with the
  existing economy system and keeps Heist Wars rewards spendable in the
  same shop.
