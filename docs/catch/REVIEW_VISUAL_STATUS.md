# Review note: status card

Branch `feat/catch-visual-status`, based on `main` (`1373d31`, includes the merged filter card #92). No SQL changed.

## What it is
`modules/catch_status_card.py` draws a 720x324 card for the player's status screen: six tiles for coins, total
catches, collection (owned, shiny, favourite), catch streak (now and best), daily streak (a READY or WAITING chip) and
Dex progress (caught / total with a caught-and-seen bar). Colours come from `catch_theme`. Only the player's own numbers
are drawn; there is no player text anywhere on the card.

**The Daily countdown is not drawn.** An image cannot tick down and a cached one would go stale, so the card only shows
READY or WAITING and the embed keeps its Daily field (with the `<t:...:R>` timestamp) as text.

## Wiring (additive)
- `open_status` calls `status_art(embed, status)` after the gate and the load, then sends with `**send_kwargs(file)`.
  The response-first order, gate, load and error paths are unchanged.
- When the card renders, the five plain fields it replaces (coins, catches, collection, streak, dex) are removed and
  the image is set. The Daily field stays. If drawing fails, `status_art` logs and returns `None` with the embed
  untouched, so the full plain embed is sent and no `file` is passed.
- `status_embed` is unchanged. **No existing test was edited.** The reply is a new ephemeral followup, so there is no old
  image to clear.

## Tests
`tests/unit/test_catch_status_card.py`: 29 tests (checked with `pytest --collect-only`). Renderer: size, every number
changes the card, each number changes only its own tile (12 parametrized crop tests), negatives and extremes, the daily
countdown never reaches the renderer, caching and fresh file, ready chip colour, Dex bar caught/seen/empty, exact tile
text and order, chip label, an icon in every tile. Wrapper: image set, fields dropped, Daily kept with its timestamp,
failure leaves the embed alone. Screen: response, gate, load, render, send order; the loaded status reaches the
renderer; failed render sends the full plain embed; refused and failed-load paths draw nothing.
Full suite on this head: 1498 passed, 42 skipped (`python -m pytest -q`). Real-Postgres suite not run (no SQL changed).
Mutation-checked, 20 mutants, each trips a test: negatives not clamped, daily state inverted, coins/catches swapped,
best shows current, shiny/favourite swapped, Dex seen or caught not drawn, seen bar too short, zero Dex total divides,
chip always grey, chip label swapped, sigil or coin not drawn, a replaced field not dropped, no fields dropped, image not
set, failed render raises, card not drawn, file not sent, wrong status drawn. One mutant (shiny/favourite swapped)
survived first because swapping the two still changed the tile; the tile text moved into a small pure function
(`_lines`) and a test pins the exact strings.

## Found by looking
The first render had the Dex tile's big number sitting on its progress bar. Fixed by drawing that tile's number smaller
and higher and moving the bar and the "seen" line down; re-rendered and checked.

## Not verified
Nothing has run in live Discord. Unverified on a real client: how the image looks (phone crop, light/dark theme) and how
the card sits above the Daily field.
