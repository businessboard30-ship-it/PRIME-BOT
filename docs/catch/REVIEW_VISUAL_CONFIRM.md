## Visual pass 8: evolve and release confirm cards

`modules/catch_confirm_card.py` draws two 720x400 cards. **Evolve:** the creature now, an arrow, what it becomes (two
medallions, names, rarity/element chips) and five stat tiles `before > after` with the difference coloured (green up,
red down). **Release:** a muted "RELEASE?" farewell card (grey-washed medallion, name, LV/rarity/element chips, SHINY or
SPECIAL, YOUR BUDDY, "Set free for good. No coins, no undo."). Nicknames are never drawn.

Wiring in `_views_catch_creature.py`, additive: `evolve_confirm_message(pv, detail)` and
`release_confirm_message(d)` wrap the unchanged `evolve_confirm_embed` / `release_confirm_embed`. The evolve card
replaces the plain stats field (removed only when the card rendered); the release embed keeps all its text because it
carries the warning. Render failure, unknown species, or a species with no evolution target gives the old text embed
with `attachments=[]`. Both confirm exits (Cancel, confirm, Back) already redraw through `creature_message` or the list,
which set `attachments` themselves, so the confirm card cannot linger (pinned by a test).

**One existing test was changed on purpose:** `test_catch_card.py::test_release_and_evolve_confirm_screens_clear_the_card`
asserted that the confirm screens have no image. That is exactly the behaviour this pass replaces, so it now asserts the
screen carries only its own card (`evolve.png` / `release.png`) and never the old `creature.png`.

Tests: `tests/unit/test_catch_confirm_cards.py` (19). Mutation-checked, each trips a test: stats field kept, image not
set (evolve, release), fallback keeps a half-edited embed, release failure not swallowed, old embeds restored on both
buttons, wrong stats / shiny / buddy passed to the renderer, buddy pill not drawn, `after` or buddy missing from the
cache key, level lost in the release render. (One run hit a pre-existing identical `buddy=d.is_buddy` in `creature_embed`
and survived; the real line was re-mutated by line number and is caught.)

No SQL changed. Unit suite: 1380 passed; ruff `F,E9` and compileall clean. Real-Postgres suite not run. Not verified on
a real client: how the cards look in Discord (phone crop, dark/light), and that the edit swaps the image cleanly.
Card text ("EVOLVE?", "RELEASE?", stat names) is hard-coded English. The stat numbers on the preview were sample values.
