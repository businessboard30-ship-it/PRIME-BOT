# Visual pass 12: guide banner (How to play)

- `modules/catch_guide_card.py`: 720x260 banner, static (no inputs), drawn once and cached (`lru_cache(maxsize=1)`).
  "HOW TO PLAY", "5 QUICK STEPS" and five numbered tiles (CATCH, CAPSULES, RARITY, COINS, COLLECTION), each with an
  element sigil and colour from `catch_theme`; the RARITY tile also shows the five tier dots. No player text is drawn.
- `guide_art(embed)` sets the embed image and returns a fresh `discord.File`, or returns `None` with the embed untouched
  if drawing fails (logged).
- Wiring: `open_guide` calls `guide_art` after the gate and sends with `send_kwargs(file)`. **The embed text and all five
  fields are kept** (the banner only shows headings; the guide text is the content). Refusals stay plain text.
- No existing test was changed. `guide_embed()` and its tests are untouched.
- Tests: `tests/unit/test_catch_guide_card.py` has 15 tests (counted with `pytest --collect-only`). 12 mutants, each trips a
  test: card not called, file not sent, image not set, failure raises, fields cleared, rarity dots, title, step count,
  sigils, theme ignored, labels, cache removed. Looped 8 times, no flake. Full suite: 1560 passed, 42 skipped.
- Tile labels are hard-coded English inside the image, like the other cards.
- Not verified on a real client: image rendering and phone crop.
