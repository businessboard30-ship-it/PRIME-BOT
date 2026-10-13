-- Goodbye message styling (PREMIUM servers): custom title, colour, banner image, footer, and whether the leaver's
-- avatar is shown. Additive/idempotent. NULL / TRUE means "the plain default look", so every existing server is unchanged.
-- Saved choices are kept when Premium lapses but are only USED while the server is Premium (see modules/goodbye_style.py).
ALTER TABLE discord_welcome_extras ADD COLUMN IF NOT EXISTS goodbye_title TEXT;
ALTER TABLE discord_welcome_extras ADD COLUMN IF NOT EXISTS goodbye_color INTEGER;
ALTER TABLE discord_welcome_extras ADD COLUMN IF NOT EXISTS goodbye_image_url TEXT;
ALTER TABLE discord_welcome_extras ADD COLUMN IF NOT EXISTS goodbye_footer TEXT;
ALTER TABLE discord_welcome_extras ADD COLUMN IF NOT EXISTS goodbye_show_avatar BOOLEAN NOT NULL DEFAULT TRUE;
