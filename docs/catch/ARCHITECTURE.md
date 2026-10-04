# Catch architecture

Catch is implemented as small, restart-safe services behind the Discord cog. The database is the source of truth for configuration, live spawns, player state, reminders, feature flags, assets, and audit records.

## Module boundaries

- `modules/catch_schema.py` — idempotent Phase 1 schema statements and table list.
- `modules/catch_db.py` — connection and transaction helpers.
- `modules/catch_setup.py` — owner setup mutations, channel configuration, speed presets, and audit entries.
- `modules/catch_gate.py` — cached global/guild feature flags and the shared action gate.
- `modules/catch_scheduler.py` — persistent due-row scheduling, expiry handling, and retention cleanup.
- `modules/catch_reminders.py` — reminder deduplication, quiet-hour checks, and due reminder claiming.
- `modules/catch_i18n.py` — `catch.*` translation lookup with English fallback.
- `modules/catch_assets.py` — Discord vault references, placeholder fallback, draft-to-live publishing, and missing-art queries.
- `modules/catch_renderer.py` — Pillow placeholder card rendering with `asyncio.to_thread` and versioned caching.
- `discord_bot/cogs/catch.py` — Discord commands, persistent views, setup controls, encounters, claims, and reminder dispatch.
- `scripts/import_catch_assets.py` — validates an asset manifest before import.

## Data files

- `data/catch/` contains species and other game data.
- `locales/*.json` contains user-facing translations; missing keys fall back to English.
- `docs/catch/` contains product decisions, the task ledger, and this module map.

## Persistence and restart behavior

Player- and guild-scoped rows include `clone_id` through generated `clone_key`. Mutations use transactions and database uniqueness constraints for idempotency. Scheduler and reminder work is selected by `due_at`/`expires_at`, so interrupted work resumes after restart. Persistent Discord views are registered during bot setup and callbacks re-read database state.

## Safety controls

User-facing actions call the shared feature gate before doing work. Owner/admin changes are permission-checked, audited, and invalidate the gate cache. Asset binaries are referenced by vault channel/message IDs; missing or unpublished art resolves to a placeholder.

## Checks

For Catch changes, run targeted `pytest` coverage, Python compilation, JSON validation for changed data, and `git diff --check`.
