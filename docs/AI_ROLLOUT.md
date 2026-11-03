# Autonomous AI: rollout, operations and recovery (Phase 8)

Covers phases 1-7 (command execution, planner/retries, scheduler, memory, skills, agents, hardening). Read `AUTONOMOUS_AI_HANDOFF.md` for the design;
this file is what you need to ship and run it.

## 1. What is switched on by default
| Feature | Default | Who controls it |
|---|---|---|
| AI runs allow-listed commands (Confirm for sensitive ones) | on | per-command toggles / permissions as before |
| Multi-step plans, retries, cancel | on | automatic |
| `/aijobs` scheduler | on (needs Manage Server to create) | Manage Server |
| `/aimemory` notes | on | each user / Manage Server for server notes |
| Skills (`/aiskills`) | each skill per its own default | Manage Server |
| **Agents (`/aiagents`)** | **OFF in every server** | Manage Server (`/aiagents enable <name>`) |
| Reply-chat command tools | only when the context is the asker's own words | `ai_trust.REPLY_TOOLS_REQUIRE_OWN_CONTEXT` |

## 2. Configuration
No new environment variables. Existing: `DATABASE_URL`, `ENCRYPTION_KEY`, `GROQ_API_KEY` (+ `_BACKUP`). Tables created on first use (no migration needed):
`ai_skill_settings`, `ai_agent_settings`, `ai_agent_tasks`, `ai_memories`. Migrations 096 (`ai_scheduled_jobs`) and 097 (`scam_shield_hits` evidence pointer,
SCHEMA_VERSION 142) are additive and idempotent (tested).

## 3. Limits (all enforced in code, all tested)
- Plan: 10 steps, 2 auto-retries per step (transient errors only), 16 attempts, 600 s wall clock, 120 s per step, 2 manual "retry remaining" rounds.
- One message: up to 10 different commands (one Confirm for the batch). Identical command within 15 s is ignored. Bulk jobs: 3 running bot-wide, 20 s per-server cooldown.
- Daily AI command cap per person by tier: basic 5, pro 20, elite 50, founder 200.
- Scheduler: 5 steps/job, 20 jobs/server, minimum interval 300 s, one-off retries 3x with 5/15/60 min backoff, recurring job disabled after 5 failed runs in a row.
- Memory: 300 chars/note, 20 per person, 30 per server; secrets, numbers, links, mentions refused.
- Skills: 5 steps. Agents: 3-4 actions, 30 s cooldown, 10 tasks/server/hour (fails closed), 45 s model timeout, one agent per message, no nesting.

## 4. Permissions model (unchanged by agents/skills)
Every action, however it was proposed (plain AI, skill, agent, scheduled job), is re-checked at execution: allowlist, the asker's real Discord permission, role
hierarchy, the daily cap, and the Confirm click for sensitive commands. Skills/agents/scheduler add no power. The AI cannot enable/disable skills or agents.

## 5. Day-1 smoke test (do in a test server, as an admin AND as a normal member)
1. Startup log shows no `[ai] ... not loaded`. If it does, the 100-command cap is full: free slots (retire a rarely-used group) and redeploy.
2. `/aiskills list`, `/aiagents list`, `/aijobs list`, `/aimemory` all respond. As a normal member `/aiagents enable ...` is refused.
3. `/aichat lock this channel because testing` -> Confirm prompt -> channel locked + topic set. Member without Manage Channels is refused.
4. Ask for 3 things in one sentence -> one Confirm, steps run in order, a deliberate failure (revoke a permission) shows partial result and "Retry remaining" does not repeat finished steps.
5. `/aijobs create` a message in 5 min; restart the bot before it fires; it still fires once (or is reported interrupted, never twice).
6. `/aiagents enable research_assistant`; ask "how is the server doing?"; `/aiagents history` shows the task. Disable it: the AI stops delegating.
7. Reply to the bot inside a thread where another member also posted: it answers in text, no command tools (by design). `/aichat` still runs commands.
8. Paste a fake token (`MTIz...` shaped) to the AI: the reply and the stored history show `[redacted]`.
9. Trigger a scam catch with an image; "is <id> a scammer?" shows the defanged link and the image; a `scam_lookup` row appears in `ai_command_log`.

## 6. Recovery
- **Bot restarted mid-job:** the poller claims jobs atomically (`FOR UPDATE SKIP LOCKED`); a `running` job older than 15 min is treated as interrupted at startup and recorded
  (one-off jobs are not re-sent; recurring jobs resume on schedule). Look at `/aijobs history`.
- **Stuck plan / runaway:** the user clicks Cancel (stops after the current step). Plans cannot exceed 600 s or 16 attempts.
- **Groq down/rate-limited:** replies say "AI service error"; nothing is executed; agents report "hit an error, nothing was run". No state to repair.
- **Database unreachable:** feature settings fall back to defaults (agents OFF), agent/job limits fail closed, chat continues without memory.
- **An agent misbehaves:** `/aiagents disable <name>` (takes effect within 60 s, the settings cache TTL). Emergency for the whole feature: set `ai_trust` switch / remove `ai_agents` from
  the loader tuple in `discord_bot/bot.py` and redeploy.

## 7. Rollback
Code: revert the squash commits (#542 scam link/image, #544 phase 6, #545 phase 7) in reverse order; nothing else depends on them. Data: all additions are new tables/columns;
leaving them in place after a revert is harmless. To also remove: `DROP TABLE ai_agent_tasks, ai_agent_settings;` and (only if you also revert #542)
`ALTER TABLE scam_shield_hits DROP COLUMN evidence_channel_id, DROP COLUMN evidence_message_id;` then lower `SCHEMA_VERSION` accordingly (see the trap note in `database.py`).

## 8. Known issues / follow-ups
- Nothing here has been run against production Discord/Postgres; steps in section 5 are the first real test.
- 4 new slash groups use Discord command slots; if the cap is hit the last ones are skipped silently (log only).
- Allowlist still lists the retired `botstore_*` / `archive_trending` commands (inert; see `tests/unit/test_ai_integration.py`).
- Redaction is pattern-based. Plain-text prompt injection is defended structurally (tools withheld, permissions rechecked, Confirm), not by the model's good behaviour.
- Reply-chat in busy channels is text-only (Phase 7 trade-off); a smarter rule (trust the chain, distrust ambient channel text) is possible later.
- Skills/agents/jobs are small starter sets; add more via `register_skill` / `register_agent`.
