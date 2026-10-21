# Welcome Card Studio: handoff for the next Claude

Repo: `github.com/businessboard30-ship-it/PRIME-BOT` (Discord bot, Python, discord.py 2.7.1, Postgres via asyncpg).
Owner style: short plain updates, decisions given in chat, writes informally. **Update this file in every PR/push** (owner rule), and every push must say what is built and what is next.
Source of truth for the design: `docs/WELCOME_CARD_STUDIO_PLAN.md` (copied from the owner's upload on 2026-10-21).
Owner rules for this work: open every pull request against **`main`**; **merge only when the owner says so** (they did on 2026-10-21 for #423 to #433); no deploy without the owner's OK. Railway deployments have been paused for a long time (owner, 2026-10-21), so a merge to `main` does not reach production yet.

## Status (2026-10-21)

> **2026-10-21: phases 0 to 3c were merged into `main` (PRs #423 to #433), in order, on the owner's instruction.** The older per-phase sections below still say "PR open, not merged" in their own text: that is history, the status table right below is current. Phase 3d (PR #434) and the render-site follow-up (PR #435) are the two open PRs; both are cut from the merged `main`, merge cleanly together, and are not merged yet.

| Phase | State | Where |
|---|---|---|
| 0. Premium gate helper (`studio_unlocked`) | **Built and merged into `main`** (stacked on the Phase 1 branch; audit part of Phase 0 still not done) | branch `feat/welcome-studio-phase0`, PR #424 |
| 1. Wizard restructure | **Built and merged into `main`** | branch `feat/welcome-studio-phase1` |
| 2a. Gallery data layer + rotation picker | **Built and merged into `main`** (small slice, no UI yet) | branch `feat/welcome-studio-phase2a` (stacked on phase 0) |
| 2b. Join render uses the gallery | **Built and merged into `main`** (join only; previews unchanged) | branch `feat/welcome-studio-phase2b` (stacked on 2a) |
| 2c-1. Pictures step: gallery line + rotation select | **Built and merged into `main`** (read-only line + rotation picker; stacked on 2b) | branch `feat/welcome-studio-phase2c` |
| 2c-2. Numbered strip + Remove / Make first + "Unlock with Premium" button | **Built and merged into `main`** (stacked on 2c-1; no Add yet, that is 2d) | branch `feat/welcome-studio-phase2c2` |
| 2d. Multi-upload + downscale + caption + daily limit + migrate old single picture | **Built and merged into `main`** (stacked on 2c-2). **This finishes Phase 2** | branch `feat/welcome-studio-phase2d` |
| 2e. Phase 2 leftovers: Replace a picture, editor preview uses the gallery, Clear background note | **Built and merged into `main`** (stacked on 2d) | branch `feat/welcome-studio-phase2e` |
| 3a. Zoom + pan arrows, text size (first slice of Phase 3) | **Built and merged into `main`** (stacked on 2e) | branch `feat/welcome-studio-phase3a` |
| 3b. Picture frames (none, thin, thick, double, rounded) + frame color | **Built and merged into `main`** (stacked on 3a) | branch `feat/welcome-studio-phase3b` |
| 3c. Nudge the text block (arrows on the Text step) + safe-area guard | **Built and merged into `main`** (stacked on 3b; PR opened against `main` on the owner's instruction, so its diff also carries the unmerged earlier phases) | branch `feat/welcome-studio-phase3c` |
| 3d. 3x3 text position (one cycle button on the Text step) | **Built, PR #434 open, not merged** (cut from merged `main`) | branch `feat/welcome-studio-phase3d` |
| Follow-up. Studio options gated at every place a card is drawn (preview, nudge, setup TV, test post) | **Built, PR #435 open, not merged** (cut from `main`, independent of #434) | branch `feat/welcome-studio-render-sites` |
| 3e (avatar nudge), frame extras (polaroid, film strip, glow) and 4 to 7 | Not started | |

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

## Phase 2c-1 (2026-10-21): what it built

Small slice on purpose. **Only the Pictures step (step 1) changed.** No render change, no upload, nothing stored except the rotation mode.

- Pictures step shows one plain line: `Gallery: N of 10 pictures · <rotation in plain words>`, plus `Picture 2, 5 needs a new picture.` when hosted images were deleted (flags set by 2b). Locked servers (no Premium/Plus) see one line: `Studio is part of Premium.`
- **More controls** on Pictures now also holds a rotation select (`CardRotationSelect`, id field `rotation`, registered in `DYNAMIC_ITEMS`), Premium/Plus only. Easy mode is unchanged (still max 3 controls); More mode is 4 selects + presets = 5, under the limit (test).
- The callback re-checks `studio_unlocked` on the server, then saves with new `db.gallery_set_rotation_mode` (only the 4 known modes are stored) and redraws.
- `_with_studio(config, guild, clone)` adds a read-only `_studio` summary to the config before the view is built (open wizard, redraw, step switch to Pictures). Never raises; on any error the step looks exactly as before.
- Tests: `tests/unit/test_welcome_studio_phase2c.py` (15). Updated the 2a test that said the wizard must not import the gallery (renderer still must not).

### Test numbers (2c-1)
Full unit suite: 5170 passed, 4 failed, 4 skipped. Same 4 as 2b: `test_ultra_trial_off::test_new_ultra_trials_are_off` (env flag) and 3 in `test_uncached_member_fixes.py` (test-order pollution).

### Known gaps in 2c-1 (on purpose)
- Still no way to put pictures in the gallery (2c-2 / 2d), so the count reads 0 for everyone today.
- "Same picture every time" still uses picture 1 (no pinned-slot column yet; "Make first" in 2c-2 solves it).
- No "Unlock with Premium" button yet, only the line. Needs the existing Premium purchase flow wired in (2c-2).
- Not tested against live Discord (select shows in More mode on a phone, saving, redraw).

## Next small slices
- **2c-2**: numbered strip + Remove / Make first (no upload needed) + "Unlock with Premium" button.
- **2d**: multi-upload, downscale to 1536 px, caption on host post, daily upload limit, copy old single picture in as picture 1.

## Rule from the owner (2026-10-21): the studio is open to everyone, only SAVING is Premium

Same idea as Ultra preview mode: anyone can open the studio and play with every control; nothing is stored unless the server has Premium/Plus. **Do not hide studio controls from free servers in later slices; gate the save and the join render instead.**

Applied in 2c-1 (follow-up commit on PR #427):
- The rotation select shows in More controls for **every** server.
- Free server: the pick is kept in memory only (`_ROTATION_DRAFT`, per person, trimmed like the other state) and shown as a "Preview only ... saves with Premium" line. Nothing goes to the database.
- Premium/Plus server: the pick is saved with `db.gallery_set_rotation_mode` (still re-checked on the server).
- Join render is unchanged: only Premium/Plus gets the gallery at join (2b).
- For 2c-2 and later: Add/Remove/Make first, frames, effects etc. should work as an in-memory preview for free servers (nothing hosted or stored), and the save/apply path checks `studio_unlocked`. "Unlock with Premium" button still to add.

## Phase 2c-2 (2026-10-21): what it built

Small slice on purpose. **Only the Pictures step (step 1) and the gallery DB layer changed.** No render change, no upload (Add is 2d).

- **Numbered strip** under the gallery line: `1● 2● 3⚠️ 4○ ... 10○` (● has a picture, ○ empty, ⚠️ needs a new picture). Shown for Premium/Plus servers that have the `slots` list in the `_studio` summary (`_with_studio` now adds `slots`).
- **More controls** on Pictures gets one more row: `🗑️ Remove a picture` and `⭐ Make first` (`CardPictureRemoveButton`, `CardPictureFirstButton`, ids `picremove` / `picfirst`). Each opens a tiny modal asking for the picture number 1 to 10 (`CardPictureNumberModal`). A modal instead of a select because More mode already has the maximum 5 selects. Hidden for a Premium server with 0 pictures (nothing to manage); visible to free servers.
- **`Unlock with Premium`** button (`CardStudioPremiumButton`, id `studiogoprem`) on the Pictures step for servers without Premium/Plus. Sits in the existing upload row, so Easy mode is still 3 control rows. Opens the existing Premium pitch (`send_premium_pitch`).
- **Owner rule kept (studio open to everyone, only saving is Premium):** free servers can open the buttons; the modal's write re-checks `studio_unlocked` on the server and a free server just gets an ephemeral "Preview only ... Pictures are stored and managed with Premium" (a free server has no stored pictures, so there is nothing to preview-change yet; real in-memory preview of added pictures comes with 2d).
- **`db.gallery_make_first(guild, clone, slot)`**: if slot 1 is empty the picture moves there; otherwise the two pictures swap their CONTENT (slot numbers stay put, so the unique key is never hit). One transaction + advisory lock. Clears `last_slot_used`. Never deletes. "Same picture every time" uses picture 1, so Make first is now the pin.
- Remove uses the existing `db.gallery_remove`.
- Tests: `tests/unit/test_welcome_studio_phase2c2.py` (17). Updated the 2c-1 test that asserted the exact `_studio` dict (now includes `slots`).

### Test numbers (2c-2)
Full unit suite: 5188 passed, 4 failed, 4 skipped. Same 4 as 2b/2c-1: `test_ultra_trial_off::test_new_ultra_trials_are_off` (env flag) and 3 in `test_uncached_member_fixes.py` (test-order pollution).

### Known gaps in 2c-2 (on purpose)
- **Remove does not delete the hosted image message** in the image-host channel (the owner reviews that channel; an orphaned image stays there). Delete it best-effort when 2d adds hosting code, or leave it for the owner's review.
- Still no way to ADD a picture, so the gallery reads 0 for everyone until 2d.
- No Undo for Remove / Make first (Undo only covers look changes). Remove is permanent for that slot.
- Not tested against live Discord (modal on a phone, the Unlock pitch from a DM copy of the wizard) or a real Postgres (the swap SQL needs one staging check).

## Next small slices
- **2d**: multi-upload modal, downscale to 1536 px + JPEG/WebP, caption on host post (server name/ID, picture number), daily upload limit, copy the old single picture in as picture 1 (never delete old columns). Free servers: in-memory preview only.
- Then Phase 3 (framing and positioning).


## Phase 2d (2026-10-21): what it built (finishes Phase 2)

Small slice on purpose. **Only the Pictures step, the gallery DB layer and one tiny migration changed.** `modules/welcome_card.py` and the join render are untouched.

- **`➕ Add pictures`** (`CardPictureAddButton`, id `picadd`, registered in `DYNAMIC_ITEMS`): first button in the More-controls manage row on Pictures, shown to **every** server (owner rule: studio open to everyone, only saving is Premium). Remove / Make first still hide when a Premium gallery is empty. Opens `CardPicturesAddModal`: a file field taking **up to 10 files at once** plus one optional link (adds one picture).
- **Premium/Plus server:** `_add_pictures(...)` runs per file, never raises, and reports each skipped file by name: copy the old single picture in as picture 1 (once) then, per file, check room (10 cap), the daily limit, **downscale to 1536 px wide JPEG (q88, never upscales, honors camera rotation, refuses >40 MP before decoding)**, post to the hosting channel via `image_host.post_to_host` with the caption `Studio picture N — guild <id> (<name>)`, then `db.gallery_add`. If hosting is down it stops and stores nothing. If the gallery raced to full after hosting, the orphan post is deleted best-effort.
- **Free server:** nothing is hosted or stored. The first readable picture is shown as the card background in memory (existing draft) with a "Preview only ... a gallery of up to 10 pictures is stored with Premium" note.
- **Daily upload limit:** 20 pictures per server per rolling 24 h (`wg.DAILY_UPLOAD_LIMIT`). Counted as the larger of stored rows from the last 24 h (`db.gallery_count_recent`) and an in-memory list (`wg.note_upload`, catches add-then-remove loops; resets on restart, so it is a best-effort flood guard, not a hard quota).
- **Migration of the old single picture:** `db.gallery_migrate_single` copies `custom_bg_channel_id/message_id/url` into slot 1 once, only if the gallery is empty and the old picture is a hosted message. Old `custom_bg_*` columns are never touched. New column `discord_welcome_config.gallery_migrated` (migration `079_welcome_gallery_migrated.sql`, `SCHEMA_VERSION` 127 -> 128, guard test pin updated) makes sure emptying the gallery later does not bring the old picture back. Pasted-link pictures with no hosted copy are not migrated (nothing to copy); they keep working through the old fallback.
- Tests: `tests/unit/test_welcome_studio_phase2d.py` (19): resize/no upscale/garbage/pixel cap, daily limit (memory + DB + expiry + per server/clone), Add button visibility and Discord limits, modal takes 10 files, caption + migrate-first, 10 cap, daily limit, bad files named, hosting down, raced-full orphan delete, error never raised, migration additive + once.

### Test numbers (2d)
Full unit suite: 5207 passed, 4 failed, 4 skipped. Same 4 as 2b/2c: `test_ultra_trial_off::test_new_ultra_trials_are_off` (env flag) and 3 in `test_uncached_member_fixes.py` (test-order pollution).

### Known gaps in 2d (on purpose)
- **Not tested live:** the real multi-file modal on a phone (Discord's max files per modal field must be confirmed, plan 3.1), real hosting-channel posts, and a real Postgres for `gallery_migrate_single` / `gallery_count_recent` (needs one staging check; run migration 079 first).
- The old "Upload image" / "Clear background" buttons still write only the old single picture. After migration, **Clear background does not remove the gallery copy** (use Remove). A later slice can merge the two flows.
- Free-server preview shows only the first picture; no in-memory multi-picture preview or rotation preview yet.
- Remove still leaves the hosted message in the hosting channel for the owner's review.
- Only the join render uses the gallery; wizard previews and `/welcome preview` still use the single picture.
- Per-picture overrides (own crop / text color) and "Replace" are not built (plan 3.1, still-open owner question 3).

## Next
1. Owner reviews the stacked PRs in order: #423 (Phase 1), #424, #425, #426, #427, #428, then this one. None merged by me.
2. **Phase 3: framing and positioning** (pan/zoom arrows, picture frames, 3x3 position grid, nudge, text size). Keep every control available as a preview for free servers; gate saving and the join render with `studio_unlocked`. Add each new option key to `studio_keys` in `modules/welcome_studio.py`.
3. Small leftovers that could be their own tiny PR: wizard preview uses the gallery, merge old Upload/Clear with the gallery, per-picture overrides, Phase 0 audit (render timing at 1536 px, host-channel limits).
4. Still-open plan questions for the owner: Premium lifetime includes studio? Keep Phase 6 DM extras? Per-picture overrides in v1 or later?

## Phase 2e (2026-10-21): what it built (Phase 2 leftovers)

Small slice on purpose. **Only the Pictures step, the editor preview and one DB method changed.** No migration, `modules/welcome_card.py` and the join render are untouched.

- **`🔁 Replace a picture`** (`CardPictureReplaceButton`, id `picreplace`, registered in `DYNAMIC_ITEMS`): in the More-controls manage row on Pictures, next to Remove / Make first (hidden for a Premium gallery with 0 pictures; visible to free servers). Opens `CardPictureReplaceModal`: picture number (1 to 10), one file or one link. The picture keeps its slot number and place in the order.
- **Premium/Plus:** `_replace_picture(...)` checks the slot exists and the daily limit (a replace counts like an Add), downscales to 1536 px JPEG, posts to the hosting channel with caption `Studio picture N (replaced) ...`, then `db.gallery_replace` swaps the row (clears `needs_new_picture`, so a deleted hosted image is fixed this way) and the OLD hosted post is deleted best-effort. Raced/removed slot deletes the new orphan. Never raises.
- **Free server (owner rule):** nothing hosted or stored; the new picture shows as the card background in memory with the usual "Preview only ... Premium" note.
- **`db.gallery_replace(guild, clone, slot, channel_id, message_id, url_cache, label)`**: one transaction + advisory lock, returns the old `(channel_id, message_id)` or `None` for an empty slot (nothing changed). No DELETE.
- **Editor preview uses the gallery:** `_gallery_preview_bytes` in `_render_tv` shows gallery picture 1 for Premium/Plus servers (stable, the real join still rotates); skips flagged/deleted pictures, caches per message id, returns `None` on any error so the old single-picture path runs exactly as before. Free servers: unchanged (draft picture).
- **Clear background** still only clears the old single picture and never touches the gallery; when gallery pictures exist it now tells the admin so ("use Remove a picture"). Decision kept non-destructive on purpose: wiping up to 10 pictures from one button, with no Undo, felt wrong. Ask the owner if Clear should mean "clear everything".
- Tests: `tests/unit/test_welcome_studio_phase2e.py` (18).

### Test numbers (2e)
Full unit suite: 5225 passed, 4 failed, 4 skipped. Same 4 as 2b to 2d: `test_ultra_trial_off::test_new_ultra_trials_are_off` (env flag) and 3 in `test_uncached_member_fixes.py` (test-order pollution).

### Known gaps in 2e (on purpose)
- Not tested live: the Replace modal on a phone, a real hosting-channel post/delete, a real Postgres for `gallery_replace` (needs one staging check).
- Preview shows picture 1 only. "Preview each picture" / "preview as a different member" (plan 3.9) is not built.
- Per-picture overrides (own crop / text color per picture) are still not built (owner question 3).
- `/welcome preview` and the nudge post still use the single picture.
- Remove still leaves its hosted message for the owner's review.

## Next (updated after 2e)
1. Owner reviews the stacked PRs in order: #423 (Phase 1), #424 to #428, 2d, then 2e. None merged by me.
2. **Phase 3: framing and positioning** (pan/zoom arrows, picture frames, 3x3 position grid, nudge, text size). Keep every control available as a preview for free servers; gate saving and the join render with `studio_unlocked`. Add each new option key to `studio_keys` in `modules/welcome_studio.py`.
3. Still-open plan questions for the owner: Premium lifetime includes studio? Keep Phase 6 DM extras? Per-picture overrides in v1 or later? Should Clear background also clear the gallery?


## Phase 3a (2026-10-21): what it built (first slice of Phase 3)

Small slice on purpose: **zoom + pan arrows for the picture, and text size.** No new table, no migration.

- **New option keys** in `ultra_card_json` (`modules/welcome_card.py`): `zoom` (100/125/150/200), `pan_x`, `pan_y` (-100..100, clamped), `text_size` (small / medium / large / xl). Validated in `parse_ultra_options`; bad values fall back to the defaults. `_zoom_pan_box` shrinks the cover-crop window around its middle by the zoom, then slides it toward an end of the picture by the pan; the window keeps the card's shape and never leaves the picture. Text size scales the name and heading maximum; **safe-area guard:** centered layout caps at x1.2, the 3:1 strip at x1.0.
- **Pixel-identical defaults:** default values take the old code path (`box` unchanged, scale 1.0). Checked by rendering 720 combinations (layouts, ratios, crop focus, big name, three fonts) with the old and new renderer: 0 differences. Test `test_default_options_render_pixel_identical_to_no_options` keeps that cheap check in the suite.
- **Wizard (More controls only, Easy mode unchanged):** step 2 Look gets a row of `◀ ▲ ▼ ▶` (10 per tap) + `🔍 Zoom: N%` (cycles 100 > 125 > 150 > 200); step 3 Text gets `🔠 Text size: ...` (cycles). New classes: `CardPanLeft/Up/Down/RightButton`, `CardZoomButton`, `CardTextSizeButton`, ids `panleft panup pandown panright zoom textsize`, all in `DYNAMIC_ITEMS`. "Reset this step" now also resets them. Pan sits on Look (not Pictures) because Pictures More mode is already at Discord's 40-component limit.
- **Gating (`modules/welcome_studio.py`):** `STUDIO_KEYS`, `STUDIO_DEFAULTS`, `uses_studio_options`, `options_for_render(config)`. **Join render** (`welcome.py` `on_member_join`) now passes `options_for_render(config)`: untouched servers get their stored value back with no Premium lookup; Premium/Plus keep the studio keys; anyone else (or a lookup error) gets them stripped, everything else kept.
- **Saving:** `_save_option` and `_apply_style` call `_gate_studio_changes` (server-side `studio_unlocked`). Premium/Plus: written to the database. Ultra-only server: **preview only** — the values live in memory (`_STUDIO_PREVIEW`, per person, trimmed) and `_effective_config` overlays them so the editor preview shows them (owner rule: open to everyone, only saving is Premium). Unpaid draft servers: in-memory draft as before. `_undo_last` never writes studio values for a non-Premium server.
- Tests: `tests/unit/test_welcome_studio_phase3a.py` (27).

### Test numbers (3a)
Full unit suite: 5252 passed, 4 failed, 4 skipped. Same 4 as 2b to 2e: `test_ultra_trial_off::test_new_ultra_trials_are_off` (env flag) and 3 in `test_uncached_member_fixes.py` (test-order pollution).

### Known gaps in 3a (on purpose)
- Not tested live: the arrow row and zoom/text-size buttons on a phone; real taps on a Premium vs Ultra-only server.
- Only the real join applies `options_for_render`. `/welcome preview`, the setup-wizard previews (`_views_welcome.py`) and `welcome_extras.py` still pass the stored options straight to the renderer, so a lapsed Premium server would still see its studio look there (join strips it). Wire them with the same helper in a tiny follow-up.
- Ultra-only preview-only values are not covered by Undo's memory beyond the snapshot restore (undo restores them, never stores them).
- No "Unlock with Premium" line on the Look/Text steps yet (only on Pictures).
- Pan is per-tap steps of 10, not per-picture; per-picture crop overrides are still not built.
- Still to do in Phase 3: picture frames (border presets), 3x3 position grid for text/avatar, nudge buttons.

## Next (updated after 3a)
1. Owner reviews the stacked PRs in order: #423 (Phase 1) ... 2e (#430), then 3a. None merged by me.
2. **Phase 3b**: picture frames (none, thin, thick, double, rounded, polaroid, film strip, glow edge: keep any glow soft/muted and ask before adding neon). Then 3c: 3x3 grid + nudge. Add each new key to `STUDIO_KEYS` / `STUDIO_DEFAULTS`.
3. Tiny follow-up: apply `options_for_render` at the other render sites.
4. Still-open plan questions for the owner: Premium lifetime includes studio? Keep Phase 6 DM extras? Per-picture overrides in v1 or later? Should Clear background also clear the gallery?


## Phase 3b (2026-10-21): what it built (picture frames)

Small slice. No table, no migration.

- **New keys** `frame` (none / thin / thick / double / rounded) and `frame_color` (the 6 text colors) in `ultra_card_json`, validated in `parse_ultra_options`; added to `STUDIO_KEYS` / `STUDIO_DEFAULTS` so the join render strips them for non-Premium (same gate as 3a) and saves are Premium-only (Ultra-only servers preview in memory).
- **Renderer:** `_draw_frame` draws the border over the finished card (after text), size unchanged. `rounded` is a border with a rounded inner picture (output is converted to RGB, so no transparent corners). Default `none` skips the code entirely: pixel-identical to before (test).
- **Wizard (Look step, More controls):** `🖼️ Frame: ...` cycles the 5 frames; `🎨 Frame color: ...` cycles colors and only appears once a frame is chosen. Classes `CardFrameButton` (id `frame`), `CardFrameColorButton` (id `framecolor`), registered; "Reset this step" resets them. Component count checked (<= 40, <= 5 buttons per row).
- **Left out on purpose:** **glow edge** (plan says ask the owner before any glow/neon), polaroid and film strip (they need layout changes to the text area; do with 3c's position work).
- Tests: `tests/unit/test_welcome_studio_phase3b.py` (12). Full unit suite: 5264 passed, same 4 known failures (env flag + 3 order-pollution tests).
- Not tested live (phone taps). Same render-site gap as 3a (only the join applies the gate).

## Next (updated after 3b)
1. Owner reviews the stacked PRs in order: #423 ... #430 (2e), #431 (3a), then 3b.
2. **3c**: 3x3 position grid for text/avatar + nudge buttons; maybe polaroid/film strip; ask the owner about glow edge.
3. Tiny follow-up: apply `options_for_render` at the other render sites.

## Owner decision (2026-10-21): Premium lifetime and yearly include the studio

Monthly, yearly and lifetime Premium (and Premium Plus) all unlock every studio feature. **No code change needed:** `studio_unlocked` uses `db.is_guild_premium_active`, which only checks that the server's Premium has not expired (plus the grace period), so every plan that sets an expiry date is covered, lifetime included. The earlier open question "Premium lifetime includes studio?" is closed. Still open: Phase 6 DM extras, per-picture overrides, Clear background vs gallery, glow edge frame.


## Phase 3c (2026-10-21): what it built (nudge the text block)

Small slice. No table, no migration. Only the **text block** (member number, name, heading) moves; the avatar and the banner stay where the layout puts them. The 3x3 grid and an avatar nudge are the next slice (3d).

- **New keys** `text_dx` and `text_dy` (whole pixels at card width 1536, clamped to -300..300) in `ultra_card_json`, validated in `parse_ultra_options`; added to `STUDIO_KEYS` / `STUDIO_DEFAULTS`, so the join render strips them for non-Premium and saves are Premium-only (Ultra-only servers preview in memory), same gate as 3a/3b.
- **Renderer:** `_draw_texts` adds the nudge to every line (and to the shadow layer). Default 0/0 adds nothing: pixel-identical to before (test).
- **Safe-area guard** (`_clamp_text_nudge`, pure function): the whole block stays 24 px inside the card edge; banner layout cannot push the text into the avatar (24 px gap, ring included, left or right side); centered layout cannot climb over the avatar. The no-move position is always allowed, so the guard never shifts a default card. The stored value is only clamped to +-300; the guard cuts it at render time, so extra taps past a limit store but show nothing (the hint line says "it stops at the card edge and next to your avatar").
- **Wizard (Text step, More controls only):** a row `◀ ▲ ▼ ▶ ↺ Reset`, 20 px per tap. Classes `CardNudgeLeft/Up/Down/Right/CenterButton`, ids `nudgeleft nudgeup nudgedown nudgeright nudgecenter`, all in `DYNAMIC_ITEMS`. "Reset this step" on Text also clears the nudge. Undo covers it (it goes through `_save_option`). Easy mode unchanged.
- **Why Text and not Look:** Look in More mode is already at 35 of Discord's 40 components (a nudge row would make 41). Text More went from 29 to 35.
- Tests: `tests/unit/test_welcome_studio_phase3c.py` (18).

### Test numbers (3c)
All welcome/card unit test files (42 files): 830 passed, 2 failed. The 2 are `test_dash_card_assets.py::test_missing_key_means_pending_and_key_is_never_logged` and `::test_request_goes_to_free_endpoint_with_image_input`; they fail the same way without this change (checked by stashing) and are not welcome code. The full unit suite was not run this time: 21 unrelated files (cat, scam shield) fail to collect in this environment. The handoff's usual 4 known failures were not re-checked for the same reason.

### Known gaps in 3c (on purpose)
- Not tested live: the arrow row on a phone, real taps on Premium vs Ultra-only.
- Rendered and checked by eye on a test card (default, nudged, pushed past the guard, centered); not checked against a real member avatar or on every font.
- No avatar nudge, no 3x3 grid yet (3d). No "Unlock with Premium" line on the Text step yet.
- Taps past the guard limit keep storing up to +-300 with no visible change. If the owner finds that confusing, the wizard could read the guard limit and disable the arrow; needs the renderer's measured box, so it was left out.
- Same render-site gap as 3a/3b: only the join applies `options_for_render`.

## Render-site follow-up (2026-10-21): studio options are gated everywhere a card is drawn

Small fix, cut from `main` (separate from the 3d PR). No new option, no table.

- **Problem (known since 3a):** only the real join (`welcome.py` `on_member_join`) ran the stored options through `options_for_render`. Five other places handed the raw stored value to the renderer, so a server whose Premium lapsed (or a clone) would still see, and post, its studio look there while the join stripped it.
- **Fixed:** `ultra_options=await options_for_render(config)` at all of them: `/welcome preview`, the nudge post and the "here's what you already have" heads-up card (`welcome.py`), the setup wizard TV and its Preview button (`_views_welcome.py`), and the test post (`welcome_extras.py`). Untouched servers (no studio keys) get their stored value back unchanged with no Premium lookup, so nothing changes for them. The gate never raises and fails closed.
- **Left alone on purpose:** the Customize Card editor's own previews (`_views_card_customize.py`): they are meant to show studio options to Ultra-only servers as a try-before-you-buy preview (owner rule), and Save is what is gated.
- Tests: `tests/unit/test_welcome_studio_render_sites.py` (6): a source guard (no file hands the raw stored options to the renderer again; every `ultra_options=` argument is gated), the setup wizard TV and the test post strip studio keys without Premium and keep other options, the TV keeps them with Premium, untouched options skip the lookup. 4 of the 6 fail without the fix (checked).
- Test numbers: 483 tests in the welcome/card/server-panel/goodbye files: 482 passed, 1 failed (`test_server_panel_phase3.py::test_home_links_to_phase3`, an import error in this environment that fails the same way on `main`).
- Not tested live. The goodbye card and other non-welcome cards are out of scope (they don't read `ultra_card_json`).
- When a new studio option key is added (3d's `text_pos` is in PR #434), it only needs adding to `STUDIO_KEYS` / `STUDIO_DEFAULTS`: every site above picks it up through `options_for_render`.

## Next (updated after 3c)
1. Owner reviews the stacked PRs in order: #423 ... #432 (3b), then 3c (opened against `main`, see the status table).
2. **3d**: 3x3 position grid for the text block and avatar (replaces banner top/bottom + avatar left/right but keeps them as presets), avatar nudge. Polaroid / film strip frames need text-area changes, do with the grid. Ask the owner before any glow edge.
3. Tiny follow-up: apply `options_for_render` at the other render sites (`/welcome preview`, `_views_welcome.py`, `welcome_extras.py`).
4. Still-open plan questions for the owner: Phase 6 DM extras? Per-picture overrides in v1 or later? Should Clear background also clear the gallery? Glow edge frame?


## Phase 3d (2026-10-21): what it built (3x3 text position)

Small slice, cut from the merged `main`. No table, no migration.

- **New key** `text_pos`: `auto` (default, the layout decides, classic look) or one of nine cells `top-left top top-right left center right bottom-left bottom bottom-right` (`ULTRA_TEXT_POS`). Validated in `parse_ultra_options`; added to `STUDIO_KEYS` / `STUDIO_DEFAULTS` (Premium/Plus save and join; Ultra-only previews in memory), same gate as 3a to 3c.
- **Renderer:** the whole text block is moved so it sits in the chosen cell (60 px from the card edge, or centered), then the 3c nudge (`text_dx` / `text_dy`) is added on top, then the **same safe-area guard** cuts it down (on the card, off the avatar). So asking for a cell next to the avatar stops beside the avatar instead of sliding under it. `auto` skips the code: pixel-identical to before (test).
- **Wizard (Text step, More controls):** `📍 Text spot: ...` button cycles Automatic, then the nine cells in reading order (top left first). Class `CardTextPosButton`, id `textpos`, in `DYNAMIC_ITEMS`; "Reset this step" resets it; Undo covers it.
- **Why a button and not a select (changed during the build):** a first version used a select, but it put the Text step at 6 selects and exactly 40 components with the preview attached, which breaks the Phase 1 rule (under 40 components, at most 5 selects; `test_more_mode_stays_inside_discord_limits_on_every_step` caught it). A button adds one component. To keep the Text step under 40 the separate "Move the text..." line from 3c was folded into the step hint line (Text More with preview: 39 components). Cost: up to 9 taps to reach the last cell. If the owner lifts the 5-select rule, a select would be nicer; or two buttons (row, column) would cut taps to 3 + 3.
- Tests: `tests/unit/test_welcome_studio_phase3d.py` (14), including a pixel check that the text lands near the chosen corner and a check that it never overlaps the avatar.

### Test numbers (3d)
All welcome/card unit test files (the same file set as 3c plus the Phase 3d file): 397 passed, 0 failed. The full unit suite was not run (21 unrelated files fail to collect in this environment; the 2 `test_dash_card_assets.py` failures from 3c are not in this file set).

### Known gaps in 3d (on purpose)
- Not tested live: the new button on a phone; real taps on Premium vs Ultra-only.
- Banner "top"/avatar side presets are **not** replaced by the grid (they still exist; the grid only places the text). Moving the avatar itself (avatar nudge) is not built: Look in More mode is at 35 of 40 components. Needs the same trick (a cycle button, or fold a hint line).
- A cell above the banner puts text over the picture; legibility is the admin's call (auto contrast and shadow help).
- Same render-site gap as 3a to 3c: only the join applies `options_for_render`.

## Next (updated after 3d)
1. Owner reviews / merges the 3d PR. Staging check (Railway deploys are paused, so there is no staging yet): the Phase 1 to 3d controls on a phone, Premium vs Ultra-only, wizards posted before these PRs still responding.
2. **3e**: avatar nudge or position, polaroid / film strip frames (need text-area changes). Ask the owner before any glow edge.
3. ~~Tiny follow-up: apply `options_for_render` at the other render sites~~ **Done in PR #435** (see the Render-site follow-up section).
4. Phase 4 onward from `docs/WELCOME_CARD_STUDIO_PLAN.md`.
5. Still-open plan questions for the owner: Phase 6 DM extras? Per-picture overrides in v1 or later? Should Clear background also clear the gallery? Glow edge frame?
