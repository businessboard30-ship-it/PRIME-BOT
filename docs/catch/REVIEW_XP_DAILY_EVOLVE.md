# Review note: XP slice two (daily buddy XP + Evolve button on the level-up message)

## What changed
- **Daily XP.** Claiming the daily reward now gives the buddy XP: 20 base + 5 per streak day, the bonus stops at day 7
  (max 55). Uses the existing `daily` source in `catch_xp.SOURCES` (100 per grant, 300 per UTC day), so `grant_xp` and
  its caps, locks, replay check and audit row are unchanged. The idempotency key is `daily-claim:<unix time the next claim
  becomes ready>`, unique per claim, so a retried callback cannot pay twice. No buddy means no XP.
- **Embed.** `open_daily` adds one "Buddy" field after the reward card when XP was gained (plain text, locale keys).
  The reward card, defer-first order and gate are unchanged. Failures are logged and swallowed: XP can never hide the reward.
- **Evolve button.** The level-up message (catch path and the new daily path) gets one Evolve button when the buddy has a
  level-based evolution available at the new level (`evolve_ready`). It opens the SAME `EvolveConfirmView` as the creature
  screen (new file `_views_catch_levelup.py`), so ownership, gate, eligibility and the single spend are still decided there.
  The button defers first, checks the `view` gate, reloads the creature and preview, refuses in words if it is no longer
  ready. Back from the creature screen closes the stale level-up message (`attachments=[]`).
- If the button cannot be built the card is still sent without it. If the card fails nothing is sent (as before).

## Existing tests
**No existing test was edited.** Existing `_send_levelup` tests pass unchanged (the button is only passed as `view=` when it applies).

## Tests
`tests/unit/test_catch_xp_daily_evolve.py`: 36 tests. Full suite on this branch: 1505 passed, 42 skipped. CI ruff list
(now includes `_views_catch_levelup.py`) and `compileall` clean. Looped the new file 8 times, no flake.
Mutation-checked, 19 mutants, each trips a test: bonus not capped, wrong source, key not per claim, no-buddy grants anyway,
grant on unclaimed daily, XP failure raises, level-up message not sent, sent before the reward, wrong wording on level-up,
button always shown, button for the wrong creature, no owner check, no defer, no gate, eligibility ignored, busy flag never
reset, stale card not cleared on Back, no button on the catch path, button-build failure raises.

## Not verified
- Nothing ran in live Discord: how the button looks under the card, and that the defer then edit swaps the level-up card for
  the evolve confirm card cleanly on a real client.
- Real Postgres was not run. The only new SQL is a copy of the buddy lookup already used by `grant_buddy_catch_xp`;
  `grant_xp` itself is untouched.
- The XP numbers (20 / +5 / cap 7 days) are a first guess, easy to change in `catch_xp.py`.
- Duplication: `send_levelup` (daily path) repeats the body of `SpawnClaimView._send_levelup`, because existing tests patch
  names inside `catch.py`. Merge them later if wanted.
