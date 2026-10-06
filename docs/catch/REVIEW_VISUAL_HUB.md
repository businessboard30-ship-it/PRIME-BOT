## Visual pass 9: hub menu card

`modules/catch_hub_card.py` draws a 720x360 card from the hub's own static data (`CATEGORIES`): "CATCH HUB", the six
categories as tabs (element sigil and colour per category, the selected tab lit) and the selected category's three
actions with their blurbs. No database, no player text. Category to element: play ember, collect verdant, social tide,
economy lumen, battle volt, info frost (unknown keys fall back to stone).

Wiring in `discord_bot/cogs/catch.py`, additive: `hub_parts(category)` returns the embed and a fresh file (the plain
fields are dropped only when the card rendered; on any failure it is the unchanged `build_hub_embed` and `None`).
`CatchHubCardView(CatchHubView)` overrides only `_select_category` and `_home`: they DEFER FIRST (rule B.6), redraw, then
`edit_original_response(..., attachments=...)`, and tell the player (`catch.hub.error`, one new locale key appended
textually) if the edit itself fails. `/catch` now opens the card view with `send_kwargs(file)`. `CatchHubView`,
`build_hub_embed` and the restart-safe dynamic items keep their behaviour; the one edit to existing code is that the
dynamic category select now passes `attachments=[]`, so after a restart a message that still carries the card does not
keep a stale image under the plain hub. Hub actions (Collection, Shop, ...) open new ephemeral messages, so they never
replace the hub card.

No existing test was changed. Tests: `tests/unit/test_catch_hub_card.py` (20). Mutation-checked, each trips a test:
plain fields kept, image not set, fallback half-edited, redraw does not defer first, attachments dropped from the edit,
category not updated, `/catch` uses the plain view, file dropped from the send, dynamic select keeps the old image, Home
goes to the wrong category, failed-edit message dropped, category not normalised, selected tab not lit (survived at
first, now isolated by a test with fixed colours and dots), actions not drawn, selection ignored, selection missing from
the render key.

No SQL changed. Unit suite: 1382 passed; ruff `F,E9` and compileall clean. Real-Postgres suite not run. Not verified on a
real client: image rendering, and how the defer-then-edit on category change feels compared with the old instant edit
(a first render of a category takes about 0.4 s, later ones are cached). Card text ("CATCH HUB", category and action
names and blurbs) is hard-coded English, as those names already are in `CATEGORIES`.
