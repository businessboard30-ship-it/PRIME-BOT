# Catch System Audit and Handoff

Date: 2026-10-04
Branch: current Catch feature branch

## Current state

Catch has a substantial Phase 1 foundation implemented:

- Database/schema layer and Catch domain modules
- Theme, species, renderer, assets, localization, feature gating, cooldowns
- Catch hub and server-owner setup controls
- Scheduler and reminder dispatch
- Reliability hardening for scheduler expiry and reminder delivery failures
- Asset manifest validation and friendly importer errors
- Catch UI localization for hub, setup, encounter, selector, and navigation controls
- Owner admin-panel entry for Catch setup
- Persistent DynamicItem registration for restart-safe buttons, with duplicate-registration protection
- Spawn persistence regression coverage

Recent pushed commits include:

- `c97d809` — avoid duplicate dynamic registration
- `4628b39` — spawn persistence regression coverage
- `4802a32` — register persistent dynamic buttons
- `bfc50cf` — Catch owner-panel controls
- `debd52d` — isolate spawn expiry failures
- `d970585` — scheduler batch regression coverage
- `52b3d7e` — encounter metadata localization

## Validation status

Passed repeatedly:

- Python bytecode compilation for changed modules
- JSON validation for `locales/en.json`
- `git diff --check`
- Clean Git working tree after pushes

Blocked:

- `pytest` is not installed in the repository environment, so the Catch unit suite has not been executed here. Do not claim the tests passed until pytest is installed and run.

## Remaining work, in priority order

### Phase 1 verification and release readiness

1. Verify CI workflow exists and runs the Catch suite.
2. Verify persistent `discord.ui.DynamicItem` registration after restart.
3. Complete end-to-end owner-panel DM/server setup integration.
4. Finish the complete asset-manager UI/import flow, not only the manifest foundation.
5. Add/refresh architecture and operational documentation.

### Phase 2 core gameplay

Implement and test these as separate focused slices:

1. Message-based spawn trigger and spawn creation.
2. Despawn/expiry behavior and scheduler integration.
3. Ball and berry inventory/catch formula.
4. Atomic claim transaction and duplicate-claim prevention.
5. Success/failure result messages and renderer integration.
6. Full trigger → encounter → throw → claim pipeline.
7. Encounter/rare-ping behavior.
8. Anti-bot/guardrail integration.
9. Restart/recovery tests for active encounters and scheduler state.

### Later phases

- Phase 3: collection/Dex and richer collection UX.
- Phase 4: progression, rarity, achievements, and rewards.
- Phase 5: economy, trading/social systems, and retention loops.
- Phase 6: polish, analytics, operations, and production rollout.

## Handoff rules for the next AI

- Work only in the existing Catch architecture; do not rewrite unrelated bot systems.
- Read this file and `docs/catch/CATCH_GAME_PLAN.md` before editing.
- Inspect live callers and tests before changing a module.
- Make one focused change at a time.
- Use staging/copy workflow if the repository is outside the editable workspace.
- Run compile checks, JSON checks where relevant, `git diff --check`, and pytest when available.
- If pytest is unavailable, report it clearly; never claim tests passed.
- Commit each coherent change with a focused message and push it to the current branch.
- Preserve existing localization keys and Catch feature gates.
- Do not introduce localStorage, mock persistence, or hardcoded production data.
- Continue from the next incomplete Phase 1 item before starting Phase 2.

## Latest audit update — 2026-10-04

The Phase 2 spawn slice is now wired through the live Catch cog, not only exposed as standalone primitives:

- `on_message` checks the player gate and configured channel trigger state.
- A spawn roll is persisted with `create_spawn` before the Discord message is published.
- The published claim view is linked back with `attach_spawn_message`.
- Spawn expiry is handled by the scheduler path, with per-row failure isolation.
- `record_catch` is the atomic claim boundary and rejects expired, fled, already-claimed, wrong-clone, and player-owned-by-another-user claims.
- Restart-safe dynamic hub buttons are registered once through `setup_hook` and covered by regression tests.

This audit confirms the spawn trigger → persistence → publish → claim path is present in the current branch. The remaining verification gap is environmental: pytest is not installed in the repository environment, so unit tests still need to run in CI or an environment with the test dependencies installed.

## Remaining build priorities

1. Add focused tests for the live cog spawn trigger and claim callback, including publish failure cleanup behavior.
2. Finish the owner-panel end-to-end setup flow and verify permissions/DM behavior against Discord.
3. Complete the asset-manager UI/import flow and document the operational import command.
4. Run the full CI suite, stub scan, and database migration/version checks.
5. Continue Phase 2 with ball/berry inventory consumption, catch result rendering, and encounter-panel recovery.

## Recommended next action

Implement focused cog-level tests around the already-wired spawn pipeline, then make the smallest concrete fix exposed by those tests. Keep the atomic `record_catch` service as the only ownership boundary, commit each coherent change, and refresh this handoff with command evidence before pushing.
