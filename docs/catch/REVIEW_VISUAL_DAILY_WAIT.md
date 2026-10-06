## Visual pass 10: daily "not ready" card

`modules/catch_daily_wait_card.py` draws a 720x250 card for the Daily screen when the reward is not ready yet: a muted
(gold pulled halfway to grey) coin with a clock badge, "DAILY REWARD", "NOT READY YET", "COME BACK SOON", and the same
seven streak pips and "DAY n" tag as the claimed card (pips wrap every 7 days, no tag at streak 0). Only the streak
number is drawn. No database, no player text.

The live countdown is NOT drawn. It stays in the embed description as a Discord relative timestamp (`<t:...:R>`), because
an image cannot tick down and a cached image would go stale. So the embed text is unchanged and the card sits under it.

Wiring, additive: `daily_wait_art(embed, *, streak)` returns a fresh `discord.File` and sets the embed image, or returns
`None` and leaves the embed untouched when drawing fails (the player then gets the old plain embed). `open_daily` in
`_views_catch_items.py` gained one `else:` branch for the not-claimed case. The claimed path, the defer-first order and the
gate are unchanged. The reply is a new ephemeral followup, so there is no old image to clear.

**One existing test was changed on purpose:** `test_catch_coin_cards.py::test_daily_not_ready_stays_a_plain_embed` asserted
no file and no image on the not-ready reply, which is exactly what this feature changes. It is renamed
`test_daily_not_ready_sends_the_wait_card_and_keeps_the_countdown_text` and now asserts the `daily_wait.png` file, the
embed image URL, and that the description (with the countdown) equals the plain `daily_embed` description. If you object,
revert only that block and the `else:` branch.

Tests: `tests/unit/test_catch_daily_wait_card.py` has 21 tests (renderer for any streak incl. negative and huge, pips and
wrap, gold vs dark pip pixels, tag drawn or absent, negative = zero, differs from the reward card, cache; wrapper: image set
and text kept, fresh file per call, failure leaves the embed untouched, negative streaks share one cache entry; screen:
card plus unchanged text, renderer gets the real streak, failed render still sends the plain embed, claimed daily does not
use this card). Mutation-checked, each trips a test: card not wired, wrong streak passed, claimed path also takes the wait
path, image not set, text dropped, failure not swallowed, no pip wrap, tag always drawn, negative streak not clamped in
the wrapper, filled pips not gold. Three mutants survived at first (tag at streak 0, filled-pip colour, a clamp in the
renderer that was dead code because `streak <= 0` already covers it); two got new tests and the dead clamp was removed.

No SQL changed. Unit suite: 1434 passed; ruff `F,E9` and compileall clean. Real-Postgres suite not run. Not verified on a
real client: how the image looks (phone crop, dark/light theme) under the countdown text. Card text is hard-coded English,
as on the other cards.
