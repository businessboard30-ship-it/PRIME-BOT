# Welcome Card Studio: handoff for the next Claude

Repo: `github.com/businessboard30-ship-it/PRIME-BOT` (Discord bot, Python, discord.py 2.7.1, Postgres via asyncpg).
Owner style: short plain updates, decisions given in chat, writes informally. **Update this file in every PR/push** (owner rule), and every push must say what is built and what is next.
Source of truth for the design: `docs/WELCOME_CARD_STUDIO_PLAN.md` (copied from the owner's upload on 2026-10-21).
Owner rules for this work: open a pull request, **never merge it**; no deploy without the owner's OK.

## Status (2026-10-21)

| Phase | State | Where |
|---|---|---|
| 0. Premium gate helper (`studio_unlocked`) | **Built, PR open, not merged** (stacked on the Phase 1 branch; audit part of Phase 0 still not done) | branch `feat/welcome-studio-phase0`, PR #424 |
| 1. Wizard restructure | **Built, PR open, not merged** | branch `feat/welcome-studio-phase1` |
| 2a. Gallery data layer + rotation picker | **Built, PR open, not merged** (small slice, no UI yet) | branch `feat/welcome-studio-phase2a` (stacked on phase 0) |
| 2b. Join render uses the gallery | **Built, PR open, not merged** (join only; previews unchanged) | branch `feat/welcome-studio-phase2b` (stacked on 2a) |
| 2c. Pictures step UI (thumbnails, add/remove, rotation select) | Not started | |
| 2d. Multi-upload + downscale + migrate old single picture | Not started | |
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
2. **Phase 2 (gallery + rotation)**. The gate helper now exists: `modules/welcome_studio.py` `studio_unlocked(guild, clone)` (wraps `db.is_guild_premium_active`, fails closed; `effective_studio_options(options, unlocked, studio_keys)` strips studio-only keys, add each new key to `studio_keys`; 6 tests in `tests/unit/test_welcome_studio_gate.py`). Phase 2 must call it on Save/Apply AND at join render, falling back to the Ultra look with picture 1. Build: tables `discord_welcome_backgrounds`, `discord_welcome_designs`, new `discord_welcome_config` columns (plan section 4); 10 pictures, multi-upload, thumbnails strip, 4 rotation modes, default Random-never-twice, migration of the single image (copy, never delete old columns). The Pictures step is where the gallery goes; keep it to 3 easy controls.
3. Still-open plan questions for the owner: Premium lifetime includes studio? Keep Phase 6 DM extras? Per-picture overrides in v1 or later?

## Gotchas

- Wizard ids are `cardwz_<field>:<guild>:<clone|->:<invoker|->`; new fields added here: `more`, `undo`, `stepreset`, `testsend`, tab `send`. Register any new class in `DYNAMIC_ITEMS` (bot.py imports it).
- A fresh file name per TV render (`_new_tv_name`) avoids the picture blinking out; keep it.
- The test fakes for `_render_tv` in older tests have no `full` argument; `_rerender` never passes it. Only the test-send path does.

## Phase 2a (2026-10-21): what it built

Small slice on purpose. **No wizard change, no render change, no behavior change for any server.**

- `database/migrations/078_welcome_gallery.sql` (registered in `database.py`, `SCHEMA_VERSION` 126 -> 127, guard test pin updated): table `discord_welcome_backgrounds` (slot 1 to 10, unique per server+clone, `needs_new_picture` flag for deleted hosted images) and 3 new columns on `discord_welcome_config`: `rotation_mode`, `rotation_counter`, `last_slot_used`. Uses `clone_key` (-1 = main bot) instead of nullable `clone_id` so the unique key works. Old `custom_bg_*` columns untouched.
- `modules/welcome_gallery.py`: pure `pick_slot(...)` for the 4 modes (pin, random never-twice default, in order, by member number), `clean_mode`, `MODE_LABELS` (plain words for the select), `MAX_PICTURES = 10`. Never raises.
- `database.py` methods: `gallery_list`, `gallery_add` (lowest free slot, 10 cap, advisory lock), `gallery_remove`, `gallery_flag_missing`, `gallery_next_counter` (atomic `UPDATE ... RETURNING`), `gallery_set_last_slot`. Join-path ones never raise.
- Tests: `tests/unit/test_welcome_studio_phase2a.py`. A test also asserts the renderer and wizard do NOT import the gallery yet.

## Next small slices (each its own PR)

- ~~2b~~ done, see below.
- **2c**: Pictures step shows the numbered strip + Add/Remove/Make first + rotation select (max 3 easy controls, rest under More). Locked servers see one line + "Unlock with Premium".
- **2d**: multi-upload modal, downscale to 1536 px + JPEG/WebP, caption on host post (server name/ID, picture number), daily upload limit, copy old single picture in as picture 1 (never delete old columns).

## Not tested live

Migration was not run against a real Postgres here (SQL is plain IF NOT EXISTS). `gallery_add` locking and `gallery_next_counter` need a staging DB check.

## Phase 2b (2026-10-21): what it built

Small slice on purpose. **Only the real join (`on_member_join`) changed.** Wizard, `/welcome preview`, the nudge post and the card-features announcement still use the old single picture, and `modules/welcome_card.py` is untouched.

- `discord_bot/cogs/welcome.py`: new `_gallery_bg_bytes_for_render(session, config, bot, member_number)` and `_gallery_slot_url`.
  - Not Premium/Plus (`studio_unlocked` false or errors), or no bot, or empty gallery: calls the old `_custom_bg_bytes_for_render`, so nothing changes for them.
  - Unlocked with pictures: `pick_slot` using `rotation_mode` (default random), `last_slot_used`, the atomic counter (only fetched in "in order" mode with 2+ pictures) and the member number (`member.guild.member_count`).
  - Hosted message deleted (Discord NotFound/Forbidden or no attachment): `gallery_flag_missing` marks the slot and the next picture is tried. A network blip or a picture that will not load is skipped for that join but **not** flagged.
  - Random mode saves `last_slot_used`. Any error anywhere falls back to the old single picture. Never raises.
- Tests: `tests/unit/test_welcome_studio_phase2b.py` (12): locked, unlocked, each mode, deleted, all deleted, will-not-load, flagged ignored, gate error, db error, no bot, only the join site uses it.

### Test numbers (2b)
Full unit suite: 5155 passed, 4 failed, 4 skipped. The same 4 fail on the 2a base without this change: `test_ultra_trial_off::test_new_ultra_trials_are_off` (env flag) and 3 in `test_uncached_member_fixes.py` (pass when run alone, fail in the full run: test-order pollution, not from the studio work).

### Known gaps in 2b (on purpose, for later slices)
- Nothing can put pictures in the gallery yet (that is 2c/2d), so no server sees a change today.
- "Same picture every time" has no pinned-slot column yet; it uses the first picture until 2c adds "Make first" or a pin.
- The admin line "slot needs a new picture" in the wizard is 2c.
- Not tested against live Discord or a real Postgres.

## Next small slices
- **2c**: Pictures step: numbered strip + Add/Remove/Make first + rotation select (max 3 easy controls, rest under More). Locked servers: one line + "Unlock with Premium".
- **2d**: multi-upload, downscale to 1536 px, caption on host post, daily upload limit, copy old single picture in as picture 1.
