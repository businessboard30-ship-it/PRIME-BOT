# Autonomous AI upgrade -- handoff

Plan: `docs/PRIME-BOT_Autonomous_AI_Phased_Plan.md` (PR #518).

## Phase 0 audit (what already exists, so nothing is duplicated)

- Tool loop: `discord_bot/cogs/ai_tools.py` (`_run_chat_turn` -> `_run_tool_calls` -> `_run_command_batch` / `_dispatch_tool_call`).
- Guard: `modules/ai_command_guard.py` (`resolve_and_check` = allowlist + asker's real permission + daily cap;
  `execute_ai_command` re-runs it at execution time; `AIConfirmView` = the Confirm click).
- Limits and pacing: `modules/ai_bulk.py` (10 commands per message, 0.8 s pause, dedupe of identical requests, per-server job locks).
- Hierarchy rules: `modules/ai_server_ops.py` and the commands in `discord_bot/cogs/ai_server_ops.py`.
- AI-only commands: `docs/AI_ONLY_COMMANDS.md` (do not re-expose them as slash commands).
- Scheduler: none for the AI yet. Other cogs use `tasks.loop` (giveaways, schedule, archive_automation, catch_scheduler).

## Phase 1 -- multi-action execution (built)

- New `modules/ai_plan.py`: ordered plan with per-step status (done / failed / skipped / not reached).
  - A failed step makes every later step skipped, because later steps may depend on it.
  - Finished steps are never run again; "Retry remaining" re-runs only failed + skipped steps, max 2 rounds.
  - Daily-cap hit stops the whole plan. Each step has a 120 s timeout; unexpected errors fail the step and stop the plan.
- `ai_tools.py`: the batch confirm (`_run_command_batch`) now runs through the plan runner and ends with an exact report.
  `PlanRetryView` adds the retry button (owner only).
- Permissions, role hierarchy and the daily cap are re-checked per step by `execute_ai_command`. Destructive/sensitive
  commands keep their existing Confirm click (one click for the whole listed plan).
- Tests: `tests/unit/test_ai_plan.py`.

Not changed: read-only commands in a batch still run immediately, before the confirm prompt.

## Phase 2 -- self-correcting execution (built)

- `modules/ai_plan.py` now separates TRANSIENT errors (Discord 429/5xx, network blips) from PERMANENT ones (403, 404, denied, bad args).
  - Transient: re-tried automatically, max 2 times per step, exponential backoff (2s, 4s, capped 30s), honouring Retry-After.
  - Permanent: never auto-retried; the report says why in plain words (e.g. "the bot is missing a permission or its role is too low").
- Hard bounds: 10 steps per plan, 16 total attempts, 600 s wall clock, 120 s per step, 2 manual "Retry remaining" rounds.
- An identical action that already failed permanently earlier in the same run is not executed again. A manual retry clears that,
  because the person may have fixed the cause (granted the permission).
- `PlanRun.cancel` (an `asyncio.Event`) stops the plan before the next step. No button calls it yet.
- `ai_tools.py` maps `discord.HTTPException` / network errors through `classify_http_status`.
- Tests: `tests/unit/test_ai_plan.py` (16).

Not done in Phase 2 (deliberately): model fallback (the AI provider setup was not changed), parallel execution (steps stay sequential;
read-only commands already run immediately), passing a step's output to later steps, a Cancel button, and LLM-driven plan revision
(today a failure ends the plan with an explanation; the AI can propose a new plan on the person's next message).

## Next

- Cancel button on the running plan, and show progress while it runs.
- Step outputs passed to later steps (e.g. id of a category just created).
- Phase 3 persistent scheduler (new jobs table + migration; reuse the `tasks.loop` pattern), then Phase 4 memory, 5 skills,
  6 sub-agents, 7 security hardening, 8 integration tests and rollout docs.
- Pre-existing failing tests on main (not from this work): `test_premium_price_copy`, `test_site_hero_orb::test_every_page_links_the_same_versioned_assets`; CI `test` is red on main because of them.
