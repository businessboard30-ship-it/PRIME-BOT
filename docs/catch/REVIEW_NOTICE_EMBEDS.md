# Plain messages restyle: refusals and errors as notice embeds (Maxwell said GO 2)

- `modules/catch_notice.py`: `notice_for(key, **fmt)` returns `{"embed": ...}`. The body is the unchanged locale text for
  `key`. Kind (error / warning / info) comes from a table (`KIND_FOR`) plus three prefix rules (`evolve.refused_`,
  `release.refused_`, `creature.nick_` are warnings); anything unknown is info. Colour from `catch_theme.state_color`
  (`danger`, `warning`, `info`), icon from three new `catch_emoji.UI` keys (`error`, `warning`, `info`), title from three new
  locale keys (`catch.notice.error|warning|info`, English only; other locales fall back per key). Body is capped at 4096.
- About 90 send sites in `discord_bot/cogs` now use it (all still ephemeral): gate refusals (`catch.unavailable`, including the
  `/catch` command itself), `*.error` and `*.load_error`, "not yours", not found / gone, no selection, busy, server only,
  encounter cooldown, claim blocked / no items / error, release refusals, nickname refusals, evolve refusals sent as a
  message. `_gate_message` in the creature views became `_gate_notice`.
- **Left as they were on purpose:** the owner setup panel messages (`setup.*`), `collection.jump.invalid`, success messages,
  and notices drawn inside a screen (the evolve refusal shown via `_refresh(...)`, `sell.gone` shown on the sell screen).
- **Existing tests edited: 46 changed lines in 18 files** (the handoff estimated about 31). All are in the same two shapes: a
  capture closure `sent.append(args[0])` became `shown(args, kwargs)`, and `sent[0][0][0]` became `body(sent[0])`; plus
  `test_catch_dex_info` (list comprehension), `test_catch_hub_card` (`_args`) and `test_catch_guide` (it asserted "no embed").
  The expected text in every assertion is unchanged. The helpers in `tests/unit/notice_helpers.py` are strict: they need the
  embed, so a refusal that falls back to bare text fails.
- New tests: `tests/unit/test_catch_notice.py`, 18 tests (counted with `pytest --collect-only`): kinds/colours/icons/titles,
  unknown kind and key never raise, cap, the key table against `locales/en.json` (every error key must be listed), a source
  scan that no listed message is still sent as bare text, a source scan that every literal `notice_for` key exists and has a
  kind, and three real flows through `open_rules` (refusal = warning, load failure = error, server only = info).
  16 mutants (colours, icon, title, cap, raise, body, return shape, table entries, prefix rule, default, prefix strip, plus
  three call sites made plain / non-ephemeral / wrong key), each trips a test. Looped 8 times, no flake. Full suite: 1563
  passed, 42 skipped. CI ruff list and compileall clean.
- **Merge order matters with #96 (guide banner) and #97 (rules banner).** Each of those has tests that say a refusal stays
  plain text (`test_refusal_stays_plain_text` in the guide card tests, and `test_refusal_stays_plain_text_and_loads_nothing`
  plus `test_load_error_stays_plain_text` in the rules card tests). I merged all three in a scratch branch: one trivial import
  conflict in `_views_catch_rules.py`, and exactly those 3 tests fail. If this merges after them, rebase and flip those 3
  assertions to expect a notice embed (about 3 lines).
- Not verified on a real client: how the notices look and whether the title plus colour bar reads well on a phone.
