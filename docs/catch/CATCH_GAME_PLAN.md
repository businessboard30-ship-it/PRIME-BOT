# PRIME-BOT Creature Catch Game — Full Plan (v6)

**Repository:** https://github.com/businessboard30-ship-it/PRIME-BOT.git (default branch `main`)

One slash command. Everything else is persistent wizards (buttons, selects, modals).
Original creatures, no third-party IP. Built on the patterns already in the repo.

> Working name used below: **Catch Game**. Single command: `/catch` (name is a decision, see Phase 0).
> The bot already registers ~87 of Discord's 100 global commands, so this feature adds exactly **one**.

> **v2 changes:** after a feature review of existing Discord catch-games (PokeMeow in particular), everything missing from v1 was added.
> New or expanded items are tagged **[NEW]**. Phases 6 and 7 are new.
> Appendix A maps every reviewed feature to the phase that covers it. Numbers from other games are *reference only*; tune ours in a spreadsheet.
>
> **v3 changes:** added the **Build and deploy rules** section (mandatory for any AI or developer shipping code), and a new **Phase 9 — Security, reports, bans and review** (reports, temporary/permanent bans, appeals, review queue, kill switches, anti-cheat).
> Owner tools moved to Phase 10 and hardening to Phase 11. v3 additions are tagged **[v3]**.
>
> **v4 changes:** added a **colour and component-style system** (every state change has a matching Discord button or embed colour, defined once in a theme file) and an **Asset Manager panel** in `/admin` for creature art, icons, images and the colour theme, plus an art checklist (Appendix B). v4 additions are tagged **[v4]**.
>
> **v5 changes:** repository URL added. Game art is now stored in a **Discord vault channel** instead of the database (0.16 and 1.6), and a new **database budget** rule (0.12) adds retention and size limits for a small (~0.5 GB) database. v5 additions are tagged **[v5]**.
>
> **v6 changes:** added the **AI build and review protocol** (one AI builds, the other reviews, never the same one), a strict **builder contract** with anti-stub rules and a mandatory completion report for builders that tend to stop early, a copy-paste **builder prompt**, a **review protocol** for fetching and checking the repo, a **task ledger** (Appendix C) that breaks every phase into small tasks with acceptance criteria, and a final completion checklist. v6 additions are tagged **[v6]**.

---

## AI build and review protocol (read before every session) **[v6]**

> **Owner note:** the AI builders used on this project include **Vo AI** and **Claude**. Vo AI has a history of stopping before the work is finished. Everything in this section exists to make unfinished work impossible to hide and unacceptable to submit. The **task ledger** (Appendix C) defines exactly what "finished" means for each task.

### R.1 Roles
| Role | Who | Does |
|---|---|---|
| **Owner** | You | Decides open questions, assigns tasks, pastes prompts, smoke-tests on the test server, merges PRs |
| **Builder** | Vo AI **or** Claude (assigned per task in the ledger) | Builds the task on a branch, runs every check, opens a PR with a completion report |
| **Reviewer** | The **other** AI | Fetches the repo, audits the PR against the task's acceptance criteria and this file, returns a written verdict |

- **Never self-review.** If Vo AI built it, Claude reviews it; if Claude built it, Vo AI reviews it. The owner treats a review with no evidence (no commands run, no file references) as **no review** and asks again.
- **Suggested split (owner's call):** give the code that must never be wrong (atomic claims, trades, payouts, the scheduler, the ban gate: `P1-02`, `P1-04`, `P2-05`, `P5-02`, `P5-03`, `P6-02`, `P9-05`) to whichever AI has been finishing reliably, and review those with extra care. Give smaller, well-bounded tasks (screens, data files, locale keys, tests) to the AI that tends to stop early.
- **Claude as reviewer can:** clone or fetch the repo and a PR branch, read the diff, install dependencies, run `pytest`, compile and lint, run the stub scan and helper scripts, and read the code path by path. **Claude cannot:** run the live bot, click Discord buttons, or see unpushed work. Live checks are the owner's job on the test server, and the reviewer lists the exact steps to do. At the time of writing the repo could be cloned without credentials; if it becomes private the reviewer needs read access.

### R.2 Builder contract (non-negotiable)
Every builder, every session, no exceptions:

1. **FINISH WHAT YOU START.** A task is built until **every** "Done when" criterion in its ledger row is true. "Mostly done", "the main part", "the rest is similar" and "you can extend this" all mean the task **failed**.
2. **NO STUBS.** Forbidden in committed code: `TODO`, `FIXME`, `XXX`, a function body that is only `pass` or `...`, `NotImplementedError`, "placeholder", "implement later", "for brevity", "left as an exercise", "similar to above", commented-out blocks standing in for logic, hardcoded fake data outside tests, and fake success messages for things that don't happen. Buttons that exist must work. Tables that exist must be used. Code that exists must be called.
3. **NO PARTIAL FILES.** Never end a message with a half-written file or a cut-off function. If you are running out of room, finish the function you are in, leave the code in a state that compiles and passes its tests, write the **HANDOFF block** (R.4), and stop at that clean point. Never stop silently and never pretend you finished.
4. **DO NOT ASK PERMISSION TO CONTINUE.** The answer is always *continue*. Only ask the owner a question when an item in **Open questions** truly blocks the task, state your assumption, and keep building everything else.
5. **BUILD, DON'T DESCRIBE.** Output code, tests and files, not summaries of what the code would do.
6. **NO CLAIM WITHOUT PROOF.** You may only write "built" or "done" next to a task with a **completion report** (R.5) that shows the evidence: file paths, function names, test names, command output.
7. **THE CHECKS ARE PART OF THE TASK.** Pull request, DB version check, tests, bug check, component limits, interaction time, colours (see Build and deploy rules). "Not enough time" is never a reason to skip one.
8. **LENGTH IS NOT A LIMIT.** Write the full code. Split a long file into several files, never into several half-done versions of the feature.
9. **DO NOT CHANGE THE PLAN SILENTLY.** If you must deviate, write it under "Deviations" in the PR with the reason.
10. **ONE TASK AT A TIME, IN ORDER.** Finish the lowest-numbered unfinished task assigned to you before starting another. Do not start a new task while one is `[~]`, unless it is blocked and you say why.
11. **DO NOT BUILD ON BROKEN WORK.** If earlier code is wrong, fix it or report it in the PR. Do not work around it.
12. **EVERY SESSION STARTS** by reading this file and `docs/catch/TASKS.md`, finding your first unfinished task, and continuing it. **EVERY SESSION ENDS** with the status of every task you touched.

### R.3 Builder prompt (paste at the start of every Vo AI or Claude build session)
```
You are the BUILDER for the PRIME-BOT Creature Catch Game.
Repo: https://github.com/businessboard30-ship-it/PRIME-BOT.git  (default branch: main)

1. Read CATCH_GAME_PLAN.md completely. Obey "AI build and review protocol" and
   "Build and deploy rules" without exception.
2. Read docs/catch/TASKS.md. Your assigned tasks: <TASK IDs HERE>.
3. Work on branch feat/catch-<task-id>-<slug>. Open a pull request into main.

YOU WILL NOT STOP until every assigned task is BUILT, TESTED and has a completion
report. Partial work is a FAILED task. You are not allowed to:
- leave TODO / FIXME / pass / NotImplementedError / placeholders / "for brevity"
- end a message with a half-written file
- ask "should I continue?" (the answer is ALWAYS continue)
- say "done" without evidence (file paths, test names, command output)
- skip tests, the DB version check, component limits, interaction timing, or colours

Run the stub scan (R.6) and the full test suite BEFORE you report.
If you truly run out of room: finish the current function, make the code compile and
pass its tests, then write the HANDOFF block (R.4) and stop. Never stop silently.

Another AI will review your PR line by line against the task's "Done when" criteria.
Anything missing is rejected. Begin now with the first unfinished task.
```

### R.4 If you must stop: the HANDOFF block
Allowed **only** when you are truly out of room. It must be the last thing in your message and in the PR description:
```
HANDOFF
Task ID(s):
Branch / last commit:
Done (with evidence):
NOT done (exact files, functions, screens, tests):
Exact next step:
Known problems:
Commands to run to see the current state:
```
The next session (either AI) starts from this block and continues. If a builder stops early **twice** on the same task, the owner splits the task into smaller ones. After **three** stops, the task is reassigned to the other AI.

**Continue prompt (owner pastes this when a builder stops):**
```
You stopped before finishing. Do not summarize and do not ask me anything.
Read the HANDOFF block (or docs/catch/TASKS.md) and CONTINUE from exactly where
you stopped, finishing every unfinished item of the task. Then run the stub scan,
the full tests and the Build and deploy checks, and send the completion report.
Stop only when every "Done when" criterion has evidence.
```

### R.5 Completion report (required at the end of every task)
Copy into the PR description:
```
## Completion report
Task ID(s):
Branch / PR:
Files added or changed (one line each, what changed):

### Acceptance criteria (copy each "Done when" item from the ledger)
| Criterion | Evidence (file:function, test name, or command output) |
|---|---|

### Checks run (paste real output)
- pytest summary line:
- compile check:
- lint:
- stub scan (R.6) output (must be empty):
- SCHEMA_VERSION before -> after (or "unchanged"):
- Component-limit test(s) and the worst case they cover:
- Interaction-time test(s) (first await is a response; slow-DB test):
- Colours: theme helper used, state changes update colour:

### Deviations from the plan (with reason):
### Known gaps (honest list; "none" only if true):
### Smoke test steps for the owner on the test server:
```

### R.6 Stub scan (run before saying "done"; the reviewer runs it again)
```
git diff origin/main...HEAD -U0 | grep -nE "^\+.*(TODO|FIXME|XXX|NotImplementedError|placeholder|for brevity|implement later|left as an exercise)"
git diff origin/main...HEAD --name-only --diff-filter=AM | grep "\.py$" | xargs grep -nE "^\s+(pass|\.\.\.)\s*$"
git diff origin/main...HEAD --name-only --diff-filter=AM | grep "\.py$" | xargs python -m compileall -q
pytest -q
```
The first two commands must print nothing (every hit must be explained in the PR, for example a legitimate `pass` in an exception handler with a comment) and the last two must pass. Also search by eye for buttons with no callback, handlers that only say "coming soon", dynamic items never registered in `setup_hook`, tables nothing writes to, and functions nothing calls.

### R.7 Review protocol (what the reviewer does, in order)
1. **Fetch.** Clone the repo (or `git fetch origin`), fetch the PR branch (`git fetch origin pull/<N>/head:pr-<N>`), check it out, read `git log` and the full diff against `main`.
2. **Completeness audit.** For **every** "Done when" criterion of every task in the PR, find the evidence in the code or tests. A criterion with no evidence is a **Blocker**. A task marked built without a completion report is automatically set back to `[~]`.
3. **Stub and fake-code scan.** Run R.6, then read for functions that return constants, buttons with no real handler, success messages for actions that don't happen, unused tables or code, and DynamicItems not registered in `setup_hook`.
4. **Run it.** Install dependencies, run `pytest`, the compile check and the linter. Report the real counts. If something can't run, say exactly why.
5. **Database.** `SCHEMA_VERSION` compared with `main`, migration idempotent and backward compatible, `clone_id` on per-server and per-player tables, schema test present, no image bytes in the database, retention rules respected (0.12).
6. **Component limits.** The `assert_view_within_limits` coverage, custom_id lengths, worst-case data (500+ creatures, longest names), paginated rather than truncated.
7. **Interaction time.** In every callback the first `await` is a response; no DB, Pillow or network work before it; modals are the first response; slow work after the defer; long waits go through the scheduler.
8. **Security.** The ban gate is called, ownership is re-validated from the database (never trusted from a `custom_id`), SQL is parameterized, mentions are escaped, rate limits and idempotency keys exist where required.
9. **Correctness under load.** Atomic claims, trades, swaps and payouts happen in single transactions; restart safety (state in the DB, loops resume); no double payout.
10. **Colours, localization, docs.** Theme helper used (no hardcoded `ButtonStyle` or hex), `catch.*` locale keys, README or `/help` entries, `docs/catch/TASKS.md` updated honestly.
11. **Verdict**, in this format:
```
VERDICT: APPROVE | REQUEST CHANGES | REJECT - INCOMPLETE
Commands run and results:
Acceptance criteria: table of criterion -> PASS / FAIL / NOT EVIDENCED
Findings (each: severity Blocker | Major | Minor | Nit, file:line, what is wrong, how to fix):
Smoke test steps for the owner (what to click on the test server and what should happen):
```
- Any unmet criterion, any stub, any failing test, any skipped check is a **Blocker**. `APPROVE` is only allowed with zero Blockers and zero Majors.
- **Re-review:** the builder fixes every Blocker and Major in the same PR and answers each finding. The reviewer re-checks those findings plus a regression pass. Three failed rounds on one task: the owner reassigns it.
- The reviewer only reviews what is pushed. Unpushed work does not exist.
- **To ask Claude for a review, paste:** `Review PR #<N> (or branch <name>) of https://github.com/businessboard30-ship-it/PRIME-BOT.git for tasks <IDs> against CATCH_GAME_PLAN.md, following R.7. Fetch it, run the checks, and give the verdict format.`

### R.8 Branches, pull requests and merging
- Branch: `feat/catch-<task-id>-<slug>` (for example `feat/catch-p1-06-hub`). PR title: `[P1-06] short description`.
- One task (or a few tightly related tasks) per PR. Do **not** stack new work on a PR that hasn't been approved, unless the tasks are independent.
- Only the owner merges, and only after an `APPROVE`. After the merge the owner (or the reviewer) sets the task to `[R]` in `docs/catch/TASKS.md`.
- Status codes: `[ ]` not started, `[~]` in progress or built but not approved, `[x]` built **with a completion report**, `[R]` reviewed, approved and merged.

---

## Build and deploy rules (read first — applies to every AI assistant and developer) **[v3]**

> Any AI (or person) helping build this feature must follow these rules every time code ships. Do not say "done" until each step below has real evidence in the pull request. If a step could not be run, say so plainly in the PR instead of skipping it.

### B.1 Pull request, never straight to `main`
- Work on a branch named as in R.8 (`feat/catch-<task-id>-<slug>`, or `fix/catch-<area>` for fixes). One task or area per PR, small enough to review.
- Open a **pull request** into `main` of the repository named at the top of this file, using the template in B.8. Do not push to `main` directly and do not merge your own PR; the owner reviews and merges.
- Rebase on the latest `main` before opening the PR and again before merge.
- If an AI has no access to open a PR, it hands over the branch or patch plus the filled-in checklist and says so.

### B.2 Database version check
1. Read `SCHEMA_VERSION` in `database.py` on the latest `main`. It was `"54"` when this plan was written; re-check, never assume.
2. If the PR adds or changes any table, column or index: bump `SCHEMA_VERSION` by exactly one and wire the migration into the normal schema pass, so it runs on boot for the main bot and every clone.
3. If another open PR also bumps the version, resolve the clash after rebasing (take the next number). Two PRs must never share a version.
4. Migrations must be **idempotent** (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, `CREATE UNIQUE INDEX IF NOT EXISTS`) and safe to run twice. They must also be **backward compatible**: never drop or rename a column in the same release that stops using it, so redeploying the previous commit still works.
5. Add or update a test in the style of the existing schema tests (assert `SCHEMA_VERSION >= N` and that the migration is wired).
6. New per-server and per-player tables are keyed for clones (`clone_id`, rule 0.4). Test at least once with a non-main clone id.
7. No schema change? Write "no DB change, version unchanged" in the PR.

### B.3 Run the tests
- `pytest` (unit, integration and security suites) must pass locally **and** in CI. Paste the summary line into the PR.
- New logic needs new tests: pure game logic in `modules/catch_*.py`, concurrency tests for atomic claims, transaction tests for trades and swaps, and odds simulations for every drop table.
- Never delete, skip or weaken a failing test to get green. If a test is wrong, say so and fix it in its own commit with the reason.
- The repo has no PR test workflow today (`.github/workflows` only holds cron and deploy jobs). Adding a `ci.yml` that runs `pytest` and a compile check on every PR, and requiring it before merge, is a **Phase 1 task**.

### B.4 Bug check
Before opening the PR the author reviews its own diff and runs:
- `python -m compileall` on changed files, plus a linter (`ruff` or `pyflakes`, added with CI).
- A manual pass over this list: exceptions are caught and logged with guild/user/feature context; works in a server **and** in DMs where relevant; every query is parameterized; mentions are escaped (`AllowedMentions.none()`); a cache miss fetches instead of crashing on `None`; `clone_id` is passed everywhere; no DB or network call inside a tight loop; background loops resume from the DB after a restart; every ID read from a `custom_id` is re-validated against the user who clicked (never trust it).
- Anything unchecked or incomplete goes under "Known gaps" in the PR. Do not hide it.

### B.5 Component limit check
Discord rejects a message that breaks its limits, and the failure only shows at runtime. Test every view with worst-case data (500+ creatures, longest names, maximum options). The limits for classic components:
- Max **5 action rows** per message; each row holds up to **5 buttons** or **1 select menu** (so at most 25 buttons).
- Select menu: max **25 options**; option label, value and description ≤ **100** characters.
- `custom_id` ≤ **100** characters; button label ≤ **80**. Our `catch:<action>:<guild_id>:<clone_id|->:<arg>` ids must stay under 100 even with long args, so test the longest case.
- Modal: max **5** inputs; title ≤ **45** characters.
- Embed: title 256, description 4096, 25 fields, field value 1024, footer 2048, **6000 characters in total**; max 10 embeds per message. Message content ≤ 2000 characters.
- Paginate instead of silently truncating. Add an `assert_view_within_limits(view)` test helper and run it on every view, every page and every state. (The repo's existing view tests already check component counts; follow that pattern.)
- Re-verify these numbers against the current Discord docs when the library version changes. Components V2 has different limits; check the docs before using it.
- **[v4] Colours:** buttons get their style from the theme helper (rule 0.10), never a hardcoded `ButtonStyle` or hex value. Destructive actions are `danger`, confirmations are `success`, and a state change (selected, on/off, used, expired, locked) updates the colour. Add a test that every view's buttons use the mapped roles.

### B.6 Interaction time check
- Every callback must acknowledge within **3 seconds**: the first `await` is `interaction.response.defer(...)` or `send_message(...)`, **before** any DB read, Pillow render or network call.
- After the defer, do the work (heavy rendering through `asyncio.to_thread`), then `edit_original_response` / `followup`. A follow-up is valid for **15 minutes**; anything longer (catchbot, eggs, timers) goes through the scheduler and a fresh message, never a held-open interaction.
- A modal must be the **first** response (you cannot defer and then open a modal).
- Persistent views use `timeout=None` and `DynamicItem`s registered in `setup_hook`, so they work after a restart.
- Test it: for each callback, assert the first awaited call is a response, and run it against a simulated slow DB (for example +2 s) to prove it still answers in time. Log time-to-first-response in staging and investigate anything over 1 s.

### B.7 Other deploy rules
- **No new environment variables for non-secret settings.** Put them in `config.py`; Railway env vars are for secrets only.
- Deploy order: merge → test server first → watch the logs for 10+ minutes (tracebacks, slow callbacks) → a few friendly servers → everyone. The feature stays behind the owner toggle.
- Every PR states its **rollback plan**. Because migrations are backward compatible, redeploying the previous commit must still work.
- Never commit tokens, `.env` files, database dumps or user data.
- Update the README, `/help` and the `catch.*` locale keys for any new player-facing text.

### B.8 Pull request template (copy into every PR)
```
## What / why
## Phase / area
## Task ID(s) from docs/catch/TASKS.md
## Builder / reviewer (Vo AI or Claude; never the same)
## Completion report (R.5) attached: yes / no
## DB
- [ ] SCHEMA_VERSION checked on latest main (was ___, now ___ / unchanged)
- [ ] Migration idempotent, clone_id keyed, backward compatible
- [ ] Schema test added or updated
## Tests
- [ ] pytest passes locally (paste summary line)
- [ ] New tests for new logic; none deleted or skipped
## Bug check
- [ ] compile + lint clean
- [ ] Stub scan (R.6) prints nothing; no TODO/FIXME/pass/placeholder left
- [ ] Self-review done (errors logged, params bound, mentions escaped, ids re-validated)
## Components
- [ ] Views tested with worst-case data: rows <=5, buttons <=5 per row, select <=25 options, custom_id <=100, embed <=6000
- [ ] Colours come from the theme helper (no hardcoded ButtonStyle/hex); state changes update colour; destructive = danger
## Interaction time
- [ ] First await in every callback is defer/response
- [ ] Slow-DB test passes; modals sent as first response
## Deploy
- [ ] No new env vars (config.py used)
- [ ] Rollback plan: ___
## Known gaps
```

---

## 0. Ground rules (apply to every phase)

### 0.1 One command, everything else is a wizard
- `/catch` opens the **Hub** (ephemeral): Collection · Catch settings · Shop · Trade · Market · Daily · Quests · Leaderboard · Help, plus the **[NEW]** areas below (Encounter, Catchbot, Fishing, Eggs, Hunt, Swap, Crew, Lottery, Achievements, Contests, Alerts, Bonuses, News).
- Every other action is a **persistent component** (`discord.ui.DynamicItem`) with a `custom_id` that encodes what it needs, e.g. `catch:<action>:<guild_id>:<clone_id|->:<arg>`. Same encoding style as `_views_welcome.py` (`_encode` / `_decode`).
- Register all dynamic items once in `bot.py` `setup_hook` via `add_dynamic_items(...)`, like the other wizards, so buttons keep working after a restart and for messages sent by any process.
- Owners configure the game from the **join DM** ("Turn on: Creature catching" in `FEATURE_TOGGLES`) and the **Server Owners Panel** (`/serversetup`), never from extra commands.

### 0.2 Interaction rules (so nothing times out)
- Discord drops any interaction not answered in **3 seconds**. Every callback does `defer()` (or `send_message`) **first**, then does DB work, then `edit_original_response` / `followup`.
- No live network calls (AI, external APIs) before the first response.
- Public spawn messages use **public** buttons; everything personal (collection, shop, settings) is **ephemeral**.

### 0.3 Permissions
- Player actions: anyone in the server.
- Setup/settings: owner or **Manage Server**, checked with `user_can_manage_guild` (works from DMs, uses owner ID, fetches members instead of trusting the cache).
- Bot permission problems are reported with `perm_check` helpers, naming exactly what to fix.

### 0.4 Data and clones
- Every table is keyed by `(guild_id, clone_id)` where it is per-server, and by `(user_id, guild_id?, clone_id)` where it is per-player. Clones never read the main bot's data.
- Tables are created with `CREATE TABLE IF NOT EXISTS` in `database.py`'s schema pass, plus a unique index per natural key (needed for `ON CONFLICT`).
- Writes that two players can race on (catching) use a single atomic `UPDATE ... WHERE caught_by IS NULL RETURNING` so only one player can win.

### 0.5 Performance
- Cache species data and per-guild config in memory with short TTLs (same idea as the honeypot's 60 s settings cache).
- `on_message` must be cheap: a dict counter per `(guild, channel)`, no DB read on the hot path unless a spawn is due.
- Render creature cards with Pillow off the event loop (`asyncio.to_thread`), and cache rendered card images by `(species, shiny, level-band)`.
- **[NEW]** Bulk work (catchbot results, egg/lootbox batches) is rolled in memory and written in **one transaction**, never one row-write per roll.

### 0.6 Safety / abuse
- **[v3]** This is the short list. The full system (reports, bans, temporary bans, appeals, review queue, kill switches, anti-cheat flags) is **Phase 9**, and its minimum parts must exist before public beta.
- Spawn counters only count **human**, non-command messages with a minimum length and a per-user rate limit.
- Per-server and per-user catch cooldowns, daily catch caps for new accounts, and an audit row for every trade, market sale and admin grant.
- Alt-farming guard: trades and gifts between accounts younger than N days are limited.
- **[NEW]** Auto-click guard: the Encounter and Fishing buttons have cooldowns, and after long unbroken streaks the player gets a one-button "are you there?" check.
- **[NEW]** All odds (lootboxes, eggs, swap, lottery, catch rate) are rolled **server-side only** with Python's `secrets`/`random.SystemRandom`, and drop tables live in data files so they can be audited and tuned.
- **[NEW]** Swap, lottery and crew perks have anti-farm rules: swap rules (no locked/favorite/buddy creatures, daily token cap), lottery ticket cap per user, crew-hopping cooldown (perks start 24 h after joining), contests exclude flagged or too-new accounts.

### 0.7 Localization **[NEW]**
- All player-facing text goes through the existing `i18n.py` `t(key, lang, ...)` and the `locales/*.json` files (en, es, fr, de, it, pt, ru, ar, ja, sw) from day one, using a `catch.*` key prefix. Retrofitting later is far more expensive than doing it now.
- Species and item names have a name key per locale; fall back to English when a translation is missing.

### 0.8 Originality **[NEW]**
- Mechanics (cooldown encounters, idle catching, daily targets, towers, clans, etc.) are common game patterns and fine to build.
- **Names, art, species, items and ladder titles must all be original.** Do not reuse another franchise's terms for the battle ladder, forms, rarities or items.

### 0.9 One catch pipeline **[NEW]**
- Every successful catch (wild spawn, encounter, fishing, catchbot, egg hatch, swap result) goes through a single function, `record_catch(...)`, in `modules/catch_game.py`'s service layer. It updates, in one transaction: ownership, dex, player stats, streak, egg progress, hunt completion, quest progress, achievement counters, crew catch counter, contest counter.
- Each source declares which counters it feeds (for example, fishing feeds eggs but not quests; catchbot feeds the dex but not streaks). That table is a design decision (see Phase 0) and lives in one place.

### 0.10 Colours and component style **[v4]**
Discord only gives buttons **four colours plus a link style**, so colour has to be used on purpose and the same way everywhere:

| Style (Discord name) | Looks like | Use it for |
|---|---|---|
| `primary` | blurple | The main action on a screen, and the **currently selected** choice (active filter, current tab) |
| `secondary` | grey | Neutral navigation (Back, Home, Prev/Next, page jump), unselected options, and anything **disabled** |
| `success` | green | Confirm, claim reward, accept trade, a toggle that is **on**, "ready" states |
| `danger` | red | Release, delete, ban, cancel trade, and the confirm step of any destructive action |
| `link` | grey, opens a URL | Support server, vote page, docs |

- **State changes change colour.** Examples: a **Throw ball** button is `primary`, then becomes disabled `secondary` once used; a toggle flips `secondary` ↔ `success`; the first press of **Release** turns the button red and relabels it "Confirm release?" (`danger`) before anything is deleted; in a trade, a side that locked its offer shows a `success` check.
- **Embed colours** can be any hex value, so they carry more meaning than buttons: a catch reveal uses the creature's **rarity colour**, a failed catch uses red, a despawned creature uses grey, a success message uses green, a warning uses yellow, and info uses blurple. Elements can have their own accent colour on the creature card.
- **Select menus and modals cannot be coloured.** For extra variety use an emoji in the label or option (a coloured square or an icon) and the embed colour beside it.
- **Defaults** (placeholders, tune later; Discord's brand colours for the states): success `#57F287`, danger `#ED4245`, warning `#FEE75C`, info `#5865F2`, expired/disabled `#4F545C`; rarity from grey (common) through green, blue, purple, gold, with distinct colours for **shiny** and the **special tier** (a gradient isn't possible, so pair the colour with an icon or badge on the card).
- **Defined once.** All colours live in `data/catch/theme.json`, read through `modules/catch_theme.py` (`button_style(role)`, `rarity_color(rarity)`, `element_color(el)`, `state_color(state)`). No view hardcodes a `ButtonStyle` or a hex value. The owner can edit the palette from the Asset Manager panel (see 1.6).
- **Accessibility:** never rely on colour alone. Pair it with a word, emoji or icon (✅ ❌ ⭐), keep text readable on both light and dark themes, and show rarity by name and icon as well as colour.
- Server owners cannot recolour rarities (consistency across servers). Premium can add cosmetic card **frames** only.

### 0.11 Art, icons and images **[v4]**
- All creature art, item icons, badges, card frames and banners are managed through the **Asset Manager panel** (spec in 1.6), not by editing code or redeploying.
- Anything without art shows a generated **placeholder**, so the game never displays a broken image and can launch before every picture exists.
- Only use art you own or have a licence for. Every asset records its source/licence note (see `catch_assets`).

### 0.12 Database budget **[v5]**
The database is small (about **0.5 GB**), so the game is designed to stay well inside it:
- **Images never go in the database.** Art lives in the Discord asset vault (0.16, 1.6); the database stores only a channel id, message id and metadata.
- **The real growth is rows, not pictures.** Every caught creature is a row in `catch_owned`, plus its indexes. As a rough order of magnitude (measure with real data), a few hundred bytes per creature means ~500k creatures could take around 150 MB. So:
  - Keep columns compact (small integers, pack the hidden stat variation into one or two numbers), no JSON blob per creature, and only the indexes that are actually used.
  - Give each player a **box capacity** (decision, with upgrades from the shop/crew perks) and a **release-duplicates** tool that converts extras into coins or candy. This also becomes an economy sink.
- **Retention rules** (run by the scheduler, Phase 1.5):
  - `catch_spawns`: delete resolved or expired spawns after N days.
  - `catch_reminders`: delete delivered rows.
  - `catch_audit`: keep N days of hot rows, then **archive** older rows as a text/JSONL file posted to the vault channel and delete them from the database.
  - Analytics are aggregated daily into `catch_stats_daily` instead of storing every event.
  - Catchbot and fishing results create creature rows only for what the player keeps, with no per-roll log.
- **Monitoring:** the admin Overview shows row counts and size per `catch_*` table and warns the owner at 70% and 85% of the limit.
- Retention jobs must never delete rows that are still live (an open spawn, a pending trade, an unresolved report). Reports, sanctions and appeals are kept until the owner's retention decision (see Open questions).

---

## Phase 0 — Design decisions (before any code)

**Goal:** lock the game's identity and economy so later phases don't churn.

| # | Decision | Recommendation |
|---|----------|----------------|
| 0.1 | Command name | `/catch` (check it doesn't clash; none exists today) |
| 0.2 | Theme/name of creatures | Original theme (anime-adjacent fits the bot). Avoid any real franchise names, designs or sprites |
| 0.3 | Launch roster | 40–60 species across 5 rarity tiers; add in batches later |
| 0.4 | Art pipeline | One consistent style; reuse the level-card art approach. Silhouette + full colour versions of each |
| 0.5 | Currency | **[CHANGED]** With a global collection, global market and crews, per-server coins stop making sense as the main currency. Recommendation: a **game-wide coin** earned from catching, with server economy coins (`adjust_economy_balance`) kept for the server shop only, optionally convertible with a daily cap. Secondary tokens: swap tokens, fishing tokens |
| 0.6 | Scope of data | Per-server spawns, **global** collection so players keep progress |
| 0.7 | Monetization stance | Free core game. Premium only changes convenience (spawn rate in your server, extra channels, slightly shorter cooldowns, extra slots), never owning creatures or odds of rare ones |
| 0.8 | **[NEW]** Encounter model | Support **both**: chat-triggered spawns *and* a player Encounter button on a cooldown (so quiet servers still work) |
| 0.9 | **[NEW]** Market scope | Global listings with a per-server browse filter; fees and price floors |
| 0.10 | **[NEW]** Rarity ladder | 5 tiers + shiny + one extra **special** tier above shiny (original name TBD). Special tier is earned only (swaps, challenges, events, giveaways), never sold |
| 0.11 | **[NEW]** Obtain sources | Every species gets an `obtain_via` tag: wild / egg / swap / fish / event / vote / battle / giveaway. Powers "exclusives" lists and dex hints |
| 0.12 | **[NEW]** Habitats | Tag species with a habitat (land, water, etc.) so fishing has its own pool |
| 0.13 | **[NEW]** Group name | The repo already uses "clan" for Discord-server clan cards (`modules/clan_cards.py`, `discord_clan_cards`). Call the player groups something else (e.g. **Crews**) and decide whether the two systems ever link |
| 0.14 | **[NEW]** Counter matrix | Table of which catch source feeds which counters (quests, streak, eggs, dex, achievements, crew, contest) — see 0.9 pipeline |
| 0.15 | **[NEW]** Original names | Final names for: ladder tiers, transformation forms, rarity names, items |
| 0.16 | **[v5]** Where art is stored | Launch art is committed under `assets/catch/` (versioned with the code, like `assets/images`). **Everything uploaded through the panel is stored in a private Discord vault channel**, not the database. The database keeps only `(channel_id, message_id)` plus metadata, and a fresh link is fetched from that message whenever needed, because Discord attachment links expire. This is the same pattern the repo already uses for ad images (`discord_bot/ad_images.py`) and welcome custom backgrounds (`image_host_channel_id`). Vault files override repo files; if the vault can't be reached the bot falls back to the repo art, then to the placeholder |
| 0.17 | **[v4]** Who makes the art | Decide: drawn by you, commissioned, or generated. Whichever it is, keep a source/licence note per asset and only ship art you have the rights to |
| 0.18 | **[v4]** Clones and assets | Recommendation: one global asset set shared by all clones (read-only for clones). Per-clone skins are out of scope for now |
| 0.19 | **[v4]** Colour palette | Approve the default rarity, element and state colours in `theme.json` (rule 0.10) |

**Deliverables:** rarity table, roster list, stat model, catch-rate formula, art style sheet, **[NEW]** currency map, counter matrix, drop tables, name sheet, **[v4]** colour palette (`theme.json`) and the art checklist (Appendix B).
**Done when:** these are written down in `docs/` and agreed.

---

## Phase 1 — Foundation

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** the single command opens a working hub; data model exists; owners can turn the game on.

### 1.1 Data model (new tables)
- `catch_species` — id, name, rarity, element/type, base stats, art key, evolves_from/evolves_at, spawn_weight, enabled. **[NEW]** Also: `habitat`, `obtain_via`, `egg_pool` flag, `form_of` (for transformations later).
- `catch_guild_config` — guild_id, clone_id, enabled, spawn_channel_ids, spawn_every_n_messages, min_seconds_between_spawns, despawn_seconds, announce_channel_id, spawn_rate_multiplier. **[NEW]** Also: encounter channel ids, encounter cooldown override, rare-spawn ping role id.
- `catch_spawns` — id, guild_id, clone_id, channel_id, message_id, species_id, level, shiny, spawned_at, expires_at, caught_by, caught_at. **[NEW]** Also: `owner_user_id` (null for public spawns; set for player encounters), `source` (chat / encounter / fishing).
- `catch_owned` — id, user_id, clone_id, species_id, level, xp, nickname, shiny, favorite, stats/IVs, caught_in_guild, caught_at, locked. **[NEW]** Also: `rarity_tag` (normal/shiny/special), `source`.
- `catch_dex` — user_id, clone_id, species_id, first_caught_at, count (powers dex completion).
- `catch_players` — user_id, clone_id, daily_streak, last_daily_at, total_catches, settings. **[NEW]** Also: coin balance, `encounter_ready_at`, `fish_ready_at`, swap_tokens, fishing_tokens, catch_streak, buddy_id.
- `catch_inventory` — user_id, clone_id, item_key, quantity (balls, berries, boosts, evolution items, lootboxes, eggs as items).
- `catch_audit` — who/what/when for trades, market, admin actions. **[NEW]** Also: swaps, lottery draws, crew bank moves.
- **[NEW]** `catch_reminders` — user_id, kind, due_at, delivered, delivery (dm/channel), dedupe key. Used by every timed feature.
- **[v4]** `catch_assets` — id, kind (creature / silhouette / thumb / item_icon / rarity_frame / card_bg / egg / badge / trainer_icon / banner / emblem), key (species id or item key), variant (normal / shiny), version, status (placeholder / draft / live), content_hash, mime, width, height, size_bytes, vault_channel_id, vault_message_id, emoji_id (for synced icons), source_note, uploaded_by, uploaded_at. **[v5]** No image bytes in this table.
- **[v4]** `catch_theme` — key, hex value, updated_by, updated_at (overrides `theme.json` defaults).
- **[v5]** `catch_stats_daily` — day, guild_id, clone_id, spawns, encounters, catches (aggregates kept after the raw rows are purged, see 0.12).

### 1.2 Code layout
- `discord_bot/cogs/catch.py` — the cog and the single `/catch` command, plus the `on_message` spawn counter.
- `discord_bot/cogs/_views_catch_*.py` — hub, collection, shop, trade, etc. (one file per area, like `_views_welcome.py`).
- `modules/catch_game.py` — pure game logic (spawn rolls, catch odds, stats, evolution). No Discord code, so it is unit-testable.
- `modules/catch_cards.py` — Pillow renderers.
- `data/catch/species.json` — roster source of truth, loaded into `catch_species` on boot.
- **[NEW]** `modules/catch_service.py` — the `record_catch` pipeline and other multi-table operations.
- **[NEW]** `modules/catch_loot.py` — lootbox, egg, swap, lottery rolls (pure functions).
- **[NEW]** `modules/catch_idle.py` — catchbot and fishing logic (pure).
- **[NEW]** `modules/catch_crews.py`, `modules/catch_battle.py` — added in their phases.
- **[NEW]** More data files: `lootboxes.json`, `eggs.json`, `achievements.json`, `shop.json`, `quests.json`, `hunts.json`, `swap.json`, `clan_tiers.json` (crews), `tower.json`, `contests.json`.
- **[NEW]** Locale keys under `catch.*` in `locales/*.json`.
- **[v4]** `modules/catch_theme.py` (colours and button styles, rule 0.10), `modules/catch_assets.py` (load with cache and placeholder fallback; validate and normalize uploads, pure functions so they're unit-testable), `data/catch/theme.json`, and `assets/catch/` for launch art.
- **[v4]** `scripts/import_catch_assets.py` — bulk-import a folder of launch art into `catch_assets` (uploading 50+ creatures one by one through the panel would be slow).

### 1.3 Hub wizard
- `/catch` → ephemeral hub with buttons per area. Hub state lives in the message; no per-user memory needed.
- Back/Home buttons on every sub-screen (same swap-in-place pattern as the welcome sub-screen).
- **[NEW]** Discord allows at most 25 components per message, and the hub now has more areas than that. Use a **category select** at the top (Play · Collect · Social · Economy · Battle · Info) that swaps the button rows below it. Only the most-used buttons (Encounter, Collection, Daily) stay pinned.

### 1.4 Owner setup
- "Turn on: Creature catching" in the join DM `FEATURE_TOGGLES`.
- Setup screen: pick spawn channels (ChannelSelect in a server, a plain select when opened from a DM), spawn speed preset (Slow / Normal / Fast), **Create #wild-zone** button, test spawn button.
- **[NEW]** Also: toggle the Encounter button and pick where its panel is posted, set a rare-spawn ping role.
- Entry in the Server Owners Panel with an on/off status line.

### 1.5 Shared engines **[NEW]**
- **Cooldown helper:** one function to check/set "ready at" timestamps (encounter, fish, daily, swap, hunt, catchbot) with friendly "ready in 2m 10s" text.
- **Scheduler:** one background task that handles all timed events (spawn despawn, egg hatches, catchbot finish, hunt rollover, lottery draw, contest end, tower reset, reminders, **[v5] retention purges and archiving**). It reads due rows from the DB, so a restart loses nothing.
- **Reminder engine:** opt-in DMs or channel pings from `catch_reminders`, deduped, with quiet hours (the Alerts screen is built in Phase 6).
- **i18n wiring** (see 0.7).
- **[v3] Player gate + kill switches:** one function, `check_player_allowed(user_id, guild_id, action)`, called at the top of every callback (after the defer). In Phase 1 it only checks a global/per-server feature flag table (`catch_feature_flags`), so the owner can switch parts of the game off instantly. Phase 9 plugs bans and restrictions into the same function, so no callback has to change later.
- **[v3] CI:** add `.github/workflows/ci.yml` (pytest + compile check on every PR) as described in B.3.

### 1.6 Asset Manager panel **[v4]**
Lives in the owner `/admin` panel (not a new slash command, and not in the player hub). Visible to the bot owner and delegated **art admins** only. Every action is audited.

**What it manages**
| Asset | Notes |
|---|---|
| Creature art (normal, optional shiny) | Transparent PNG/WebP, one square size for all (e.g. 512×512, final size TBD) |
| Silhouette and thumbnail | **Generated automatically** from the full-colour art with Pillow; can be overridden |
| Item and ball icons | Small square icons; also **synced as application emojis** so they can appear inside buttons and select options |
| Rarity frames and card backgrounds | Overlays per rarity, per element, and for the special tier |
| Eggs, lootboxes, rods, catchbot | One image per type/tier |
| Achievement badges, battle/trainer icons | Square icons |
| Event banners and hub header | Wide images |
| Crew emblems | Only if crews get emblems |
| **Colour theme** | Rarity, element and state colours (rule 0.10) |

**Screens**
1. **Overview:** counts per asset type, **% of the roster with live art**, a list of species/items still on placeholders (with a jump button), and warnings (oversized, wrong format, unused assets).
2. **Browse and preview:** pick a type, then a species or item. Shows the current asset and a **preview rendered through the real card renderer**.
3. **Upload / replace:** the owner sends the image (attachment or direct link; reuse the approach in the Customize Card wizard). It is posted into the **asset vault** (see below) and saved as a **draft**.
4. **Draft → Publish:** preview the draft on a real card, then press **Publish**. A bad image never goes live by accident.
5. **History and revert:** the last N versions of each asset are kept; **Revert** puts a previous one back.
6. **Icons:** a **Sync icons** button uploads item icons as application emojis and stores their ids; if a sync fails or the limit is reached, the bot falls back to a plain unicode emoji.
7. **Theme editor:** edit rarity, element and state colours with a hex modal (validated), see a sample embed, **Reset to default**. Saved in `catch_theme`.
8. **Roster gate:** an optional switch that stops a new species from being enabled until its art is live.

**Upload checks and processing**
- Allowed formats PNG, WebP, JPG; static only (no animated files); size cap (e.g. 2 MB); minimum and maximum dimensions; creature art must have transparency; reject duplicates by content hash.
- The bot normalizes each upload (trim, centre, resize to the standard size, convert to PNG/WebP), generates the silhouette and thumbnail, and stores the result.
- Each asset keeps a **source/licence note** (see 0.11).
- Rendered card images are cached by `(asset id, version)`, so publishing or reverting automatically invalidates the cache.
- Uploads are rate-limited, size-checked, and never executed or trusted as anything but image data.

**Asset vault (Discord channel storage) [v5]**
- **Reuse what the repo already does.** `discord_bot/ad_images.py` re-posts an image into the image-hosting channel, stores only `(channel_id, message_id)`, and re-fetches a live URL from that message when needed. Do the same for game art. Give game art its **own vault channel(s)** (set from the panel and saved in the global settings table, not as another environment variable) so it doesn't mix with ad images; optionally one channel per kind (creatures, icons, frames, banners, backups).
- **Private place.** The vault is a channel in a server only you and the bot can see. The bot needs View Channel, Send Messages, Attach Files and Read Message History; check it with the `perm_check` helpers and name exactly what's missing.
- **Self-describing uploads.** Each file is posted with a short caption (kind, key, variant, version, content hash, source note). A **Rebuild index from vault** button can recreate `catch_assets` rows from the channel if the table is ever lost.
- **Never fetch from Discord before the first response.** Images are loaded into an in-memory/disk **cache** keyed by asset id and version. Live assets are warmed at boot, and anything else is fetched lazily *after* the defer. Rendered cards are cached on top of that.
- **Limits to design around.** Keep uploads small (cap at 2 MB; the ad-image helper allows 8 MB), upload one file at a time with retry and backoff, and run bulk imports slowly in the background.
- **Risks, stated plainly.** If the vault channel or server is deleted, or the bot loses access, uploaded art is gone from there; Discord also could change how attachments and links work. So treat the vault as the *primary* store but keep your own copies: the original files on your computer, launch art in the repo, a **Backup** button that exports all live assets, and the repo/placeholder fallback.

**Phase 1 minimum:** the vault channel setup, the loader with placeholder fallback, the theme helper, the bulk import script, basic upload/replace with draft → publish, and the Overview with missing-art list. **Later (Phase 10):** history/revert, vault backup and index rebuild, icon sync, the theme editor, usage warnings, art-admin delegation.

**Done when:** owner can enable it, see a test spawn, and `/catch` opens a hub that survives a bot restart; **[v4]** a species with no art shows a placeholder instead of an error, and uploading and publishing art changes the next rendered card.

---

## Phase 2 — Core loop: spawn and catch

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** creatures appear, players catch them, ownership is recorded exactly once.

### 2.0 Player encounters **[NEW]**
- An **Encounter** button in the hub, plus an optional persistent **encounter panel** message in the spawn channel (public button) so a channel can play like command-based catch bots.
- Pressing it rolls a creature **for that player only**: the spawn row has `owner_user_id`, and only that player can throw balls at it. Cooldown from `encounter_ready_at`; reminder optional.
- Works on top of chat spawns; owners can enable either or both.
- Modifiers that apply to encounters and chat spawns: hunt target boost (Phase 4), repel/lure/radar effects, clan/crew perks, active events.

### 2.1 Spawning
- Triggers: message counter per channel (threshold with jitter), minimum gap, and a slow timer fallback for quiet servers.
- Species roll uses `spawn_weight` × rarity × server multiplier; small shiny chance; level roll by server activity or a flat range.
- Spawn message: embed with silhouette, rarity colour, and persistent buttons **Throw ball** (opens ball picker) and optionally **Flee**. Message id and expiry saved to `catch_spawns`.
- Despawn: a background loop edits expired spawns to "it ran away" and disables the buttons. On restart the loop resumes from the DB.
- **[NEW]** Rare spawns can ping the server's configured role (opt-in) so players know to come.

### 2.2 Catching
- Press **Throw ball** → ephemeral ball picker (Basic / Great / Ultra / Master from inventory; Basic is unlimited). **[NEW]** The picker also offers a berry (catch-rate boost) before the throw.
- Catch chance = f(rarity, level gap, ball, **[NEW]** berry, **[NEW]** crew perk, streak, shiny). Resolved server-side in `modules/catch_game.py`.
- Atomic claim: only the first successful claimant wins; others get "someone caught it first".
- Success: public message edits to the reveal card (full colour art, stats, who caught it); failure: creature may flee based on rarity; player gets a cooldown notice. **[v4]** Colours follow rule 0.10: the reveal embed uses the rarity colour, a failed throw uses red, a despawn turns grey, and the **Throw ball** button disables itself (grey) once the spawn is resolved.
- First time catching a species updates the dex and shows a "New dex entry" line.

### 2.3 Rewards
- Coins/XP for catching; small streak bonus for consecutive catches; first-catch bonus per species.
- **[NEW]** All of this, plus egg progress, hunt completion, quest and achievement counters, crew and contest counters, runs through the single `record_catch` pipeline (0.9).

**Done when:** two people pressing at the same moment can never both win; restarting the bot mid-spawn keeps buttons working and expiry correct; **[NEW]** a catch credits every counter exactly once, even if the callback is retried.

---

## Phase 3 — Collection

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** a collection screen players enjoy using. This is the centerpiece.

### 3.1 Box view
- Paginated grid/list with **Prev/Next**, page jump modal, and per-page size.
- **Filter** selects: rarity, element, shiny, favorite, level range, "not in a trade". **[NEW]** Also: habitat, obtain source, special tier.
- **Sort** select: newest, level, rarity, name, favorite first.
- **Search** modal (name/nickname).

### 3.2 Creature detail
- Select a creature → detail screen with the rendered **creature card** (art, level, stats, shiny badge, caught date/server).
- Actions: Favorite, Nickname (modal), Lock (blocks release/trade), Release (two-step confirm, gives coins), Set as **Buddy** (shown on the player's profile card).

### 3.3 Dex
- Dex screen: grid of all species with silhouette for unseen, colour for owned, completion % per rarity and element, completion rewards.
- **[NEW]** Unseen exclusives show a hint of *how* they're obtained (egg, event, swap...) once discovered or after a milestone, driven by `obtain_via`.

### 3.4 Profile
- Player profile card: total catches, rarest catch, dex %, buddy, streak, badges. **[NEW]** Badges now come from the Achievements system (Phase 4); also shows crew and battle icon.

### 3.5 Checklist and species info **[NEW]**
- **Checklist:** per rarity/element/habitat list of species you're still missing.
- **Species info:** a search modal that opens a species page (stats, element chart, evolution line, how it's obtained, whether you own it).
- **Lists:** public "exclusives / shinies / special" lists so players know what exists.

**Done when:** a player with 500+ creatures can filter, sort, and page without any interaction timing out.

---

## Phase 4 — Progression

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** creatures become worth keeping and improving, and players have daily goals.

- **XP and levels:** creatures gain XP from the buddy slot, from chat activity (light), from battles later, and from XP items. **[NEW]** The buddy gains XP from catching, fishing, quests and egg hatching.
- **Evolution:** at level or with an item; evolution screen with a preview card and a confirm button; keeps nickname and favorite.
- **Stats:** base + hidden variation per creature (kept modest), shown as bars on the card.
- **Items** (stored in `catch_inventory`): balls, XP candy, evolution stones, incense, lures. **[NEW]** Full list now includes:
  - **Repel:** fewer common spawns for N encounters, so rarer ones show up more often.
  - **Berry:** raises catch chance for one throw.
  - **Radar:** reveals or pings a rarity range for the next spawn.
  - **Coin booster:** multiplies coin gains for a time.
  - **Quest reset scroll:** rerolls a quest.
  - **Level-up candy:** rare, jumps one level.
- **Lootboxes [NEW]:** earned from votes, quests, achievements, events and crew goals. Open screen with a data-driven drop table (balls, coins, repel, berry, egg, quest scroll, ultra-rare candy). The odds are shown to the player. Table in `lootboxes.json`.
- **Eggs [NEW]:** hold 1+ eggs (slots grow with perks); an egg hatches after N **catches** while held (N depends on egg tier). Hatch pools are rare-or-better and can be event-specific. Eggs screen shows progress bars. Counted via `record_catch`.
- **Hunts [NEW]:** a daily target species (rolls at the daily rollover) that spawns more often for that player; catching it pays a reward scaled by rarity. Hunt streak counted for achievements.
- **Daily:** `Daily` button in the hub: free ball + coins, with streak bonuses. **[NEW]** Also grants free swap tokens.
- **Quests:** daily/weekly quests ("catch 3 rare", "evolve 1 creature"), claimed from a Quests screen. **[NEW]** Quest reset scroll.
- **Achievements [NEW]:** data-driven goals across categories (catching, collection, battle, general, social), a progress screen, claimable rewards, **badges** shown on the profile, and one completionist badge for finishing nearly all of them. Source of truth: `achievements.json`; counters fed by `record_catch` and battle/social events.

**Done when:** a new player has clear goals for their first week and the numbers are tuned in a spreadsheet, not guessed; **[NEW]** drop tables are simulated (1M rolls) and match their data files.

---

## Phase 5 — Economy and social

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** players interact with each other, and coins have sinks.

- **Shop:** coins buy balls and items; rotating daily stock; purchase confirm buttons.
- **Trading:** invite another player via a user select; both sides add creatures/items through selects; **both must press Confirm** on a locked offer; trade is one DB transaction; audit row written. Trades expire after N minutes.
- **Marketplace:** list a creature for coins (modal for price), browse with filters, buy with confirm. Price floors and fees to curb abuse. Reuse the pagination patterns from `cards.py`'s `MarketBrowserView`. **[NEW]** Listings are global by default with a "this server only" filter (decision 0.9); sold/expired listings trigger an optional alert.
- **Gifts:** one-way send with a confirm step.
- **Swap [NEW]:** give up one of your creatures to the bot and receive a random one back. Costs a swap token (free tokens daily, more from events). Rules: cannot swap locked/favorite/buddy creatures; the result pool is weighted by `swap.json`; special-tier creatures can only appear with very low odds. Audit row for every swap; daily cap to stop mass farming.
- **Lottery [NEW]:** periodic draws (e.g. weekly). Buy tickets with coins (cap per user); prize pool plus a special creature for the winner; draw uses secure randomness and is logged to `catch_audit`; announced in the hub's News screen.
- **Voting rewards [NEW]:** vote every 12 hours for a lootbox + coins; streaks grant eggs. The repo already references top.gg in several places (`economy.py`, `leveling.py`, `topgg_stats.py`, `api_server.py`). Check what exists before reusing it, and make the webhook **idempotent** so a re-delivered vote can't pay twice.
- **Leaderboards:** most catches, dex completion, rarest collection, per server and global. **[NEW]** Also crews and the battle tower (when those exist).

**Done when:** a failed or interrupted trade can never duplicate or delete a creature; **[NEW]** a swap or lottery failure can't lose or duplicate a creature or coins.

---

## Phase 6 — Idle and activity systems **[NEW]**

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** give players things to do when they can't be online, and a second and third way to play.

### 6.1 Catchbot (idle catching)
- Pay coins to start a run for up to N hours. The bot "catches" automatically while the player is away.
- **Upgrades (coins, escalating costs):** more catches per hour, lower cost per catch, longer maximum duration. Stored in `catch_catchbot` (levels, `run_started_at`, `run_ends_at`, `run_cost`).
- **Results:** rolled in one batch when the run ends or the player collects (not per catch), using the normal species table but with a **reduced** rate for the rarest tiers compared to manual play. Added in one transaction through `record_catch`, with a size cap so the box can't flood.
- Decision (Phase 0 counter matrix): catchbot catches count for the dex and achievements but not for streaks or most quests.
- Finish alert via the reminder engine.

### 6.2 Fishing
- Needs a **rod** (tiers in the shop). Own cooldown, longer than encounters.
- Flow: **Cast** button → message edits to "waiting..." → after a random delay a **Pull!** button appears for a short window. Miss it and nothing bites. Better rods have fewer "no nibble" casts and better shiny odds.
- Pool: water-habitat species only. Fishing is the only source of **fishing tokens**, spent at a fishing shop (baits, lures, special items).
- Fishing catches feed eggs but not quests (counter matrix).
- Interaction note: use `defer` + edit-in-place and keep the "Pull!" window well inside the interaction token lifetime (15 minutes), and never rely on the 3-second rule for the delayed step.

### 6.3 Alerts screen
- Per-player toggles (DM or channel): cooldown ready (encounter, fishing), daily/hunt ready, catchbot finished, egg hatched, market sale, crew events, rare spawn in my server.
- Quiet hours, a "mute all" switch, and a per-day cap on pings.
- This replaces the need for external helper bots, which other catch games depend on.

### 6.4 Timed boost windows
- Scheduled "boost hours" (server-wide or global) that raise spawn, shiny or event rates for a short period; announced in spawn channels and the News screen. Owners can start a short local one from the setup wizard; global ones are admin-only.

**Done when:** a catchbot run finishes and pays out correctly even if the bot was offline when it ended; fishing can't be exploited by clicking early; alerts never double-send after a restart.

---

## Phase 7 — Groups and community **[NEW]**

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** social reasons to keep playing.

### 7.1 Crews (game clans)
- Name: **Crews** (see decision 0.13).
- **Create / join / leave** through modals and selects: create (coin cost), invite by user select or join code, roles (leader, officers, members), member cap that grows with tier, transfer or disband.
- **Crew catch counter:** every member's catch (through `record_catch`) adds to the crew total.
- **Tiers** (names TBD, thresholds in `clan_tiers.json`): reaching a catch threshold *and* paying a coin upgrade cost unlocks a tier. Perks are cumulative: small bonuses to catch rate, shiny rate, event rate, an extra bonus item, a shop discount, and a higher member cap. Keep the bonuses small and capped.
- **Anti-abuse:** perks start after 24 h in a crew, leave cooldown, leader-only upgrades, audit log of every upgrade and kick.
- **Crew screen:** members and contributions, tier progress, perks, a crew leaderboard.
- Tables: `catch_crews`, `catch_crew_members`, `catch_crew_log`.

### 7.2 Contests
- Opt-in **monthly** (and optionally weekly) contests for the most catches. Join button; live countdown and leaderboard; top-N rewards (lootboxes, special items, a special-tier creature for first place); snapshot at the end; archive of past winners.
- Anti-cheat: minimum account age, excludes flagged accounts, catchbot catches excluded (decision).
- Tables: `catch_contests`, `catch_contest_entries`.

### 7.3 Giveaways and drops
- Hook into the existing giveaway wizard (`_views_giveaway_wizard.py`) so owners can pick a creature, lootbox, or item as the prize.
- **Drops:** an admin-triggered public message where the first N players to press the button win an item. Atomic claim, same pattern as spawn catching.

### 7.4 Events and promos
- Scheduled boosted spawns (themed weekends), event-only species, an **event shiny rate** stat (boosted by crew perks), event eggs, limited-time shops and quests.
- Owners can start a **server** event from the setup wizard; admins run **global** events.
- A News screen in the hub lists current/upcoming events, contests, the lottery draw and boost windows.

### 7.5 Bonuses screen
- One screen listing everything currently boosting the player (crew perks, items, premium, events, boost windows) with remaining time, so players can see what's active.

**Done when:** a crew's total always equals the sum of its members' counted catches; leaving and rejoining can't be used to farm perks; contest results can be reproduced from the logs.

---

## Phase 8 — Battles (optional but valuable)

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** a reason to keep and level specific creatures, and a long-term progression ladder.

### 8.1 Teams and moves
- Team of **3** creatures (set from a Team screen). Each has 3–4 moves, learned by level or from move items. Simple passive **abilities**. Element advantages from a type chart in a data file.

### 8.2 NPC trainers and the ladder **[NEW]**
- **NPC battles:** trainers by region with set difficulty and rewards, chosen from a list.
- **Gym-style series:** a set of leaders per region, a badge for each, first-clear rewards, repeatable at lower rewards. (Use original names — see 0.8.)
- **Top-tier ladder:** a unlockable high-level sequence (like an elite four) followed by a champion fight, gated by badges.
- **Challenges:** repeatable goals against fixed opponents with their own rewards.

### 8.3 Battle Tower **[NEW]**
- Register for the current wave, climb floor by floor. Progress is saved when your whole team faints; you can forfeit safely.
- Waves reset on a schedule (for example 3 times a week). Registration closes shortly before prizes are paid. Prizes by floor reached; a high-score board for the current wave.
- The scheduler handles reset, payout and announcements, and can run after a restart.

### 8.4 Transformation chamber **[NEW]**
- A rotating challenge (data-driven, changes every ~2 weeks) where players earn a form-change item.
- Unlock: swap or release N of a named species (fewer for rarer ones; premium may lower N slightly). Then win several battles in a row with the required creature on your team.
- Decision: is the form permanent, or only during battles? (Phase 0.)

### 8.5 PvE wild fights and PvP duels
- **PvE wild fights:** turn-based fights driven entirely by buttons (Attack / Special / Swap / Run).
- **PvP duels:** challenge via user select; both players act through persistent buttons; timeout forfeits.

### 8.6 Cosmetics and bosses
- **Battle icons:** unlockable trainer icons shown on profile and in battles (tied to achievements).
- **Raid-style bosses:** a rare spawn the whole channel damages together; contribution-based rewards.

**Done when:** a battle survives a bot restart (state in DB) and can't be stalled by one player going AFK; the tower reset and prize payout can't run twice.

---

## Phase 9 — Security, reports, bans and review **[v3]**

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

**Goal:** the bot owner (and delegated admins) can see problems, act on them, undo mistakes, and protect the economy. Nothing here deletes a player's data without a human decision.

> Before building, read how the repo already handles this: the `bot_blacklist` table, `server_listing_reports` and `report_notify_config`, and the Safety area of the admin panel (`_views_admin_panel_safety.py`, which has watchlist and reports views). Reuse those patterns and extend them; do not build a parallel system.

### 9.1 Who can do what
| Role | Can do |
|------|--------|
| **Bot owner** | Everything: global ban, lift any sanction, grant/remove anything, flip kill switches, add/remove game admins, see all audit logs |
| **Game admin** (delegated by the owner) | Review reports, warn, restrict, temp-ban (up to a set limit), uphold/reduce appeals. Cannot grant creatures or currency, change odds, or issue permanent bans without the owner |
| **Server owner / Manage Server** | Server scope only: ban a player from the game *in their server*, turn features off for their server, see reports from their server |
| **Player** | Report, appeal their own sanction, see their own sanction status |

Every action by anyone above is checked server-side on every click (never by hiding a button) and written to `catch_audit`.

### 9.2 Reports
- A **Report** button on: market listings, creature cards (for offensive nicknames), trade offers, crew pages, and the profile. A general "Report a problem / exploit" entry in the hub.
- Flow: modal with a category (scam, offensive name, cheating/alt farming, harassment, exploit, other) and details. Max N reports per player per day.
- `catch_reports`: id, reporter, target type and id, category, text, **evidence snapshot** (the listing, nickname or trade contents as they were, so the target can't edit it away), status (open / in review / actioned / dismissed), assigned_to, created_at, resolved_at, resolution, resolver.
- Reporter gets an optional DM with the outcome. Repeated false reports lead to a restriction on reporting.
- **Review queue** in the `/admin` panel: filter by status/category/server, open a report to see the snapshot plus the target's history (prior reports, sanctions, recent audit rows, active flags). Actions: dismiss, warn, remove content (reset nickname, delist the item), restrict, temp-ban, permanent ban (owner only), escalate to owner.

### 9.3 Sanctions: warnings, restrictions, temporary bans, bans
| Kind | Effect |
|------|--------|
| **Warning** | Recorded and shown to the player; no restriction |
| **Restriction** | Blocks specific features only: trade/gift, market, swap, lottery, battles, reporting, or crews |
| **Temporary ban** | Blocks the whole game until a set time (1 h, 24 h, 7 d, 30 d, or custom). Expires automatically through the scheduler |
| **Permanent ban** | Blocks the whole game until a human lifts it (owner only) |
| **Server ban** | Server owner's tool: blocks the player in that server only |

- `catch_sanctions`: id, user_id, scope (global/guild), guild_id, kind, features, reason_code, note, issued_by, issued_at, expires_at, lifted_at, lifted_by, evidence_ref.
- **Enforcement point:** `check_player_allowed(...)` (Phase 1.5) reads active sanctions. Cache them in memory for ~30–60 s and **invalidate immediately** when a sanction is added or lifted. Also re-check inside trade, market, swap and lottery transactions, so a slow cache can't let a banned player through.
- The player sees a clear message: what they are blocked from, until when, the reason category, and an **Appeal** button.
- On a ban: open trades are cancelled, market listings are frozen, catchbot runs are paused, crew roles are suspended. **Creatures and coins are never deleted**, so a lifted ban restores everything.
- Escalation ladder is a suggestion, not automatic: warning → restriction → temp ban → permanent ban. Permanent bans always need the owner.
- Ban evasion: admins can mark accounts as linked by hand. No IP or device tracking (Discord doesn't provide it, and we should not collect more than we need).

### 9.4 Appeals and review
- A sanctioned player presses **Appeal** → modal → `catch_appeals` (sanction id, text, status, reviewer, decision, decided_at). One open appeal per sanction, with a cooldown after a rejection.
- Reviewers see the original report, evidence snapshot and the player's history. Decisions: uphold, shorten, lift. Reviewer cannot be the same person who issued the sanction (four-eyes), and permanent bans need the owner.
- The player is told the decision by DM; the decision and reason are audited.
- **Review of the reviewers:** a weekly digest to the owner listing every sanction, grant and lift by each admin, and any admin grant above a threshold needs owner confirmation.

### 9.5 Anti-cheat flags (automatic, never auto-permaban)
Detectors write rows to `catch_flags` (user, kind, score, details, status). Examples:
- Catch or encounter rate faster than the cooldown allows; inhuman fishing reaction times; very long unbroken streaks with no pauses.
- Alt funnelling: repeated one-sided trades or gifts to a new account; market sales to the same buyer at inflated prices; swap or lottery loops.
- Currency or item spikes that don't match logged sources; duplicate vote webhooks; contest or crew anomalies (many fresh accounts joining at once).
- A high score can **hold** the account's trades/market pending review, but only a human can issue a ban. Flags appear in the same review queue as reports.

### 9.6 Content safety
- Nicknames, crew names, descriptions and listing notes go through the repo's existing blocked-words list, length limits, and a no-links/no-invites rule; mentions are always escaped.
- Offensive content can be reset by an admin (nickname reverts to the species name) without sanctioning the player.

### 9.7 Technical security and kill switches
- **Never trust a `custom_id`.** IDs inside buttons can be tampered with, so the server re-checks that the creature, listing, trade or crew belongs to (or is visible to) the user who clicked.
- Permissions are checked server-side on every action; parameterized SQL only; per-user, per-action rate limits; idempotency keys on purchases, claims, drops, votes and payouts.
- Vote webhook: verify the shared secret and make processing idempotent.
- **Kill switches** in the `/admin` panel (global) and the server setup (per server): whole game, spawns, encounters, trading, market, swap, lottery, catchbot, fishing, battles, contests. Takes effect immediately through the Phase 1.5 gate. A **freeze economy** mode stops all coin and item movement while an incident is investigated.
- **Integrity checks** run on a schedule and alert the owner on a mismatch: no creature owned by two players, no negative balances, coin audit totals match balances, no listing for a creature the seller no longer owns.
- **Recovery tools:** restore a creature or balance from the audit log (owner confirmation, audited).
- `catch_audit` is append-only in code (no UPDATE or DELETE paths) and is included in backups.
- Secrets live only in environment variables, never in logs, embeds or error messages.

### 9.8 Rules, privacy and notices
- A short **Game rules** screen shown on a player's first `/catch` (fair play, no alt farming, how bans and appeals work).
- Store only Discord IDs and game data. A player's data can be exported or deleted on request, except audit rows needed for fraud cases (document this).
- **Notifications to the owner:** DM or log-channel alerts for integrity failures, mass flags, permanent bans and appeals waiting longer than N days; a configurable sanctions log channel.

**Done when:** a reported listing can be reviewed with its snapshot, the seller temp-banned, the ban expires on its own, the player appeals and is lifted, and at no point is a creature or coin lost; a forged `custom_id` can never act on another player's creature; flipping a kill switch stops that feature within seconds.

---

## Phase 10 — Owner tools, premium, analytics

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

- **[v4] Asset Manager and theme editor:** the full panel specified in 1.6 (history/revert, icon sync, theme editor, usage warnings, delegated art admins) inside the `/admin` panel.
- **Server settings wizard:** spawn channels, speed, rarity boost cap, despawn time, allowed roles, log channel for rare catches. **[NEW]** Also: Encounter/Fishing/Catchbot panel channels, encounter cooldown, rare-spawn ping role, server contest toggle, server event starter.
- **Rare-catch announcements:** optional embed in an announce channel for epic+ catches.
- **Owner/admin console (`/admin` panel):** grant/remove creatures, ban a user from the game (full sanction, report and appeal tools are in Phase 9), add or disable species, run a global event, view economy sinks/sources. **[NEW]** Also: grant lootboxes/eggs/special-tier creatures, schedule global boost windows, manage contests, run a global drop, inspect the lottery and swap logs, view a player's crew and audit history.
- **Premium (server premium):** higher spawn multiplier cap, extra spawn channels, custom spawn pool/theme filters, cosmetic card frames. **[NEW]** Convenience only: slightly shorter encounter/fishing cooldowns, an extra egg slot, extra alert options. Never required to catch or own anything, and never raises odds for the rarest tiers.
- **Analytics:** catches per day, spawn-to-catch rate, average time to catch, economy flow, retention by day. Logged to a table and shown in the admin panel. **[NEW]** Also: catchbot and fishing usage, egg hatch rates, lootbox/lottery/swap sinks and sources, crew counts and sizes, contest participation, alert opt-in rates, **[v5]** database size per `catch_*` table with alerts at 70% and 85% of the limit.

---

## Phase 11 — Hardening and launch

> **BUILDER REMINDER [v6]:** every task for this phase in Appendix C must be built **completely** (no stubs, no TODOs, no half-wired buttons), with tests and a completion report, then reviewed by the other AI. Stopping early means the task failed.

- **Tests:** unit tests for `modules/catch_game.py` (odds, evolution, trade validation); integration test for the atomic catch claim using concurrent tasks; trade transaction tests. **[NEW]** Also: statistical tests that simulate 1M rolls of each lootbox, egg, swap and lottery table and compare them to the data files; catchbot batch roll bounds and idempotent payout; `record_catch` credits every counter once; crew perk math and cooldown rules; tower reset/payout run exactly once; vote webhook idempotency; reminder dedupe after a restart.
- **Load/behaviour tests:** simulate busy channels to check the message listener cost and spawn pacing. **[NEW]** Also: the catch pipeline under many simultaneous catches, and scheduler behaviour with thousands of due rows.
- **Failure handling:** every callback logs guild/user/feature context on exceptions and tells the player what happened (same approach as the join-DM toggle handler).
- **[v3] Security and moderation tests:** ban gate blocks every action type and every callback; temporary ban expires on schedule and the cache invalidates on add/lift; reports keep their evidence snapshot; a forged `custom_id` cannot touch another player's creature, listing or trade; rate limits trigger; kill switches take effect immediately; integrity checks catch a deliberately duplicated creature; audit table has no update/delete path.
- **[v3] Every PR follows the Build and deploy rules** at the top of this file (pull request, DB version check, tests, bug check, component limits, interaction time).
- **[v4] Asset and theme tests:** upload validation (format, size, dimensions, transparency, duplicates, animated files); placeholder fallback when art is missing; draft/publish/revert; card cache invalidates on a new version; silhouette generation; every view's buttons use the theme roles and destructive actions are `danger`; theme hex input rejects bad values; only the owner and art admins can upload.
- **[v5] Storage and retention tests:** a vault upload stores only ids and metadata; a fresh URL is fetched from the message; the loader falls back to repo art and then the placeholder when the vault is unreachable; the cache is filled after the defer, never before; rebuild-from-vault restores the table; purge and archive jobs never delete live rows (open spawns, pending trades, unresolved reports); archived audit files can be read back.
- **Migration/rollout:** ship behind the owner toggle; enable on your own test server first, then a few friendly servers, then everyone.
- **Docs and onboarding:** add a "Creature catching" entry to the README, `/help`, and the join DM; include it in your setup tutorial video. **[NEW]** Also translate the `catch.*` locale keys for all supported languages before public launch.
- **Balance pass:** review logs after one week and adjust spawn weights, odds, and prices in data files, not code.

---

## Suggested build order (shortest path to something playable)

1. Phase 0 decisions → 2. Phase 1 (schema, hub, owner setup, shared engines, **asset manager minimum + theme**) → 3. Phase 2 (spawn, **encounters**, catch) → 4. Phase 3 (collection + dex + checklist) → release privately.
5. Phase 4 (progression: items, **lootboxes, eggs, hunts, achievements**) → 6. Phase 5 (shop, trade, market, **swap**, voting) → **Phase 9 core first (player gate, kill switches, report queue, temp bans, appeals, integrity checks)** → public beta.
7. Phase 6 (**catchbot, fishing, alerts**) → 8. Phase 7 (**crews, contests, events**) as the audience grows.
9. Phase 8 (battles and tower) → 10. Phase 10 owner tools and analytics grow alongside every step.
11. Phase 11 (hardening) runs alongside every step, not just at the end. **Every PR follows the Build and deploy rules.**

**[v6] Who builds and who reviews:** every task in Appendix C is assigned to one builder (Vo AI or Claude) and reviewed by the other, following the AI build and review protocol at the top of this file. No task counts as finished until it is `[R]`.

**Cut line if scope gets too big:** lottery, transformation chamber, raids, battle icons, weekly contests and boost windows can all wait until after public beta without hurting the core game.

## Open questions to settle early
- Creature theme and roster size at launch.
- Per-server collections vs one global collection per player (recommendation: global).
- Whether shiny/rare/special creatures can ever be bought (recommendation: no; only earned).
- How much of the economy should be tied to the existing server coins. **[CHANGED]** With a global collection, market and crews, recommendation is a separate game-wide coin.
- Whether battles are in scope for the first public release.
- **[NEW]** Name for the player groups (not "clan") and for the extra rarity tier.
- **[NEW]** Does the catchbot feed streaks/quests/contests, or only the dex?
- **[NEW]** Is the transformation form permanent or battle-only?
- **[NEW]** Should the market and swap pool be global from day one, or start per-server?
- **[NEW]** Reuse the existing top.gg vote code, or rebuild it for the game.
- **[v3]** Who are the delegated game admins at launch, and what is the longest temporary ban they may issue alone?
- **[v3]** Should a global ban also hide the player's market listings and leaderboard entries, or only freeze them?
- **[v3]** How long are audit rows and report snapshots kept?
- **[v4]** Who creates the art (drawn, commissioned or generated), and what licence record do you keep for each file?
- **[v4]** Final image size for creature art and card layout, so every asset is made to the same spec.
- **[v4]** Use application emojis for item and ball icons, or plain unicode only?
- **[v4]** Which colours become the official rarity palette?
- **[v5]** What is the database's real hard limit, and at which size should the owner be alerted?
- **[v5]** How big is a player's box (capacity), and what do duplicates convert into?
- **[v5]** Retention periods for spawns, audit rows, reports and appeals.
- **[v5]** Which private server and channel(s) hold the asset vault, and where do you keep your own backup copy?
- **[v6]** Which tasks go to Vo AI and which to Claude, and who reviews each? (Fill in the `Builder` and `Reviewer` columns of `docs/catch/TASKS.md`.)
- **[v6]** Where does the owner run the smoke tests (which test server), and who reports the results back to the reviewer?

---

## Appendix A — Feature review checklist

Reviewed against PokeMeow's public wiki, command list and community guides. Feature names are descriptive, not copied.

| Reviewed feature | In plan | Phase |
|---|---|---|
| Chat-spawned wild creatures | Yes (v1) | 2.1 |
| Player-triggered encounter with cooldown | Added | 2.0 |
| Balls, shop, daily, quests | Yes (v1) | 2, 4, 5 |
| Catch-rate berry, repel, radar, coin booster, quest scroll | Added | 4 |
| Lootboxes with a drop table | Added | 4 |
| Eggs hatched by catching | Added | 4 |
| Daily hunt target | Added | 4 |
| Achievements and badges | Added | 4 |
| Dex, box, profile, buddy, evolve | Yes (v1) | 3, 4 |
| Checklist and species info lookup | Added | 3.5 |
| Trading and global market | Yes (v1); scope decided | 5 |
| Swap (random trade with the bot) | Added | 5 |
| Lottery | Added | 5 |
| Voting rewards and streaks | Added | 5 |
| Catchbot with upgrades | Added | 6.1 |
| Fishing with rods, tokens and timing | Added | 6.2 |
| Reminders and rare-spawn alerts | Added | 1.5, 6.3 |
| Timed boost hours | Added | 6.4 |
| Clans with ranks and perks | Added (as Crews) | 7.1 |
| Monthly contests | Added | 7.2 |
| Giveaways and drops | Added | 7.3 |
| Events, promos, event shiny rate | Expanded | 7.4 |
| Bonuses/unlocks overview | Added | 7.5 |
| Team of 3, moves, abilities | Added | 8.1 |
| NPC trainers, gym-style ladder, top tier, champion, challenges | Added | 8.2 |
| Battle Tower with scheduled resets | Added | 8.3 |
| Rotating transformation challenge | Added | 8.4 |
| PvE, PvP, raid bosses | Yes (v1) | 8.5, 8.6 |
| Battle icons | Added | 8.6 |
| Special rarity above shiny, exclusives by source | Added | 0.10, 0.11 |
| Multi-language support | Added | 0.7 |
| Premium/patron benefits | Yes (v1); limited to convenience | 10 |

---

## Appendix B — Art and icon checklist **[v4]**

Counts are **estimates for planning**; adjust to the final roster and item list. "Auto" means the Asset Manager generates it from other art.

| Asset | Approx. count at launch | Spec | Notes |
|---|---|---|---|
| Creature art (normal) | 40–60 (one per species) | Square, transparent, same size for all | Evolution stages count as separate species |
| Creature art (shiny) | Optional, same as above | Same | If missing, use a recolour/overlay fallback |
| Silhouette + thumbnail | Auto | Generated | Overridable |
| Ball icons | 4 (Basic / Great / Ultra / Master) | Small square | Also synced as app emojis |
| Item icons (berry, repel, radar, candy, stone, booster, scroll, incense, lure) | ~10–15 | Small square | App emojis |
| Egg art | 3 tiers | Square | |
| Lootbox art | 1–3 | Square | |
| Rod tiers, catchbot | ~4 | Square | |
| Rarity frames (5 tiers + shiny + special) | ~7 | Overlay matching the card size | |
| Element icons and card backgrounds | One per element | Icon + background | |
| Achievement badges | One per achievement | Small square | Number depends on the final list |
| Trainer / battle icons | ~6–10 to start | Square | Phase 8 |
| Event banners | 1 per event | Wide | Made per event |
| Hub header / spawn thumbnails | 1–3 | Wide / square | |
| UI emoji set (✔ ✖ ⭐ arrows, status dots) | ~10–20 | Small square | Optional; unicode works too |
| Crew emblems | Optional | Square | Only if crews get emblems |

**Before you start making art:** fix the size and style in the art style sheet (Phase 0), name files consistently (`species_<id>.png`), keep the source file and licence note for each one, and import the first batch with `scripts/import_catch_assets.py`.


---

## Appendix C — Task ledger **[v6]**

Copy this appendix to `docs/catch/TASKS.md` and add the columns **Builder**, **Reviewer**, **Status** (`[ ]` `[~]` `[x]` `[R]`) and **PR**. A task is finished only when **every** "Done when" item is true **and** it has a completion report **and** the other AI approved it. Do tasks in order inside a phase unless the ledger says otherwise. Every task also needs the checks from Build and deploy rules (B.2–B.7).

### Phase 0 — Decisions (owner, written in `docs/catch/`)
| ID | Task | Done when |
|---|---|---|
| P0-01 | Rarity table and launch roster | File lists all tiers (incl. special tier) and every launch species with element, habitat, `obtain_via` |
| P0-02 | Stat model and catch-rate formula | Formula and inputs written with worked examples |
| P0-03 | Currency map and counter matrix | Each currency, source and sink listed; each catch source mapped to the counters it feeds |
| P0-04 | Drop tables | Lootbox, egg, swap and lottery tables in the data-file format with percentages that sum correctly |
| P0-05 | Name sheet | Final names for crews, special tier, ladder tiers, forms, items |
| P0-06 | Theme and art style sheet | `theme.json` palette approved; creature art size and style fixed |
| P0-07 | Open questions answered | Every item under "Open questions" has an answer or an explicit "decide later" |

### Phase 1 — Foundation
| ID | Task | Done when |
|---|---|---|
| P1-01 | CI workflow | `.github/workflows/ci.yml` runs pytest and a compile check on every PR and fails when either fails |
| P1-02 | Phase 1 schema | All Phase 1 tables created idempotently with unique indexes and `clone_id` keys; `SCHEMA_VERSION` bumped; schema test passes |
| P1-03 | Theme module | `catch_theme.py` and `theme.json` provide button styles and rarity/element/state colours; tests cover every role |
| P1-04 | Core services | `record_catch`, `check_player_allowed` (feature flags only) and the cooldown helper exist with unit tests |
| P1-05 | Species loader | `species.json` loads into `catch_species` on boot, safely re-runnable |
| P1-06 | `/catch` hub | One command opens the hub with the category select, Home/Back on every screen; component-limit test passes |
| P1-07 | Persistent item registry | All catch DynamicItems registered in `setup_hook`; buttons still work after a restart |
| P1-08 | Owner setup | Join-DM toggle, setup screen (channels, speed preset, Create #wild-zone, test spawn) and Server Owners Panel entry all work, from a server and from a DM |
| P1-09 | Scheduler | One background task handles due rows and resumes from the DB after a restart; retention purge job exists and spares live rows |
| P1-10 | Reminder engine | `catch_reminders` table and dispatcher work with dedupe and quiet hours (no UI yet) |
| P1-11 | Localization | Every Phase 1 string uses `catch.*` keys in `locales/*.json`; missing translations fall back to English |
| P1-12 | Asset Manager minimum | Vault channel setup, loader with placeholder fallback, upload with draft → publish, Overview with missing-art list, import script |
| P1-13 | Kill switches (minimal) | `catch_feature_flags` and an admin toggle switch parts of the game off immediately through the gate |
| P1-14 | Placeholder card renderer | Pillow renderer runs in `asyncio.to_thread` with a cache keyed by asset id and version |
| P1-15 | Docs | README entry and `docs/catch/ARCHITECTURE.md` describe the modules and data files |

### Phase 2 — Spawn and catch
| ID | Task | Done when |
|---|---|---|
| P2-01 | Spawn trigger | Cheap per-channel counter with human-only, min-length, per-user rate limit, jitter, minimum gap and timer fallback |
| P2-02 | Spawn creation | Weighted species roll, shiny chance and level; spawn message with silhouette and persistent buttons; row saved |
| P2-03 | Despawn loop | Expired spawns edit to "ran away" and disable buttons; resumes correctly after a restart |
| P2-04 | Ball picker, berry and catch formula | Formula in `catch_game.py` with unit tests; ephemeral picker shows owned balls and berries |
| P2-05 | Atomic claim | Concurrent test proves only one claimant can win |
| P2-06 | Result messages | Reveal, fail and flee messages use the theme colours; Throw ball disables when resolved |
| P2-07 | Catch pipeline | `record_catch` credits coins, XP, dex, streak and first-catch bonus once, even on a retry |
| P2-08 | Player encounters | Encounter button and optional panel create an owner-only spawn with a cooldown |
| P2-09 | Rare-spawn ping | Opt-in role ping on rare spawns works and respects limits |
| P2-10 | Auto-click guard | "Are you there?" check triggers after long unbroken streaks |
| P2-11 | Restart test | A restart mid-spawn keeps buttons working and the expiry correct |

### Phase 3 — Collection
| ID | Task | Done when |
|---|---|---|
| P3-01 | Box view | Pagination with Prev/Next, page jump modal and page size |
| P3-02 | Filters | Rarity, element, shiny, favorite, level range, habitat, obtain source, special tier, not-in-trade |
| P3-03 | Sort and search | Sort select and a name/nickname search modal |
| P3-04 | Creature detail | Rendered card with art, level, stat bars, shiny badge and catch info |
| P3-05 | Creature actions | Favorite, nickname modal (content-filtered), lock, two-step release, set buddy |
| P3-06 | Dex | Silhouettes for unseen, colour for owned, completion per rarity and element, exclusive hints |
| P3-07 | Profile card | Catches, rarest, dex %, buddy, streak, badges, crew |
| P3-08 | Checklist and species info | Missing-species checklist and a species search page |
| P3-09 | Box capacity and release duplicates | Capacity enforced; duplicates convert into coins or candy |
| P3-10 | Scale test | 500+ creatures filter, sort and page with no interaction timing out |

### Phase 4 — Progression
| ID | Task | Done when |
|---|---|---|
| P4-01 | XP, levels and buddy | Buddy gains XP from catching, fishing, quests and hatching; level-up works |
| P4-02 | Evolution | Preview and confirm; keeps nickname and favorite; level or item trigger |
| P4-03 | Stats | Base stats plus hidden variation shown as bars on the card |
| P4-04 | Items and inventory | Balls, berry, repel, radar, boosters, candy, stones, incense, lure, scroll all usable with working effects |
| P4-05 | Lootboxes | Open screen, data-driven table, odds shown, simulation test of 1M rolls matches the file |
| P4-06 | Eggs | Hold, progress by catches, hatch screen, event pools |
| P4-07 | Hunts | Daily target rolls at rollover, boosts spawn for that player, pays by rarity |
| P4-08 | Daily | Daily reward with streak and free swap tokens |
| P4-09 | Quests | Daily and weekly quests, claim screen, quest reset scroll |
| P4-10 | Achievements and badges | Data-driven goals, progress screen, claimable rewards, badges on the profile |
| P4-11 | Balance sheet | First-week progression modelled in a spreadsheet and stored in `docs/catch/` |

### Phase 5 — Economy and social
| ID | Task | Done when |
|---|---|---|
| P5-01 | Shop | Rotating stock, confirm buttons, purchases idempotent |
| P5-02 | Trading | Two-sided locked offer, both confirm, one transaction, audit row, expiry; interruption can never duplicate or delete |
| P5-03 | Marketplace | List with price modal, browse with filters, buy with confirm, fees and price floors, global with server filter |
| P5-04 | Gifts | One-way send with a confirm step |
| P5-05 | Swap | Token cost, rules, weighted pool, daily cap, audit row |
| P5-06 | Lottery | Ticket purchase with cap, secure draw, prize pool plus special creature, logged and announced |
| P5-07 | Voting rewards | 12-hour vote reward, streak eggs, webhook verified and idempotent |
| P5-08 | Leaderboards | Catches, dex, rarest; per server and global |

### Phase 6 — Idle and activity systems
| ID | Task | Done when |
|---|---|---|
| P6-01 | Catchbot runs and upgrades | Start a run, three upgrade tracks with escalating cost, state in the DB |
| P6-02 | Catchbot payout | Results rolled in one batch and written in one transaction through `record_catch`; correct even if the bot was offline at the end |
| P6-03 | Fishing | Rod tiers, cast → delayed Pull! window, water pool, fishing tokens and shop, cooldown |
| P6-04 | Alerts screen | Per-player toggles, DM or channel, quiet hours, daily cap; no double sends after a restart |
| P6-05 | Boost windows | Scheduled and owner-started boosts, announced, expire automatically |

### Phase 7 — Groups and community
| ID | Task | Done when |
|---|---|---|
| P7-01 | Crews basics | Create, invite/join, leave, roles, transfer, disband |
| P7-02 | Crew counter, tiers and perks | Crew total equals the sum of members' counted catches; tier upgrades and capped perks |
| P7-03 | Crew anti-abuse | Perks start after 24 h, leave cooldown, audit log |
| P7-04 | Contests | Join, live board, snapshot at the end, rewards, archive |
| P7-05 | Giveaways and drops | Giveaway wizard offers creature/item prizes; drops use an atomic claim |
| P7-06 | Events and promos | Event pools, event shiny rate, event eggs, limited shop, owner-started server events |
| P7-07 | Bonuses screen | Lists every active boost with remaining time |
| P7-08 | News screen | Events, contests, lottery draw and boost windows |

### Phase 8 — Battles
| ID | Task | Done when |
|---|---|---|
| P8-01 | Teams, moves, abilities | Team of 3, moves, simple abilities, type chart in a data file |
| P8-02 | NPC battles | Trainer list with rewards |
| P8-03 | Gym-style ladder | Leaders, badges, first-clear and repeat rewards |
| P8-04 | Top-tier ladder and champion | Gated by badges |
| P8-05 | Challenges | Repeatable fixed-opponent goals |
| P8-06 | Battle Tower | Register, floors, progress saved, scheduled reset and prizes that can't run twice |
| P8-07 | Transformation chamber | Rotating challenge, unlock rules, reward item |
| P8-08 | Wild fights | Button-driven turn-based fights |
| P8-09 | PvP duels | Challenge via user select, timeouts forfeit |
| P8-10 | Raid bosses | Shared damage with contribution-based rewards |
| P8-11 | Battle icons | Unlockable icons tied to achievements |
| P8-12 | Restart safety | A battle survives a restart and can't be stalled by an AFK player |

### Phase 9 — Security, reports, bans and review
| ID | Task | Done when |
|---|---|---|
| P9-01 | Roles and permissions | Owner, game admin, server owner and player rules enforced server-side and audited |
| P9-02 | Report flow | Report buttons and modal with evidence snapshots and daily limits |
| P9-03 | Review queue | Admin queue with filters and every action (dismiss, warn, restrict, temp-ban, ban, escalate) |
| P9-04 | Sanctions | Warning, restriction, temporary ban with auto-expiry, permanent ban (owner only), server ban |
| P9-05 | Enforcement gate | `check_player_allowed` blocks every action, cache invalidates on add or lift, transactions re-check |
| P9-06 | Appeals | Appeal button and modal, four-eyes review, DM with the decision |
| P9-07 | Anti-cheat flags | Detectors write flags to the queue and may hold trades; never auto-ban |
| P9-08 | Content filter | Nicknames, crew names and notes filtered; mentions escaped |
| P9-09 | Kill switches and freeze economy | Full list of switches, global and per server, effective within seconds |
| P9-10 | Integrity checks | Scheduled checks catch a deliberately duplicated creature and negative balances and alert the owner |
| P9-11 | Recovery tools | Restore a creature or balance from the audit log with owner confirmation |
| P9-12 | Rules screen | Shown on first `/catch` |
| P9-13 | Owner notifications | Alerts and sanctions log channel configurable |
| P9-14 | Security tests | Forged `custom_id` can't act on another player's data; rate limits trigger; audit has no update/delete path |

### Phase 10 — Owner tools, premium, analytics
| ID | Task | Done when |
|---|---|---|
| P10-01 | Server settings wizard | Spawn channels, speed, despawn, roles, log channel, encounter and fishing panels, rare ping role, server events |
| P10-02 | Rare-catch announcements | Optional embed for epic-and-above catches |
| P10-03 | Admin console extras | Grants, species and event management, global drops, boost windows, contest management |
| P10-04 | Premium perks | Convenience-only perks that never raise odds for the rarest tiers |
| P10-05 | Analytics | Daily aggregates and admin dashboard |
| P10-06 | Asset Manager full | History/revert, vault backup and rebuild, icon sync, theme editor, usage warnings, art-admin delegation |
| P10-07 | Database budget panel | Table sizes with alerts at 70% and 85% |

### Phase 11 — Hardening and launch
| ID | Task | Done when |
|---|---|---|
| P11-01 | Full test coverage | Every module and rule named in Phase 11 has tests and the whole suite is green in CI |
| P11-02 | Load and behaviour tests | Busy-channel, many simultaneous catches and scheduler stress tests pass |
| P11-03 | Failure handling audit | Every callback logs context on exceptions and tells the player what happened |
| P11-04 | Rollout | Test server, friendly servers, then everyone, each with a log review |
| P11-05 | Docs and locales | README, `/help`, join DM and all `catch.*` keys translated |
| P11-06 | Balance pass | One week of logs reviewed; odds, weights and prices adjusted in data files |

---

## Final completion checklist **[v6]**
The game is **not finished** until every box is true. A builder or reviewer who stops before this list is complete has not finished the job.

- [ ] Every task in Appendix C is `[R]` (built, completion report, approved by the other AI, merged by the owner)
- [ ] Full `pytest` suite green locally and in CI; stub scan clean on all of `main`
- [ ] `SCHEMA_VERSION` consistent with every migration; migrations idempotent; clone test passed
- [ ] Every view passes the component-limit test with worst-case data; every callback passes the interaction-time test
- [ ] Colours come only from the theme helper; destructive actions are red
- [ ] Ban gate, reports, temporary bans, appeals, kill switches and integrity checks are live and tested
- [ ] Art for 100% of the launch roster and items is live; vault backup exists outside Discord
- [ ] Database size alerts and retention jobs are running
- [ ] README, `/help`, join DM and locales are updated
- [ ] Owner smoke-tested every phase on the test server and the results were reported to the reviewer
- [ ] Rolled out: test server → friendly servers → everyone
