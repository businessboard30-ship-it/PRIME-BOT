# Catch architecture

Catch is a persistent Discord game subsystem. The bot process owns the gateway and
background loops; PostgreSQL is the source of truth for configuration, spawns,
reminders, and audit data.

## Module boundaries

- `modules/catch_schema.py` defines the Phase 1 tables and schema version.
- `modules/catch_db.py` owns database-facing helpers and transaction boundaries.
- `modules/catch_setup.py` models server setup state and validated owner mutations.
- `modules/catch_gate.py` is the kill-switch boundary. Commands check it before
  opening game surfaces or performing game actions.
- `modules/catch_scheduler.py` selects due work, applies retention rules, and
  exposes the state needed to resume after a restart.
- `modules/catch_reminders.py` creates and dispatches deduplicated reminders while
  respecting configured quiet hours.
- `modules/catch_i18n.py` resolves `catch.*` keys and falls back to English when a
  locale is incomplete.
- `modules/catch_assets.py` loads published art and returns a safe placeholder when
  an asset is missing.
- `modules/catch_renderer.py` renders placeholder cards off the event loop and
  caches output by asset id and version.
- `discord_bot/cogs/catch.py` contains the slash command, persistent components,
  owner setup screen, and localized Discord presentation.

## Runtime flow

1. `/catch` checks the feature gate and opens the hub.
2. Persistent components read the current server setup and route to the selected
   screen.
3. Spawn and reminder work is persisted before background dispatch, so a process
   restart can resume from due rows rather than in-memory state.
4. Asset loading and rendering are defensive: missing or unpublished art uses the
   placeholder path instead of failing the Discord interaction.
5. Configuration changes update the feature flag and append an audit record.

## Restart and safety rules

Background loops must be single-owner per bot process, claim due rows before work,
and leave live rows untouched during retention cleanup. Every user-facing action
must re-check the gate because an administrator can disable Catch immediately.
Database writes should remain idempotent so retries after a disconnect do not create
duplicate setup, reminder, or audit records.

## Data and localization

Catch-specific data files are kept under `data/catch/`; translations are stored in
`locales/<locale>.json` under `catch.*` keys. English is the fallback catalog. New
copy should be added to the catalog before it is referenced by the Discord cog.

## Operational checklist

- Run the compile check before deployment.
- Run the Catch unit tests when the repository test dependencies are installed.
- Keep the bot on an always-on host; Discord gateway and scheduler work are not
  compatible with request-only serverless execution.
- Review audit records when changing setup or feature flags.

This document describes the current Phase 1 foundation; later phases may add spawn,
catch, inventory, and economy services without moving persistence into the cog.

## Related documents

- [`CATCH_GAME_PLAN.md`](CATCH_GAME_PLAN.md)
- [`AUDIT_P1-02_to_P1-05.md`](AUDIT_P1-02_to_P1-05.md)
