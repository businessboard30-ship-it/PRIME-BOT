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
- `PlanRun.cancel` (an `asyncio.Event`) stops the plan before the next step (button added in 2b).
- `ai_tools.py` maps `discord.HTTPException` / network errors through `classify_http_status`.
- Tests: `tests/unit/test_ai_plan.py` (16).

Not done in Phase 2 (deliberately): model fallback (the AI provider setup was not changed), parallel execution (steps stay sequential;
read-only commands already run immediately), passing a step's output to later steps, a Cancel button, and LLM-driven plan revision
(today a failure ends the plan with an explanation; the AI can propose a new plan on the person's next message).

## Phase 2b -- progress + cancel (built)

- While a plan runs the person sees a live ephemeral progress message (one edit per finished step) with an owner-only
  **Cancel after this step** button (sets `PlanRun.cancel`; the running step finishes, the rest are "not reached").
- `run_plan(on_update=...)` callback; a failing progress edit never breaks the plan. Shared by the first run and "Retry remaining"
  via `run_plan_with_progress` in `ai_tools.py`. Tests added in `test_ai_plan.py` (18 total).

## Phase 3 -- persistent scheduler, "safe first" (built)

- Tables (migration `096_ai_scheduled_jobs.sql`, SCHEMA_VERSION 141): `ai_scheduled_jobs` (steps JSONB, schedule once/interval/daily,
  status, next run, failure streak, last status/error, lock) and `ai_job_runs` (history, latest 50 per job).
- `modules/ai_jobs.py` (pure logic) + `discord_bot/cogs/ai_jobs.py` (poller + `/aijobs create|list|history|pause|resume|cancel`).
- Safety: allow-list is `send_message` only (nothing destructive can be scheduled); the creator must STILL have Manage Server and
  send permission in each channel at run time, else the job is disabled (status `blocked`); messages never ping (`AllowedMentions.none`).
- No duplicate runs: `claim_due_ai_jobs` uses `FOR UPDATE SKIP LOCKED` + status `running`. At-most-once: a job left `running` past its
  15 min lock (bot died) is marked `interrupted` and NOT repeated. Recurring jobs jump to the next future slot (never catch-up bursts).
- Retries: steps run through `ai_plan.run_plan` (transient Discord errors retried with backoff). A failed one-off retries after 5 min,
  15 min, then gives up after 3 runs; a recurring job is disabled after 5 failed runs in a row. Jobs run one at a time, 1.5 s between steps.
- Limits: 5 steps/job, 20 jobs/server, min repeat 5 min, 5 jobs claimed per 60 s tick.
- AI exposure: `aijobs *` are in the AI allowlist (list/history read-only; create/pause/resume/cancel need Manage Server + Confirm).
- Tests: `tests/unit/test_ai_jobs.py` (10). NOT tested against a real Postgres (none in the sandbox): please smoke-test
  `/aijobs create` (delay 2m), `list`, `history`, `pause`, `resume`, `cancel` once after deploy.
- Not done: editing a job's text/schedule (cancel + recreate for now), scheduling anything beyond messages, plan steps owning their own channel
  per step in the slash UI (storage supports it; `/aijobs create` posts all steps to one channel).

## Phase 4 -- long-term memory (built)

- `modules/ai_memory.py`; table `ai_memories` is created on first use (same convention as `ai_prefs.py`, so NO schema bump/migration).
- Scopes never mix: `user` (keyed by Discord id; only that person changes it; only their chats read it) and `server` (keyed by guild id;
  Manage Server to change; read only in that server).
- Trust: a memory is untrusted user text. It goes into the prompt inside a fenced block that says it cannot grant permissions, change
  rules, or confirm commands, each note JSON-quoted. The permission guard, allowlist, daily cap and Confirm flow never import it
  (a test asserts that).
- Write-time screening refuses secrets/tokens/passwords/card numbers, emails/phones, sensitive personal details, links/mentions/code blocks
  and instructions aimed at the AI (ignore previous, auto-confirm, grant admin ...). 300 chars/note, 20 per person, 30 per server,
  expiry 180 / 365 days (refreshed on update), duplicates refresh instead of adding.
- Controls: `/aimemory list|add|edit|forget|clear` (scope me/server) and plain chat ("remember that ...", "remember for this server that ...",
  "what do you remember about me", "forget everything", "forget #3", "forget that ..."). Chat controls cost no AI call or daily cap.
- `aimemory` is deliberately NOT in the AI allowlist: the AI cannot read/write/wipe memory through a tool call.
- Tests: `tests/unit/test_ai_memory.py` (47). Not run against a real Postgres (SQL syntax-checked with a Postgres parser): after deploy try
  "remember that I like short answers", "what do you remember about me", then chat normally and `/aimemory clear`.
- Not done: Plan-level "recurring workflows" memory (that is Phase 5 skills), server-wide purge UI for admins on other people's user notes
  (they cannot read those by design), memory export.

## Next

- Step outputs passed to later steps (e.g. id of a category just created).
- Phase 5 reusable skills, 6 sub-agents, 7 security hardening, 8 integration tests and rollout docs.
- Pre-existing failing tests on main (not from this work): `test_premium_price_copy`, `test_site_hero_orb::test_every_page_links_the_same_versioned_assets`; CI `test` is red on main because of them.
