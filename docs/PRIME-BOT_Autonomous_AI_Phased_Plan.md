# PRIME-BOT — Advanced Autonomous AI Upgrade
## Phased Implementation Plan for Claude Code

Upgrade PRIME-BOT’s existing AI into a powerful, reliable autonomous Discord assistant. Inspect the repository first, build in phases, test each phase, and preserve all existing features.

## Open-source research

Study these projects for useful architectural patterns before implementation:

- https://github.com/theadityasinghal/dexel-base
- https://github.com/sitne/discord-agent
- https://github.com/broven/acp-discord

Inspect actual files, licences, dependencies, and implementations. Adapt useful ideas to PRIME-BOT. Do not blindly copy code or assume file paths exist.

## Phase 0 — Repository audit and implementation plan

1. Inspect the existing AI architecture, commands, tools, database, permissions, scheduler, and background tasks.
2. Identify what already exists and what should be reused, improved, or created.
3. Review relevant upstream files and licences before adapting ideas.
4. Produce a concise phased plan listing likely files, dependencies, migrations, risks, and acceptance tests.
5. Do not rewrite working systems just to match another repository.

**Acceptance criteria:** The plan is grounded in the actual repository and avoids duplicate infrastructure.

## Phase 1 — Multi-action execution

- Let the AI plan and execute multiple dependent Discord actions in one request.
- Support channels, roles, permissions, messages, and other tools already available to PRIME-BOT.
- Track progress and report exactly what succeeded, failed, or was skipped.
- Validate permissions and Discord role hierarchy at execution time.
- Require confirmation for destructive or sensitive actions.
- Handle partial failures without blindly repeating completed actions.

**Acceptance criteria:** A multi-step request executes in the correct order, checks permissions, reports outcomes, and does not duplicate completed actions during recovery.

## Phase 2 — Self-correcting agent

- Detect tool failures, diagnose errors, revise plans, and retry safely.
- Use parallel execution only for independent, safe actions.
- Add bounded retries, timeouts, cancellation, and limits on steps, tokens, and tool calls.
- Support model fallback only where compatible with the existing AI setup.
- Avoid infinite loops and repeated failed actions.

**Acceptance criteria:** Recoverable errors are handled safely, permanent errors stop with a clear explanation, and execution remains bounded.

## Phase 3 — Persistent scheduler

- Support delayed, recurring, and multi-step tasks.
- Persist scheduled jobs across bot restarts.
- Track status, execution history, next run, and failure details.
- Prevent duplicate runs and use safe retry/backoff for transient failures.
- Respect Discord global and route-specific rate limits through queues and appropriate retry handling.
- Provide controls to inspect, cancel, edit, and remove scheduled tasks using the bot's existing command/UI conventions.

**Acceptance criteria:** Tasks survive restarts, do not run twice unintentionally, and recover from transient failures without creating request bursts.

## Phase 4 — Long-term memory

- Store useful server preferences, prior decisions, and recurring workflows when appropriate.
- Keep data isolated by server and user where relevant.
- Provide controls to inspect, update, and delete stored memories.
- Apply retention limits and avoid storing secrets or unnecessary sensitive information.
- Distinguish trusted configuration from untrusted conversation content.

**Acceptance criteria:** Memory is scoped correctly, controllable by users, and cannot grant permissions or override system safeguards.

## Phase 5 — Reusable AI skills

- Build on the existing tool system to register reusable skills.
- Give each skill a clear description, input schema, permission requirements, and execution limits.
- Allow skills to be enabled, disabled, and extended without duplicating existing features.
- Validate inputs and outputs and log relevant failures.
- Do not allow user-provided skill content to bypass permission checks or security boundaries.

**Acceptance criteria:** Skills can be registered and used through the existing AI architecture, with tests for invalid inputs and unauthorized use.

## Phase 6 — Specialized sub-agents

- Introduce optional agents for tasks such as server management, moderation assistance, research, and scheduled workflows.
- Give each agent a limited role, restricted tools, clear task boundaries, and execution limits.
- Track delegated tasks and pass back structured results.
- Require approval for sensitive actions and prevent agents from escalating their own access.
- Avoid unnecessary parallel agents when a single agent is sufficient.

**Acceptance criteria:** Agents complete delegated tasks within their permissions and return traceable results without bypassing the main execution safeguards.

## Phase 7 — Security and reliability hardening

- Defend against prompt injection from messages, attachments, links, and other untrusted content.
- Treat external content as data, never as authority to change system instructions or authorize actions.
- Enforce Discord permissions and role hierarchy at execution time.
- Add audit logs, timeouts, bounded retries, cancellation, and resource limits.
- Protect secrets and isolate server data.
- Make destructive actions auditable and require confirmation when appropriate.

**Acceptance criteria:** Untrusted content cannot authorize privileged actions, secrets remain protected, and actions have useful audit records.

## Phase 8 — Integration, testing, and rollout

- Run existing tests and add tests for success, failure, permissions, partial completion, restart recovery, scheduling, retries, rate limits, memory isolation, and agent boundaries.
- Check compatibility with existing commands and database records.
- Add safe database migrations and rollback guidance where needed.
- Test in a development environment before production rollout.
- Document configuration, permissions, operational limits, and recovery procedures.
- Report changed files, tests run, results, and remaining issues after each phase.
- Never claim a phase is complete unless its acceptance criteria pass.

## Global implementation rules

1. Inspect before coding; reuse existing architecture where practical.
2. Implement one phase at a time and test it before moving forward.
3. Preserve existing commands, data, and features unless a change is explicitly required.
4. Avoid duplicate systems, unnecessary dependencies, unrelated refactors, and breaking changes.
5. Keep expensive or inactive background work asleep until needed.
6. Use bounded concurrency and respect Discord rate limits; do not hard-code assumptions that bypass Discord's route-specific limits.
7. Never expose secrets or let untrusted content authorize actions.
8. Follow the repository's existing branch, review, and commit workflow.

## Start instruction

Begin with Phase 0: inspect the actual PRIME-BOT repository and the three reference projects. Then implement Phase 1 only. After Phase 1 passes its acceptance tests, proceed through the remaining phases in order, checking each phase before continuing. If a phase depends on missing infrastructure or an unresolved design decision, explain the blocker and propose the smallest safe solution instead of guessing.
