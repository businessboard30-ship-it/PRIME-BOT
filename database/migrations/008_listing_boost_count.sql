-- Adds paid "boosts" to server_listings: a purchasable counter that
-- pushes a listing higher in the "trending" sort alongside votes and
-- confirmed conversions (see database.py's _LISTING_SORTS). Boosts are
-- applied via api/apply_boost.py, which currently has no payment
-- verification wired in front of it — see that file's header comment
-- before shipping this to a real payment flow.

ALTER TABLE server_listings
    ADD COLUMN IF NOT EXISTS boost_count INTEGER NOT NULL DEFAULT 0;
