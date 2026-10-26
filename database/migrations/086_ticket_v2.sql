-- Ticket system V2: premium panel look (title, text, colour) and per-ticket category + the member's answer.
-- Additive and idempotent. (categories_json, custom_buttons_json, auto_close_hours, transcript_channel_id and
-- custom_close_message already exist; set_ticket_config now actually saves them.)
ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS panel_title TEXT;
ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS panel_text TEXT;
ALTER TABLE discord_ticket_config ADD COLUMN IF NOT EXISTS panel_color TEXT;
ALTER TABLE discord_tickets ADD COLUMN IF NOT EXISTS category TEXT;
ALTER TABLE discord_tickets ADD COLUMN IF NOT EXISTS topic TEXT;
