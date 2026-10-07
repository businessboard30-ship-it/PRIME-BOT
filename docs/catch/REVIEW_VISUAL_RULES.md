# Visual pass 13: rules banner (Server rules)

- `modules/catch_rules_card.py`: 720x250 banner. Title, ON/OFF pill (green when on, grey when off), the speed preset name, and
  three tiles: SPAWN EVERY n MSGS, MIN WAIT, TIME TO CATCH. Durations read as minutes only when whole, otherwise seconds.
- Only numbers and fixed words are drawn. The preset is checked against `SPEED_PRESETS` (anything else draws as `normal`),
  numbers are clamped (negative to 0, huge capped), channel and role lists are never drawn.
- Cached per setup key (`lru_cache(maxsize=64)`). `rules_art(embed, setup)` sets the embed image and returns a fresh
  `discord.File`, or returns `None` with the embed untouched if anything fails (logged).
- Wiring: `open_rules` calls it after the gate and the setup load, sends with `send_kwargs(file)`. **The embed text and every
  field are kept**, including the channel lists. Refusals, "server only" and the load error stay plain text.
- No existing test was changed.
- Tests: `tests/unit/test_catch_rules_card.py` has 19 tests (counted with `pytest --collect-only`). 19 mutants were tried,
  each trips a test (the "channels drawn" mutant crashes with a NameError, so it only proves the card is not silently
  drawn from other data, not that the test is precise). One survivor on the first run (pill text) was fixed. Looped 8 times,
  no flake. Full suite: 1564 passed, 42 skipped.
- Tile text is hard-coded English inside the image, like the other cards.
- Not verified on a real client: image rendering and phone crop.
