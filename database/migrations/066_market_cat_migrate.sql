-- General Marketplace R1b: copy the old cat market rows into market_listings, KEEPING THEIR IDS (posted Buy / Take back
-- buttons carry the listing id), then close the old open rows so they stop holding cats. Idempotent: reruns copy nothing.
INSERT INTO market_listings (id, item_type, item_ref, seller_id, seller_clone_id, price, fee, guild_id, channel_id, message_id,
                             status, buyer_id, buyer_clone_id, created_at, expires_at, closed_at)
SELECT l.id, 'cat', l.adoption_id, l.seller_id, l.seller_clone_id, l.price, 0, l.guild_id, l.channel_id, l.message_id,
       l.status, l.buyer_id, l.buyer_clone_id, l.created_at,
       CASE WHEN l.status = 'open' THEN NOW() + INTERVAL '14 days' ELSE COALESCE(l.closed_at, l.created_at) END, l.closed_at
FROM cat_listings l
ON CONFLICT (id) DO NOTHING;

UPDATE cat_listings SET status = 'void', closed_at = COALESCE(closed_at, NOW()) WHERE status = 'open';

SELECT setval(pg_get_serial_sequence('market_listings', 'id'),
              GREATEST((SELECT COALESCE(MAX(id), 1) FROM market_listings), (SELECT last_value FROM market_listings_id_seq)));
