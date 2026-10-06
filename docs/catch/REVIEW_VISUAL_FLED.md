# Visual pass 7: "it got away" card (expired spawn)

(Kept in its own file, not appended to `REVIEW_P1_FIXES.md`, so it cannot conflict with the level-up PR's note
that also appends there. Fold it in whenever convenient.)

`modules/catch_fled_card.py` draws a 720x400 washed-out card (grey backdrop, dim medallion, no sparkles, "IT GOT
AWAY...", species name, rarity/element chips, "Nobody caught it in time."). It replaces the wild card on the public
spawn message when `CatchCog._mark_spawn_expired` runs. The expired spawn row already carries `species_id`
(`claim_expired_spawns` RETURNING), so no SQL changed; the level is not in the row, so the card shows no level.

Rules kept: the plain fled embed text is unchanged; the edit passes `attachments=[fled_file]` with the card, or
`attachments=[]` when there is none (the old wild card must not linger); no species, an unknown species, or a render
error means the plain embed; if the card edit fails with a Discord error it is retried once as plain; a fresh
`discord.File` per send; the helper is the module-level `fled_card_for_row(row)` because the existing expiry test
calls `_mark_spawn_expired` on a bare fake `self`.

Not changed on purpose: the ephemeral "creature got away" reply to a failed claim stays plain. The failed
`CatchResult` has no species (adding it means touching `record_catch` SQL) and `test_failed_claim_has_no_card` pins it.

Tests: `tests/unit/test_catch_fled_card.py` (13). Mutation-checked after a green baseline, each trips a test: card never
attached, image not set, plain edit keeps the stale card, no plain retry, view dropped, render errors not swallowed,
second type missing from the cache key.
Not verified: how the card looks on a real Discord client, and that editing the public message to swap the image works
(attachments replaced) in a live channel.
