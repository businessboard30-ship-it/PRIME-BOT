# Catch game — Audit / Handoff: P1-02 to P1-05 (Phase 1 foundation, partial)

**The full building plan is in this repo: [`docs/catch/CATCH_GAME_PLAN.md`](./CATCH_GAME_PLAN.md).**
It is the source of truth (Discord bot, not a web game). Read it before doing anything; you do not
need the user to re-send it. If it changes, update that file in the same PR.

Builder: v0 (session ran out of credit). Reviewer must be the other AI (Claude), per R.1.
Status for every task below: `[~]` (built, NOT complete: unit tests are missing).

```
HANDOFF
Task ID(s): P1-02 (schema), P1-03 (theme), P1-04 (core services), P1-05 (species loader).
            Plus Phase 0 data files. P1-01 (CI) NOT started.
Branch / last commit: feat/catch-p1-01-05-foundation (see PR; base = main @ 0afcef4)
Done (with evidence): see "What is built" below.
NOT done (exact files, functions, screens, tests): see "What is NOT done".
Exact next step: write tests/test_catch_*.py (list below), then P1-01 CI workflow,
                 then run R.6 + full pytest and fill in the completion report.
Known problems: see "Known problems / risks".
Commands to run to see the current state:
  git fetch origin && git checkout feat/catch-p1-01-05-foundation
  python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
  .venv/bin/python -m pytest -q                       # expect 841 passed, 21 skipped
  .venv/bin/python -m compileall -q modules/catch_*.py database.py discord_bot/bot.py
  .venv/bin/python -c "from modules import catch_species as s; print(len(s.load_file()), s.validate(s.load_file()))"
                                                      # expect: 48 []
```

## What is built

### Data files (Phase 0 decisions) — `data/catch/`
| File | Contents |
|---|---|
| `species.json` | 48 original species (rule 0.8): id, slug, name, rarity, element/element2, habitat, base_stats, evolution fields, catch_rate_mod, spawn_weight, exclusive_to, egg_pool, enabled. Passes `catch_species.validate` with zero problems. |
| `theme.json` | Default rarity / element / state colours and button-role -> style map (rule 0.10). |
| `counter_matrix.json` | Element counter (type advantage) table used by `catch_service.counters_for`. |
| `drop_tables.json` | Reward drop tables used by the catch pipeline. |

### Modules — `modules/`
| File | Purpose | Public API |
|---|---|---|
| `catch_game.py` | Pure game rules, no Discord, no DB. | `Rarity`, `rarity`, `roll_rarity`, `roll_level`, `roll_ivs`, `roll_shiny`, `compute_stats`, `iv_percent`, `catch_chance`, `sell_value`, `weighted_pick` |
| `catch_db.py` | Thin pool/transaction helpers on top of the existing `database.py` pool. | `get_pool`, `transaction`, `connection`, `clone_key` |
| `catch_schema.py` | **P1-02.** Idempotent `CREATE TABLE IF NOT EXISTS` + unique indexes, all per-server/per-player tables keyed with `clone_id`. | `create_tables(conn)` |
| `catch_theme.py` | **P1-03.** Colours + button styles from `theme.json` with DB overrides from `catch_theme`. | `parse_hex`, `button_style`, `button_roles`, `state_color`, `rarity_color`, `element_color`, `validate_override`, `apply_overrides`, `refresh_overrides`, `set_override`, `reset_override` |
| `catch_cooldowns.py` | **P1-04.** Shared cooldown helper (1.5). Atomic "ready at" check-and-set. | `remaining_seconds`, `format_remaining` ("ready in 2m 10s"), `get_ready_at`, `try_start`, `clear` |
| `catch_gate.py` | **P1-04.** `check_player_allowed(user_id, guild_id, action)`; Phase 1 checks only `catch_feature_flags` (global + per-server), cached. Phase 9 plugs bans in here. | `Gate`, `decide` (pure), `check_player_allowed`, `set_feature_flag`, `invalidate` |
| `catch_service.py` | **P1-04.** The single `record_catch` pipeline (rule 0.9): one transaction writes `catch_owned`, `catch_dex`, `catch_players`, `catch_spawns` claim, `catch_audit`. | `CatchBlocked`, `CatchResult`, `record_catch`, `break_streak`, `counter_matrix`, `counters_for` |
| `catch_species.py` | **P1-05.** Loads + validates `species.json`, upserts into `catch_species`, disables removed species, skips work when the file hash (stored in `admin_config`) is unchanged. | `load_file`, `validate`, `sync_to_db(conn=None, force=False)`, `all_species`, `get`, `invalidate_cache` |

### Tables created (`catch_schema.create_tables`)
`catch_species, catch_guild_config, catch_spawns, catch_owned, catch_dex, catch_players, catch_inventory, catch_cooldowns, catch_feature_flags, catch_reminders, catch_assets, catch_theme, catch_stats_daily, catch_audit` — matches plan §1.1 (plus `catch_cooldowns` for the 1.5 helper). `catch_assets` stores no image bytes (rule 0.12).

### Wiring into existing code
- `database.py`: `SCHEMA_VERSION` **"54" -> "55"** with a changelog comment; `catch_schema.create_tables(conn)` called inside the migration block (~line 2835).
- `discord_bot/bot.py` `setup_hook`: after `db.init()`, calls `catch_species.sync_to_db()` inside try/except so a broken roster file logs an error and never stops the bot.

## Checks run (real output, this session)
- `pytest -q` (full existing suite): **841 passed, 21 skipped** — same as baseline, nothing broken.
- `py_compile modules/catch_*.py database.py discord_bot/bot.py`: OK.
- Roster validation: 48 species, 0 problems.
- R.6 stub scan on the diff + new modules (TODO/FIXME/NotImplementedError/bare `pass`/`...`): **empty**.
- Real-Postgres migration smoke test: **NOT run at the end of the session** (local Postgres in the sandbox stopped responding). Reviewer must run it (see below).

## What is NOT done
1. **Unit tests (blocks P1-02..P1-05 from `[x]`).** Create:
   - `tests/test_catch_game.py` — rarity table sums, `roll_*` ranges with seeded RNG, `compute_stats` formula, `catch_chance` bounds (0..1, ball/rarity modifiers), `weighted_pick`.
   - `tests/test_catch_theme.py` — every role in `theme.json` maps to a valid `discord.ButtonStyle`; every rarity/element/state has a colour; `validate_override` rejects bad hex; `apply_overrides` wins over defaults.
   - `tests/test_catch_cooldowns.py` — `format_remaining` text cases; `try_start` refuses while active and is atomic.
   - `tests/test_catch_gate.py` — `decide` precedence (global off > server off > on; unknown action allowed/denied as designed).
   - `tests/test_catch_service.py` — `record_catch` happy path, double-claim of the same spawn returns blocked (only one winner), dex count increments, audit row written.
   - `tests/test_catch_species.py` — `validate` catches duplicate ids/slugs, bad rarity, evolution loops; `sync_to_db` second run returns 0; removed species become `enabled = FALSE`.
   - `tests/test_catch_schema.py` — `create_tables` runs twice without error; every per-player/per-server table has `clone_id`. Follow the existing real-DB test pattern used by the v54 quarantine tests (skip when no DB).
2. **P1-01 CI** — `.github/workflows/ci.yml` (pytest + compileall on every PR). Not started.
3. **P1-06 .. P1-15** — hub, persistent items, owner setup, scheduler, reminders, localization, asset manager, kill-switch admin toggle UI, card renderer, docs. Not started. No `discord_bot/cogs/catch.py` yet, so nothing is visible in Discord yet.
4. `docs/catch/TASKS.md` and `docs/catch/ARCHITECTURE.md` not created yet (P1-15).

## Known problems / risks for the reviewer
- Migration not yet proven on real Postgres in this session. Run `create_tables` twice on an empty DB, then `sync_to_db()` twice (second must return 0).
- `catch_species.sync_to_db` stores its hash in `admin_config`; confirm that table/columns (`key`, `value`, `updated_at`) match `database.py` on main.
- `record_catch` claim of a spawn must be a conditional `UPDATE ... WHERE caught_by IS NULL ... RETURNING` inside the same transaction — verify under concurrency (R.7 step 9).
- Gate cache: confirm `set_feature_flag` invalidates so a kill switch takes effect immediately (P1-13 "Done when").

## Smoke test for the owner (after merge — nothing visible yet)
1. Deploy; bot log should show `Database tables verified/created.` then `Catch species synced (48 rows written).`
2. Restart; second boot should log `Catch species synced (0 rows written).`
3. There is no `/catch` command yet — that is P1-06.

## Continue prompt for the next v0 / Claude session
```
Read docs/catch/AUDIT_P1-02_to_P1-05.md and CATCH_GAME_PLAN.md. Check out branch
feat/catch-p1-01-05-foundation. CONTINUE from "What is NOT done": write the listed
tests, add the P1-01 CI workflow, run the Postgres smoke test, run R.6 and the full
pytest, then replace this file's HANDOFF with the R.5 completion report.
Do not start P1-06 on this branch.
```
