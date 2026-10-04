# Review fixes for the Phase 1 stack (PRs #69, #70, #71)

Written by the reviewer (Claude) on branch `fix/catch-review-fixes-p1`, based on the tip of
`feat/catch-p1-08-owner-setup` (545ad12). Because the reviewer wrote this code, it needs a
review from the other AI (Vo AI) or the owner before merge.

## Findings fixed

| # | Finding | Fix | Evidence |
|---|---|---|---|
| 1 | Admin Home had 26 buttons (limit 25); "Catch setup" used a plain letter as its emoji | Restored `_views_admin_panel.py` to `main` | `tests/unit/test_admin_panel_money.py::test_hub_buttons_fit_discord_rows` passes again |
| 2 | Two hub tests failed (stale component count; `.template.pattern` is a property) | Count is 8 with a real action-row check via `view.to_components()`; template read from `__discord_ui_compiled_template__` | `tests/unit/test_catch_hub.py` passes |
| 3 | DB/permission work ran before the first response in `/catch`, hub setup, encounter and setup refresh (rule B.6) | `defer()` first, then gate/permission/DB, then `followup` | `tests/unit/test_catch_interaction_timing.py` (8 tests; all 8 fail on the old code, all pass now) |
| 4 | P1-08: no join-DM toggle, no Server Owners Panel entry; setup shown to everyone; `interaction.permissions` used instead of `user_can_manage_guild` | Join-DM "Creature catching" toggle (`_enable_catch`), Features hub button, `user_can_manage_guild` checked before showing and before saving | `tests/unit/test_catch_owner_entrypoints.py` (6 tests) |
| 5 | Setup load/save ignored `clone_id` (rule 0.4) | `CatchSetupView` carries `clone_id`; passed to `load_setup`, `save_setup`, `set_feature_flag` | `test_setup_view_passes_clone_id_to_storage` |
| 6 | `logger` was used but never defined in `catch.py` (NameError inside four error handlers); unused `ThrowChoice` import | Defined `logger`; removed the import | `ruff check --select F,E9` clean |
| 7 | Duplicate `CATCH_DYNAMIC_ITEMS` import in `bot.py`; `try/except/pass` in a test; no lint in CI | Removed duplicate; `pytest.raises`; ruff (`F,E9`) step added to `ci.yml` for the catch files | CI file, test file |

## Verified after the fixes
- `python -m pytest tests -q`: 931 passed, 21 skipped.
- `ruff check --select F,E9` on the catch modules, `catch.py`, the import script and the catch tests: clean.
- `python -m compileall -q .`: clean. Stub scan on added lines (TODO/FIXME/NotImplementedError/bare `pass`/`...`): empty.

## Still NOT done (do not mark P1-08 complete yet)
- ~~"Create #wild-zone" and "Test spawn" only sent a message~~ **Fixed:** both defer first, check Manage Server, then create/reuse #wild-zone and save it as a spawn channel (needs Manage Channels), or post a real persisted `source="test"` spawn through the shared `publish_spawn` helper. Tests: `tests/unit/test_catch_setup_actions.py`.
- ~~After a restart the hub Setup button and category select did nothing useful~~ **Fixed:** `CatchHubDynamicButton` now handles `setup`; new `CatchHubDynamicSelect` restores the category select. The button template excludes `catch:hub:category` because discord.py dispatches every matching template regardless of component type. Test: `test_hub_setup_button_and_category_select_survive_restart`.
- Collection and Dex are now real (`modules/catch_collection.py`, `discord_bot/cogs/_views_catch_collection.py`, tests in `test_catch_collection.py`): paged owned list with sort and favourite toggle, and a Dex with caught/seen/unknown states. Still placeholder replies ("is ready for this server"): Wild zone, Trade, Gifts, Leaderboard, Shop, Sell, Wallet, Team, Battle, Moves, Guide, Rules, Status. They must not count as built features.
- Daily and Inventory are now real (`modules/catch_items.py`, `discord_bot/cogs/_views_catch_items.py`, `test_catch_items.py`). **Found while building:** nothing ever granted balls, so every throw failed with "not available". Fixed with a one-time starter kit (10 basic capsules, flag on `catch_players.settings`, granted atomically), Daily (5 capsules + coins, streak bonus, locked player row so a double tap pays once), and a friendly "out of capsules" message in the claim flow. Reward numbers are placeholders for P11-06 balance tuning. No real-Postgres run of the new SQL yet.
- The real-Postgres migration smoke test and the Phase 2 code in #71 (spawn trigger, throw/claim) were not reviewed.
