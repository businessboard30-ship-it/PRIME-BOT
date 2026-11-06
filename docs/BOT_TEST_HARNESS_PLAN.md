# Bot test harness (test server only) -- plan

Status: PLAN ONLY. Nothing in this file is built yet. Not linked from the README on purpose.

## What it is
An AI-execution feature that lets the owner ask the AI, **inside the dedicated bot-testing server only**, to run controlled
attacks against the bot's own protections (raid, nuke, scam wave, permission escalation) and then put the server back the way it
was. Purpose: find holes in antiraid / automod / Scam Shield / verification / logging before a real attacker does, and measure how
fast each one reacts.

It is for testing PRIME-BOT (and its clones) only. It is never a general feature, never offered to other servers, and never
documented for users.

## Hard rules (these decide whether it ships)
1. **One server.** A single constant `TEST_GUILD_ID` (config.py, same style as `OWNER_GUILD_ID`). **Empty/0 = the whole feature is
   disabled everywhere**, including the tool list. The owner supplies the ID; it is not guessed or read from the invoking server.
2. **Three independent gates**, each enough on its own to refuse:
   - *Offer gate*: the tools are only put in the AI's tool list when `interaction.guild.id == TEST_GUILD_ID`.
   - *Execution gate*: `ai_command_guard.resolve_and_check` re-checks guild id + asker before running (a model can never "talk its way" past the tool list).
   - *Handler gate*: the cog's own code checks again at the top of every handler and again before every destructive step (the server could be the wrong one at click time).
3. **Owner only.** Asker must be in `DISCORD_CLONE_ADMIN_IDS`. Having Manage Channels / Administrator is not enough.
4. **Main bot only.** Refuse when `bot.clone_id is not None` (clones never run it).
5. **No new slash commands.** Uses the existing AI-only pattern (`ai_only=True`, hidden before sync, no command-slot cost). Nothing visible in Discord, nothing in the README or help text.
6. **Confirm before anything destructive**, with the exact scenario, size and auto-recover time on the button (existing `AIConfirmView`).
7. **Snapshot before, restore after, always.** No destructive scenario starts unless the snapshot was saved and verified first.
8. **Dead-man timer.** If the run isn't finished and restored within N minutes (default 15), the bot restores on its own.
9. **Kill switch.** An AI-only `stop` operation cancels the run and starts the restore immediately; a bot restart also triggers restore of any unfinished run (state stored in the DB).
10. **Rate-limited and capped.** Fixed caps per scenario (e.g. <= 25 channels, <= 20 roles, <= 15 test members), pauses between Discord calls (same style as `ai_bulk`), stop at the first Discord refusal. Never parallel bursts.
11. **Test accounts only.** Raid scenarios use bot accounts the owner owns, or synthetic events. Never user-account automation (breaks Discord's rules).
12. **Audited.** Every run logs who, scenario, start/stop, result, restore outcome (existing `log_action`).
13. **Money-free and DB-safe.** Touches no payments/premium/economy tables; test data uses clearly named test rows and is removed on restore.

## Fit with the existing code
- Registered like `setup bulk-create`: `_sub(..., ai_only=True)` in `modules/ai_command_allowlist.py`, handler pair `ai_<x>_prompt` / `ai_<x>` in a new cog file, tool schema from `AI_TOOL_PARAMS`.
- New spec flag `test_guild_only: bool` on `AICommandSpec`, honoured in `get_qualifying_commands` (offer gate) and `resolve_and_check` (execution gate). Today the guard deliberately has no owner-only commands; this flag is the one deliberate exception and must be documented in the allowlist docstring.
- New modules (pure logic, unit-testable): `modules/test_harness.py` (scenario catalog, caps, state machine, report), `modules/guild_snapshot.py` (save/restore). New cog: `discord_bot/cogs/test_harness.py`.
- Note: `modules/admin_snapshot.py` is the dashboard/status snapshot, NOT a server backup, so it is not reusable here. The server snapshot is new work.

## Two kinds of scenarios

### A. Simulated (no damage, run first, most of the value)
Feed the bot's real handlers synthetic events and check what it does. Safe to repeat many times.
- Join burst (N fake members in T seconds) -> antiraid_pro threshold, lockdown, join-gate, verification.
- Scam-link / invite-spam messages from a fresh account -> Scam Shield, automod.
- Mass mention, mass emoji, repeated-text spam -> automod rules.
- Role granted dangerous permissions -> audit/log alerts.
- Checks: which protection fired, how many seconds after the first event, what it did, what it logged.

### B. Real (damage, test server only, snapshot first)
Needed only where Discord's real behaviour matters (audit log timing, role hierarchy, permission errors).
- Channel delete wave (cap 25) -> anti-nuke detection and channel restore.
- Role delete/permission wave (cap 20).
- Mass ban/kick of owned test members (cap 15).
- Webhook spam, invite-delete wave.
- Checks: detected? stopped? attacker punished? logs posted? did the bot recover its own state?

## Snapshot / restore design
- **Saved:** categories, text/voice/forum/stage channels (name, topic, nsfw, slowmode, position, parent), permission overwrites, roles (name, colour, permissions, position, hoist, mentionable), server settings subset (verification level, system channel, rules channel), test-member role assignments.
- **Stored:** JSON in a DB table keyed by run id (small; the DB is ~0.5 GB), with a hash so a corrupt snapshot is detected before any attack starts.
- **Restore:** diff-based. Recreate what is missing, fix what changed, delete what the test added, then verify by re-reading the server and comparing to the snapshot. Report anything that could not be restored.
- **Cannot be restored (stated up front in the report):** message history, deleted messages, bans of real users, webhooks' tokens, integrations. The test server should hold nothing irreplaceable.
- Order matters: roles first, then categories, then channels (parents before children), then overwrites, then positions.

## Report (posted in the test server after each run)
Per scenario: PASS/FAIL, time to first detection, time to containment, what the bot did, what it logged, restore result, and a list of gaps. Also saved to the DB so the history can be shown later (a page on the owner's site is optional and a separate phase).

## Phases
1. Gates + skeleton: `TEST_GUILD_ID`, `test_guild_only` flag, owner/clone checks, the three gate layers, audit logging. Tests prove it refuses everywhere else (wrong guild, non-owner, clone, ID unset, direct handler call).
2. Snapshot/restore with an exhaustive fake-guild test suite; dead-man timer and restart recovery.
3. Simulated scenarios + report (category A).
4. Real scenarios (category B), smallest cap first, one scenario per PR.
5. Optional: run history page on the site; scheduled regression run before each release.

Each phase is its own PR with tests, a handoff note (built / next), and no README change.

## Risks and how they are covered
- *Wrong guild:* three gates, empty ID = off, ID never taken from user text.
- *Model misuse:* AI cannot call anything not in the allowlist; destructive steps need a click from the owner.
- *Restore fails halfway:* snapshot verified first, dead-man timer, restart recovery, honest "could not restore" list.
- *Discord rate limits / flags:* small caps, pauses, stop on first refusal.
- *Code leaks in the public repo:* the scenarios are only dangerous in a server where the bot already has the permissions; the gates make it inert elsewhere. If the repo is public, consider keeping the real scenarios (phase 4) in a private module.
- *Cost:* simulated scenarios call no paid APIs.

## Open items (needed from the owner)
- The test server's ID (`TEST_GUILD_ID`).
- Which protections to cover first (antiraid_pro, automod, Scam Shield, verification, logging, anything else).
- Whether the owner has bot accounts available for real raid joins, or simulated events only for now.
- Default dead-man timer (proposed 15 minutes) and caps (proposed above).

## Built / Next
- Built (phase 1, gates + skeleton): `config.TEST_GUILD_ID` (0 = off), `modules/test_harness.py` (`check_gates`), `AICommandSpec.test_guild_only`,
  offer gate (`get_qualifying_commands`) + execution gate (`resolve_and_check`) in `ai_command_guard`, handler gate in
  `discord_bot/cogs/test_harness.py` (AI-only, never on the slash tree, never loaded on clones), one read-only op `testharness status`,
  audit via `log_action`. Tests: `tests/unit/test_test_harness_gates.py` (wrong guild, non-owner, clone, ID unset, direct handler call). No scenarios, no destructive code.
- TEST_GUILD_ID is now set (test server). Next: owner picks the first protections; then phase 2 (snapshot/restore + dead-man timer + restart recovery) as its own PR.
- Built (phase 2, snapshot/restore): `modules/guild_snapshot.py` (capture, sha256-sealed snapshot, diff-based restore, verify = re-capture and re-plan must be empty; matches
  items by id, then by name/type/parent so anti-nuke's recreated copies are fixed, not duplicated), `test_harness_runs` table + `Database.th_*` (one unfinished run per guild,
  atomic claim), AI-only `testharness snapshot` (Confirm click; read-back verified before it arms) and `testharness stop` (kill switch, no confirm), dead-man timer (default 15 min,
  max 60) and restart recovery (first tick after a restart restores every unfinished run). Handler gate is re-checked before every Discord call during a restore.
  Tests: fake guild (`tests/unit/test_guild_snapshot.py`, `test_test_harness_runs.py`) and real Postgres (`tests/integration/test_test_harness_postgres.py`).
- Not covered by phase 2: member role assignments (no test members exist; simulated events only), threads, managed roles, webhooks/integrations.
- Next: phase 3, simulated scenarios + report (join burst, scam links, mass mention, dangerous role grant). Needs the owner's pick of which protections first.
