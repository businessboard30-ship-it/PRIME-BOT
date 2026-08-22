-- Heist Wars — items/inventory/loadout expansion.
--
-- Additive only. Does not alter heist_runs, heist_players, heist_events_log
-- state-machine columns, or any existing constraint — the core heist system
-- (001_heist_wars.sql) keeps working unmodified.
--
-- Item *definitions* stay in code (game/items.py ITEMS dict), same pattern
-- as heist_locations vs game/locations.py: these tables hold only
-- ownership/equip state, never catalog content, so there is nothing here
-- that can drift out of sync with game logic in a way that breaks it.

BEGIN;

-- Item drop attached to a completed heist's payout. Nullable columns on the
-- *existing* reward ledger row (not a new grant table) so the item grant
-- rides the same UNIQUE(run_id) atomicity heist_service.complete_heist
-- already guarantees for cash/xp — a retried completion request cannot
-- grant an item twice, by construction, without any new locking.
ALTER TABLE heist_reward_ledger
    ADD COLUMN IF NOT EXISTS item_key    TEXT,
    ADD COLUMN IF NOT EXISTS item_rarity TEXT;

-- Player inventory. Stackable items (currently none in v1 content, but the
-- column exists for future consumables) use quantity > 1; unique/owned-once
-- items are quantity = 1 and never incremented further.
CREATE TABLE IF NOT EXISTS heist_player_inventory (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    clone_id      INTEGER,
    user_id       BIGINT NOT NULL,
    item_key      TEXT NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    acquired_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS heist_player_inventory_identity_item
    ON heist_player_inventory (guild_id, COALESCE(clone_id, -1), user_id, item_key);
CREATE INDEX IF NOT EXISTS idx_heist_player_inventory_lookup
    ON heist_player_inventory (guild_id, COALESCE(clone_id, -1), user_id);

-- Player loadout. Three tool/equipment slots (gameplay) + three cosmetic
-- slots. Slot columns hold an item_key or NULL — validated against
-- ownership and category server-side by item_service before every write
-- and again before every heist start; never trusted from a Discord
-- component's custom_id.
-- clone_id is nullable (solo/non-clone deployments), so it cannot be part
-- of a NOT NULL PRIMARY KEY. Identity is enforced the same way every other
-- per-player table in this schema does it: a COALESCE(clone_id, -1) unique
-- index, used as the ON CONFLICT target for upserts.
CREATE TABLE IF NOT EXISTS heist_player_loadout (
    guild_id      BIGINT NOT NULL,
    clone_id      INTEGER,
    user_id       BIGINT NOT NULL,
    tool_slot_1   TEXT,
    tool_slot_2   TEXT,
    tool_slot_3   TEXT,
    skin          TEXT,
    mask          TEXT,
    badge         TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS heist_player_loadout_identity
    ON heist_player_loadout (guild_id, COALESCE(clone_id, -1), user_id);

-- Audit log for inventory/loadout mutations (brief §29). Mirrors
-- heist_events_log's shape/conventions.
CREATE TABLE IF NOT EXISTS heist_item_log (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT NOT NULL,
    clone_id     INTEGER,
    user_id      BIGINT NOT NULL,
    action       TEXT NOT NULL,   -- granted | equipped | unequipped | drop_rolled
    item_key     TEXT NOT NULL,
    source_type  TEXT,            -- e.g. HEIST_REWARD
    source_id    TEXT,            -- e.g. run_id, kept as text to stay source-agnostic
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_heist_item_log_identity
    ON heist_item_log (guild_id, COALESCE(clone_id, -1), user_id, created_at);

COMMIT;
