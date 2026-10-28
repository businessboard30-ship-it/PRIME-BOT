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

## Next

- Phase 2: self-correcting agent (diagnose failures, revise plan, bounded retries/token/tool-call limits, model fallback only where compatible).
- Then Phase 3 persistent scheduler (needs a new jobs table + migration; reuse the `tasks.loop` pattern), Phase 4 memory,
  Phase 5 skills, Phase 6 sub-agents, Phase 7 security hardening, Phase 8 integration tests and rollout docs.
- Known gap: steps cannot yet pass outputs to later steps (e.g. use the id of a category just created); today
  commands resolve by name. Revisit in Phase 2.
- Pre-existing failing tests on main (not from this work): `test_premium_price_copy`, `test_site_hero_orb::test_every_page_links_the_same_versioned_assets`.
