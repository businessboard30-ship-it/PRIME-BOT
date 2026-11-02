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

## Phase 5 -- reusable skills (built)

- `modules/ai_skills.py`: a skill = developer-defined recipe (code, never user text): typed input schema, permission requirement,
  cooldown, step cap (5), per-server on/off, and a `build()` that returns EXISTING allow-listed commands. `register_skill()` is the
  extension point; names/fields are validated at registration.
- No new powers: the AI calls `skill__<name>`; `_expand_skill_calls` (ai_tools.py, first thing in `_run_tool_calls`) swaps it for ordinary
  command calls, which take the normal path (allowlist, asker's real permission, hierarchy, daily cap, Confirm, ai_plan runner).
  Expansion re-checks that every step is allowlisted and is not itself a skill (no recursion). Refusals are logged.
- Skill tools are only offered when enabled in the server and the person holds the skill's permissions. Bad input / missing permission /
  disabled / cooldown each give a plain message and run nothing.
- Built-ins: `lockdown_channel` (lock + topic notice), `reopen_channel` (unlock + clear topic), `welcome_quickstart` (enable welcome cards + set text).
- Admin control: `/aiskills list|enable|disable` (Manage Server); table `ai_skill_settings` created on first use (no migration/schema bump).
  `aiskills` is NOT in the AI allowlist: the AI cannot toggle skills. The permission guard/allowlist do not import skills (tested).
- Tests: `tests/unit/test_ai_skills.py` (19): invalid inputs, unauthorised use, allowlist escape attempts, cooldown, disabled skills, wiring.
  Not run against a real Postgres (SQL syntax-checked). After deploy: ask the AI "lock #general because of a raid" as an admin and
  as a non-admin; check `/aiskills list`.
- Not done: more built-in skills (add via `register_skill`), skills that pass one step's output to the next, skills combining read-only
  lookups, per-skill daily limits (the existing AI daily cap still counts every expanded command).

## Phase 6 -- specialist sub-agents (built)

- `modules/ai_agents.py`: an agent = developer-defined profile (role, short instructions, a FIXED tool list that must be a subset of the AI
  allowlist, needed permissions, <=5 actions, cooldown). `register_agent()` validates names/tools; agents can never list another agent, a skill,
  `scam_lookup` or the choice tool. Built-ins: `server_manager`, `moderation_assistant`, `research_assistant`, `workflow_scheduler`. None has a
  destructive tool (no ban/kick/purge/bulk-delete/permission edits).
- Flow: the AI calls `agent__<name>(task)`. `_delegate_agent_calls` (ai_tools.py, runs before skill expansion) checks: enabled in this server,
  asker holds the agent's permissions, task text valid, 30 s cooldown, server limit 10 tasks/hour (fails CLOSED if it can't count). Then ONE model
  call (45 s timeout) with only the agent's tools, restricted to what the asker can run right now. What it proposes is vetted (`vet_calls`: own
  tools only, capped) and sent down the NORMAL path (allowlist, real permission, hierarchy, daily cap, Confirm click, ai_plan runner). One agent
  per message, no nesting. Agents approve nothing and cannot widen access.
- OFF by default everywhere. `/aiagents list|enable|disable|history` (Manage Server; history needs it too). `aiagents` is NOT in the AI allowlist and
  the guard/allowlist/plan modules never import agents (tested). Tables `ai_agent_settings` / `ai_agent_tasks` are created on first use (no
  migration, no schema bump); task rows (who, agent, task, proposed commands, outcome) keep the latest 50 per server.
- Tests: `tests/unit/test_ai_agents.py` (16): registration + escalation attempts, vetting, off-by-default, perms, cooldown, hourly limit, timeout,
  model error, nested agent/skill attempts, storage failure, wiring. NOT run against a real Postgres or Discord.

### Loading fix found while building Phase 6
`bot.py` never loaded the Phase 3-5 cogs (`ai_jobs`, `ai_memory`, `ai_skills`), so `/aijobs`, `/aimemory`, `/aiskills` did not exist and the
job poller never ran (and the AI's own `aijobs_*` tools could not resolve). `bot.py` now loads `ai_skills`, `ai_agents`, `ai_memory`, `ai_jobs` LAST,
each in its own try/except. They add 4 top-level slash groups against Discord's 100 cap: if the cap is hit, only the last ones are skipped (a
warning is logged) and nothing earlier is displaced. After deploy check the log for `[ai] ... not loaded`; if `ai_jobs`/`ai_memory` were skipped,
free slots (as was done for archive/heist) or fold them under an existing group.
- After deploy: `/aiagents enable research_assistant`, then ask the AI "how is the server doing this week?" (should delegate), `/aiagents history`.

## AI scam lookup (built, outside the numbered phases)

- Admin asks the AI in chat with a Discord user id ("is 123456789012345678 a scammer?"); the bot replies PUBLICLY with an evidence embed.
  No slash command: it is an AI tool (`scam_lookup`) handled in `ai_tools.py` (`_handle_scam_lookup`), offered only to people with
  `ai_scam_lookup.REQUIRED_PERMISSION` (= `administrator`; change that one word to loosen/tighten) and re-checked at execution.
- `modules/ai_scam_lookup.py` builds the report from recorded data, so the model never writes/paraphrases evidence: this server's
  `scam_shield_hits` (id, time, channel, kind, matched rule + its note, deleted?, stored snippet if not yet scrubbed by the privacy
  retention), other servers as COUNTS ONLY (existing `scam_reputation`, never names or text), this server's warns, and Discord facts
  (account age from the id, joined, banned here, timed out). Verdict level is deterministic from counts, footer says it is a heads-up.
- Public-output safety: snippets are defanged (hxxp, [.], pings broken, markdown escaped); 8 s cooldown per admin; one lookup per message;
  non-admins get a private refusal and nothing is queried; lookups are logged (who looked up whom).
- Tests: `tests/unit/test_ai_scam_lookup.py` (14). Not run against a real Postgres (SQL syntax-checked).
- Scam link + image (added): `scam_shield_hits` now stores a POINTER (`evidence_channel_id`, `evidence_message_id`, migration 097, SCHEMA_VERSION 142)
  to the archived copy Scam Shield already saves. At lookup time `ai_scam_lookup.load_evidence` opens the newest 3 copies, only trusts one whose header
  names this server AND this user, and shows the scam link (always defanged, never clickable) and the scam image (main embed + up to 2 extra embeds).
  Domain catches with no copy still show the matched domain. Older hits (before this change) and servers with no evidence channel have no pointer, so
  they show no image. Smoke-test after deploy: trigger a scam catch with an image, then ask the AI "is <id> a scammer?".
- Not done: showing evidence from `/modlogs` actions, honeypot/antiraid catches.

## Next

- Step outputs passed to later steps (e.g. id of a category just created).
- Phase 7 security hardening, 8 integration tests and rollout docs.
- Pre-existing failing tests on main (not from this work): `test_premium_price_copy`, `test_site_hero_orb::test_every_page_links_the_same_versioned_assets`; CI `test` is red on main because of them.
