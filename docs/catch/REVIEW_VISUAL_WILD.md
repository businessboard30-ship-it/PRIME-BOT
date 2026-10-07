# Review note: wild zone banner card

## What changed
- **New card** `modules/catch_wild_card.py`: a 720x200 banner for the Wild zone screen. One orb per live spawn (up to 10,
  soonest to leave first, the same order as the embed list), filled with the rarity colour from `catch_theme`, with a sparkle
  and the shiny colour when shiny. Unused slots stay as dim outlines, so an empty zone reads as quiet. The right side says
  `N IN THE WILD` (the real total) or `QUIET`; `+K MORE` appears only when all 10 slots are full and there are more.
  No creature, player or channel text is drawn.
- **Wiring** (`discord_bot/cogs/_views_catch_wild.py`): `open_wild_zone` now calls `wild_art(embed, spawns, total)` and sends
  the file with `send_kwargs`. The embed text (list, jump links, footer) is unchanged and never removed. If the card cannot be
  drawn the plain embed is sent as before. The card also shows on an empty zone.
- Static input only (rarity, shiny, total), so the render is cached (`lru_cache`, 64 entries) and drawn in a thread.

## Existing tests
**No existing test was edited.** `tests/unit/test_catch_wild.py` passes unchanged.

## Tests
`tests/unit/test_catch_wild_card.py`: 21 tests (counted with `pytest --collect-only`): size and PNG, cache and determinism,
title and count drawn, exact words drawn (spy on `ImageDraw.text`), quiet state, empty slots outlined, one orb per spawn in its
rarity colour, only as many orbs as spawns, shiny sparkle and colour, `+K MORE` rules, theme colours, file and image wiring,
cap at 10 and order, negative total clamped, render failure leaves the embed untouched, `open_wild_zone` sends the card with all
text kept, empty zone, plain fallback, spawns and real total handed to the card, defer-first and gate-before-load order.
Looped 6 times, no flake. CI ruff list clean.
Mutation-checked, 23 mutants, each trips a test: title missing, count always quiet, count uses shown instead of total, quiet
label not muted, rarity colour ignored, shiny colour ignored, sparkle removed, `+K MORE` too eager, never shown, wrong number,
off-by-one lit slot, dim outline removed, slots not capped, order reversed, total not clamped, cache removed, image not set,
render failure raises, element colour hardcoded, card never attached, file dropped from send, spawns not passed to the card,
shown count passed as total.

## Not verified
- Nothing ran in live Discord: how the banner sits above the list on a real client.
- No SQL changed, so real Postgres was not run.
- Preview images were viewed locally (full, 10 orbs with `+13 MORE`, and the quiet state).
