# Review note: collection filter card

Branch `feat/catch-visual-filter`, based on `main` (`74375f7`). No SQL changed.

## What it is
`modules/catch_filter_card.py` draws a 720x290 card for the collection filter screen: the rarity row (ANY plus the
five tiers) and the element row (ANY plus the nine elements) with the chosen one lit, and three on/off chips
(Shiny, Favourite, Search). The header says `NO FILTERS` or `n ACTIVE`. Colours come from `catch_theme`.

**The search term is never drawn.** It is player text and the bundled font cannot show every character. The card
only shows that a search is on; the embed description keeps the term as text. The term is not in the render cache
key either (a test pins this).

## Wiring (additive)
- `CollectionFilterCardView(CollectionFilterView)` overrides only `_redraw` (defer first, rebuild, edit with the new
  image) and `_search`. `CardSearchModal(SearchModal)` defers, updates the filter, then redraws.
- `CollectionCardView._open_filters` now defers first, then swaps the collection card for the filter card. If the card
  cannot be drawn the plain screen is shown with `attachments=[]`, so the collection card never lingers. A failed
  edit logs and sends `collection.error` ephemerally.
- Unchanged: `CollectionFilterView`, `SearchModal`, `CollectionBoxView._open_filters` (the plain box, still
  `edit_message`), Show results (`_apply` -> `_back_to_list` sets the attachment itself, so the filter card does not
  linger; a test covers both a non-empty and an empty result).
- Embed text is unchanged; the image is added under it.

## One existing test was changed on purpose
`test_catch_collection_card.py::test_opening_the_filter_screen_responds_first_and_clears_the_card` pinned
`edit_message(..., attachments=[])` on opening the filter screen. That is what this feature changes (it now defers,
draws, then edits with `filter.png`). The test keeps its intent (responds first; the collection card `box.png` must
not linger) and now asserts the new behaviour. Nothing else existing was edited.

## Tests
`tests/unit/test_catch_filter_card.py`: 30 tests (checked with `pytest --collect-only`). Renderer: size, every
rarity/element/toggle changes the card, bad values cleaned, search text kept out of the key and image, caching,
fresh file, header count, "only the part a control owns changes" crop tests, sigils, tick. Screen: defer-first
ordering with a spy on the renderer's filter, plain fallback, failed edit, every callback, search modal, same
controls as the plain screen, Show results, plain box unchanged.
Full suite on this head: 1469 passed, 42 skipped (`python -m pytest -q`). Real-Postgres suite not run (no SQL changed).
Mutation-checked, 20 mutants, each trips a test: rarity/element never lit, three chips wired to the wrong flag,
count ignores shiny, search text in the cache key, key not cleaned, sigil not drawn, tick not drawn, open without
defer, plain screen used on open, failed open edit not reported, redraw without defer, attachments dropped, card not
drawn, wrong filter drawn, failed redraw raises, plain search modal used, search modal without defer, search term
ignored.

## Not verified
Nothing has run in live Discord. Unverified on a real client: how the image looks (phone crop, light/dark theme),
that deferring then editing feels fine on every pick compared with the old instant edit (first render of a new
combination ~0.4 s, later ones cached), and the swap from the collection card to the filter card.
The "UNCOMMON" label is drawn smaller than the other tier names to fit its cell.
