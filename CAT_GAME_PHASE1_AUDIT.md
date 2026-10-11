# Cat Game Phase 1 Audit

## Scope completed
This pass implements the first three requested features:

1. **Daily care and streaks** — one UTC claim per owner/bot per day, bounded 30-day streak, coin reward, and a weekly diamond reward.
2. **Cosmetics shop foundation** — persistent hats, collars, and profile-card backgrounds with coin/diamond prices and atomic wallet debits.
3. **Public profile/showcase foundation** — profile bio, visibility flag, up to three pinned owned cats, collection size, and rarest-cat summary.

The `/cat` response now includes compact, emoji-led buttons for Daily care, Cosmetics, and My profile. The layout intentionally stays under Discord's component limit.

## Correctness and security notes
- All persistent writes use parameterized SQL and transactions.
- Daily care is idempotent for the UTC calendar day.
- Cosmetic ownership is separate from cat ownership, so selling, gifting, trading, or releasing a cat cannot remove purchased cosmetics.
- Pinned showcase IDs are filtered against the member's current collection before display.
- Profile bio is capped at 280 characters in storage.
- Clone/main-bot wallet scoping uses the existing normalized clone key convention.
- The existing fixed-price sale flow keeps the listing accessible: a listing stores the cat ID and renders the listing embed, while the cat remains owned by the seller until the atomic purchase transaction. The cat is not deleted or detached merely because it is listed. This is the intended accessible-for-sale behavior; the seller cannot release or trade it while the listing is open.

## Database change
Migration `043_cat_daily_cosmetics_profile.sql` adds daily claims, cosmetic catalog/ownership, and profile tables. `database.py` schema version is bumped from `87` to `88`.

## Deliberately not included in this phase
Daily quests, auctions, collections/badges, kittens, events, server competitions, wishlist, undo/history, and advanced My Cats search/filter/bulk actions remain follow-up phases. They should be added behind the existing command/component budget rather than creating many new global slash commands.

## Review checklist for the next AI
- Apply migration 043 on a disposable database and verify nullable clone uniqueness on the generated clone key.
- Exercise one owner in main bot and clone accounts separately.
- Verify concurrent daily-care claims result in one reward.
- Verify insufficient balances cannot produce negative coins or diamonds.
- Verify cosmetics remain available after selling/releasing a cat.
- Verify the cat profile's owner ID is rendered by the caller layer if public-card copy requires it; this phase keeps the profile data model ready and the private `/cat` button output scoped to the requesting user.
- Verify persistent Discord views/custom IDs are registered if these buttons need to survive process restarts.
- Verify total components in the `/cat` view remains below Discord's 25-component limit as future buttons are added.

## Test status
Per request, no tests or runtime checks were run in this pass. Another AI should run syntax, migration, concurrency, and Discord interaction checks before production deployment.
