-- Scam Shield starter rules: the fake "Kai Cenat crypto casino" post (hubowin.com, promo code BET). Additive, idempotent.
INSERT INTO scam_shield_rules (kind, pattern, note) VALUES
    ('word',   'hubowin',          'fake streamer crypto-casino giveaway'),
    ('domain', 'hubowin.com',      'fake streamer crypto-casino giveaway'),
    ('image',  '2d093960743bbedd', 'scam screenshot: fake Kai Cenat X post / hubowin.com')
ON CONFLICT (kind, pattern) DO NOTHING;
