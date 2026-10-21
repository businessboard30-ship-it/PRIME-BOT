# Welcome Card Studio: handoff for the next Claude

Repo: `github.com/businessboard30-ship-it/PRIME-BOT` (Discord bot, Python, discord.py 2.7.1, Postgres via asyncpg).
Owner style: short plain updates, decisions given in chat, writes informally. **Update this file in every PR/push** (owner rule), and every push must say what is built and what is next.
Source of truth for the design: `docs/WELCOME_CARD_STUDIO_PLAN.md` (copied from the owner's upload on 2026-10-21).
Owner rules for this work: open a pull request, **never merge it**; no deploy without the owner's OK.

## Status (2026-10-21)

| Phase | State | Where |
|---|---|---|
| 0. Audit + premium gate helper (`studio_unlocked`) | **Not started** (owner said start Phase 1; the gate is only needed once Phase 2 adds studio-only features) | |
| 1. Wizard restructure | **Built, PR open, not merged** | branch `feat/welcome-studio-phase1` |
| 2. Gallery + rotation | Not started | |
| 3 to 7 | Not started | |

## What Phase 1 built

All in `discord_bot/cogs/_views_card_customize.py` (the `/welcome` wizard's "Customize Card" editor). **No new card features:** `modules/welcome_card.py`, the stored options (`ultra_card_json`, `parse_ultra_options`), the database and the join-time render are untouched, so an untouched server renders exactly as before.

- **4 numbered steps** replace the 3 tabs: 1 Pictures, 2 Look, 3 Text, 4 Send. Internal step keys stay `bg` / `layout` / `text` (+ new `send`), so custom ids on wizard messages already posted keep working (`CardTabButton` pattern now also accepts `send`). One plain-words hint line per step.
- **Easy mode by default, max 3 controls per step**; a **More controls / Fewer controls** button (`CardMoreButton`, state in `_MORE_STATE`, per step, 30 min TTL) adds that step's extras only:
  - Pictures: Upload/Clear, Card shape, Banner darkness. More: Crop focus.
  - Look: Card layout, Banner position, Avatar side. More: Avatar frame shape.
  - Text: Text color, Font, Heading/Member # buttons. More: Brackets, Style extras.
  - Send: Send me a test, Reset all, Done / Unlock.
- **Undo** (`CardUndoButton`): last 10 look changes per person per server (`_UNDO`, memory only). Covers option selects, presets, avatar shape, heading, member #, step reset and reset all. Does **not** cover the picture (upload/clear). Works on the in-memory draft for unpaid servers and the DB row for paid ones; the paid/unpaid gate is re-checked on the server.
- **Reset this step** (`CardStepResetButton`, fields in `_STEP_RESET_FIELDS`): resets only that step's options; never the picture or the other steps. The old whole-layout reset (`CardResetButton`) now lives on step 4 as "Reset all".
- **Send me a test** (`CardTestSendButton`): draws the real card at full size (`_render_tv(..., full=True)`, no TV downscale) and shows it **only to the admin** as an ephemeral message; watermarked while the server is unpaid; 5 s cooldown per person.
- `_apply_style` now takes `shape=None` (leave shape alone) and pushes an undo snapshot; `_tv_finish` got a `full` flag.
- Component budget checked by test: every step in More mode is under 40 components, at most 5 selects, at most 5 buttons per row.

### Deviations from the plan, on purpose (small)

1. **"Nothing is saved until Apply" is NOT in Phase 1.** Today paid servers save every tap straight to the live card, and the plan's Phase 1 says behavior stays identical. Undo is the safety net for now. A draft/Apply flow is a bigger change; do it with Phase 2 or on its own if the owner wants it.
2. **Test send is private (ephemeral), not a real DM/channel post.** Posting into the live welcome channel from a test button felt risky; real DM delivery belongs to Phase 6.
3. Presets stay on every step (unchanged), not counted in the "3 controls".
4. The old Neon preset name stays as is. Owner dislikes neon in designs: **ask before adding any glow/neon effect in Phase 4** (effects list in the plan has "avatar glow" and "sparkle", keep them soft/muted, off by default).

## Tests

- New: `tests/unit/test_welcome_studio_phase1.py` (steps, old ids, Easy/More limits, component limits, undo for paid/unpaid/no-access/cap/per-person, step reset, reset all, More toggle, test send incl. cooldown/failure/full size).
- Updated: `tests/unit/test_card_wizard_tv.py::test_each_tab_shows_only_its_own_controls` (Easy-mode control sets). Everything else in the card tests passes unchanged.
- Full unit suite on this branch: see the PR description for the exact numbers. Failures that also fail on `main` (checked by stashing): `test_ultra_trial_off::test_new_ultra_trials_are_off` (expects `ULTRA_TRIAL_ENABLED` False but it is True in this environment), `test_premium_price_copy` (already known).
- **Not tested against live Discord.** Needs a staging check: the 4 step buttons on a phone, More/Fewer, Undo, Reset this step, Send me a test, and that wizards posted before this PR still respond.

## What is next

1. Owner reviews and merges the Phase 1 PR (not merged by me).
2. **Phase 0 (gate helper)** then **Phase 2 (gallery + rotation)**: `studio_unlocked(guild, clone)` using `db.is_guild_premium_active(guild_id, clone_id)`, fail closed to the Ultra look with picture 1; tables `discord_welcome_backgrounds`, `discord_welcome_designs`, new `discord_welcome_config` columns (plan section 4); 10 pictures, multi-upload, thumbnails strip, 4 rotation modes, default Random-never-twice, migration of the single image (copy, never delete old columns). The Pictures step is where the gallery goes; keep it to 3 easy controls.
3. Still-open plan questions for the owner: Premium lifetime includes studio? Keep Phase 6 DM extras? Per-picture overrides in v1 or later?

## Gotchas

- Wizard ids are `cardwz_<field>:<guild>:<clone|->:<invoker|->`; new fields added here: `more`, `undo`, `stepreset`, `testsend`, tab `send`. Register any new class in `DYNAMIC_ITEMS` (bot.py imports it).
- A fresh file name per TV render (`_new_tv_name`) avoids the picture blinking out; keep it.
- The test fakes for `_render_tv` in older tests have no `full` argument; `_rerender` never passes it. Only the test-send path does.
