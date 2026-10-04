"""Phase 1 catch game schema (P1-02). Called from Database._create_tables.

Every statement is idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS) so a
re-run is a no-op. Per-player rows are keyed by (user_id, clone_key) where
clone_key = COALESCE(clone_id, -1) is a stored generated column: that keeps
the main bot (clone_id NULL) and every clone in separate scopes while still
allowing ``ON CONFLICT (user_id, clone_key)`` upserts.

catch_audit is append-only: no code path UPDATEs or DELETEs it except the
retention archiver (P1-09), which deletes only rows it has already archived.
"""

from __future__ import annotations

CLONE_KEY = "clone_key INTEGER GENERATED ALWAYS AS (COALESCE(clone_id, -1)) STORED"

PHASE1_TABLES = (
    "catch_species",
    "catch_guild_config",
    "catch_spawns",
    "catch_owned",
    "catch_dex",
    "catch_players",
    "catch_inventory",
    "catch_cooldowns",
    "catch_feature_flags",
    "catch_reminders",
    "catch_assets",
    "catch_theme",
    "catch_stats_daily",
    "catch_audit",
)

STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS catch_species (
        id SMALLINT PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        rarity SMALLINT NOT NULL CHECK (rarity BETWEEN 0 AND 4),
        element TEXT NOT NULL,
        element2 TEXT,
        habitat TEXT NOT NULL,
        base_stats SMALLINT[] NOT NULL,
        art_key TEXT,
        evolves_to SMALLINT,
        evolve_level SMALLINT,
        evolve_item TEXT,
        form_of SMALLINT,
        catch_rate_mod REAL NOT NULL DEFAULT 1.0,
        spawn_weight INTEGER NOT NULL DEFAULT 100 CHECK (spawn_weight >= 0),
        exclusive_to TEXT,
        egg_pool BOOLEAN NOT NULL DEFAULT FALSE,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS catch_species_spawnable_idx ON catch_species (rarity, habitat) "
        "WHERE enabled AND exclusive_to IS NULL"
    ),
    f"""
    CREATE TABLE IF NOT EXISTS catch_guild_config (
        guild_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        spawn_channel_ids BIGINT[] NOT NULL DEFAULT '{{}}',
        encounter_channel_ids BIGINT[] NOT NULL DEFAULT '{{}}',
        announce_channel_id BIGINT,
        rare_ping_role_id BIGINT,
        speed_preset TEXT NOT NULL DEFAULT 'normal',
        spawn_every_n_messages INTEGER NOT NULL DEFAULT 25 CHECK (spawn_every_n_messages > 0),
        min_seconds_between_spawns INTEGER NOT NULL DEFAULT 90 CHECK (min_seconds_between_spawns >= 0),
        despawn_seconds INTEGER NOT NULL DEFAULT 300 CHECK (despawn_seconds > 0),
        spawn_rate_multiplier REAL NOT NULL DEFAULT 1.0,
        encounter_cooldown_override INTEGER,
        join_dm_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, clone_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_spawns (
        id BIGSERIAL PRIMARY KEY,
        guild_id BIGINT,
        clone_id INTEGER,
        {CLONE_KEY},
        channel_id BIGINT,
        message_id BIGINT,
        species_id SMALLINT NOT NULL REFERENCES catch_species(id),
        level SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 100),
        shiny BOOLEAN NOT NULL DEFAULT FALSE,
        ivs SMALLINT[] NOT NULL,
        source TEXT NOT NULL DEFAULT 'chat' CHECK (source IN ('chat', 'encounter', 'fishing', 'test')),
        owner_user_id BIGINT,
        spawned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        caught_by BIGINT,
        caught_at TIMESTAMPTZ,
        fled BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    (
        "CREATE INDEX IF NOT EXISTS catch_spawns_live_idx ON catch_spawns (expires_at) "
        "WHERE caught_by IS NULL AND NOT fled"
    ),
    "CREATE INDEX IF NOT EXISTS catch_spawns_guild_idx ON catch_spawns (guild_id, clone_key, spawned_at DESC)",
    (
        "CREATE UNIQUE INDEX IF NOT EXISTS catch_spawns_message_key ON catch_spawns (message_id) "
        "WHERE message_id IS NOT NULL"
    ),
    f"""
    CREATE TABLE IF NOT EXISTS catch_owned (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        species_id SMALLINT NOT NULL REFERENCES catch_species(id),
        level SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 100),
        xp INTEGER NOT NULL DEFAULT 0 CHECK (xp >= 0),
        nickname TEXT CHECK (nickname IS NULL OR char_length(nickname) <= 24),
        shiny BOOLEAN NOT NULL DEFAULT FALSE,
        special BOOLEAN NOT NULL DEFAULT FALSE,
        ivs SMALLINT[] NOT NULL,
        favorite BOOLEAN NOT NULL DEFAULT FALSE,
        locked BOOLEAN NOT NULL DEFAULT FALSE,
        source SMALLINT NOT NULL,
        caught_in_guild BIGINT,
        spawn_id BIGINT,
        idem_key TEXT,
        caught_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS catch_owned_user_idx ON catch_owned (user_id, clone_key, caught_at DESC)",
    "CREATE INDEX IF NOT EXISTS catch_owned_user_species_idx ON catch_owned (user_id, clone_key, species_id)",
    # One creature per spawn and per idempotency key: the database itself
    # refuses a double grant even if application code is wrong.
    "CREATE UNIQUE INDEX IF NOT EXISTS catch_owned_spawn_key ON catch_owned (spawn_id) WHERE spawn_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS catch_owned_idem_key ON catch_owned (idem_key) WHERE idem_key IS NOT NULL",
    f"""
    CREATE TABLE IF NOT EXISTS catch_dex (
        user_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        species_id SMALLINT NOT NULL REFERENCES catch_species(id),
        seen BOOLEAN NOT NULL DEFAULT TRUE,
        caught_count INTEGER NOT NULL DEFAULT 0 CHECK (caught_count >= 0),
        shiny_caught INTEGER NOT NULL DEFAULT 0 CHECK (shiny_caught >= 0),
        first_caught_at TIMESTAMPTZ,
        PRIMARY KEY (user_id, clone_key, species_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_players (
        user_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        coins BIGINT NOT NULL DEFAULT 0 CHECK (coins >= 0),
        total_catches INTEGER NOT NULL DEFAULT 0 CHECK (total_catches >= 0),
        catch_streak INTEGER NOT NULL DEFAULT 0 CHECK (catch_streak >= 0),
        best_streak INTEGER NOT NULL DEFAULT 0 CHECK (best_streak >= 0),
        daily_streak INTEGER NOT NULL DEFAULT 0 CHECK (daily_streak >= 0),
        last_daily_at TIMESTAMPTZ,
        swap_tokens INTEGER NOT NULL DEFAULT 0 CHECK (swap_tokens >= 0),
        fishing_tokens INTEGER NOT NULL DEFAULT 0 CHECK (fishing_tokens >= 0),
        buddy_id BIGINT,
        settings JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        disclaimer_accepted_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, clone_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_inventory (
        user_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        item_key TEXT NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, clone_key, item_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_cooldowns (
        user_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        kind TEXT NOT NULL,
        ready_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (user_id, clone_key, kind)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_feature_flags (
        guild_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        feature TEXT NOT NULL,
        enabled BOOLEAN NOT NULL,
        reason TEXT,
        updated_by BIGINT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (guild_id, clone_key, feature)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_reminders (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        clone_id INTEGER,
        {CLONE_KEY},
        kind TEXT NOT NULL,
        due_at TIMESTAMPTZ NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'dm' CHECK (delivery IN ('dm', 'channel')),
        channel_id BIGINT,
        payload JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        dedupe_key TEXT NOT NULL,
        attempts SMALLINT NOT NULL DEFAULT 0,
        delivered BOOLEAN NOT NULL DEFAULT FALSE,
        delivered_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS catch_reminders_dedupe_key ON catch_reminders (user_id, clone_key, dedupe_key)",
    "CREATE INDEX IF NOT EXISTS catch_reminders_due_idx ON catch_reminders (due_at) WHERE NOT delivered",
    """
    CREATE TABLE IF NOT EXISTS catch_assets (
        id BIGSERIAL PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('creature', 'silhouette', 'thumb', 'item_icon',
            'rarity_frame', 'card_bg', 'egg', 'badge', 'trainer_icon', 'banner', 'emblem')),
        key TEXT NOT NULL,
        variant TEXT NOT NULL DEFAULT 'normal' CHECK (variant IN ('normal', 'shiny')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('placeholder', 'draft', 'live')),
        content_hash TEXT NOT NULL,
        mime TEXT NOT NULL,
        width INTEGER,
        height INTEGER,
        size_bytes INTEGER,
        vault_channel_id BIGINT,
        vault_message_id BIGINT,
        emoji_id BIGINT,
        source_note TEXT NOT NULL DEFAULT '',
        uploaded_by BIGINT,
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS catch_assets_version_key ON catch_assets (kind, key, variant, version)",
    "CREATE UNIQUE INDEX IF NOT EXISTS catch_assets_live_key ON catch_assets (kind, key, variant) WHERE status = 'live'",
    """
    CREATE TABLE IF NOT EXISTS catch_theme (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL CHECK (value ~ '^#[0-9A-F]{6}$'),
        updated_by BIGINT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS catch_stats_daily (
        day DATE NOT NULL,
        guild_id BIGINT NOT NULL DEFAULT 0,
        clone_id INTEGER,
        {CLONE_KEY},
        spawns INTEGER NOT NULL DEFAULT 0,
        encounters INTEGER NOT NULL DEFAULT 0,
        catches INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, guild_id, clone_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catch_audit (
        id BIGSERIAL PRIMARY KEY,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor_id BIGINT,
        user_id BIGINT,
        clone_id INTEGER,
        guild_id BIGINT,
        action TEXT NOT NULL,
        ref_id BIGINT,
        detail JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS catch_audit_at_idx ON catch_audit (at)",
    "CREATE INDEX IF NOT EXISTS catch_audit_user_idx ON catch_audit (user_id, at DESC) WHERE user_id IS NOT NULL",
)


async def create_tables(conn) -> None:
    for statement in STATEMENTS:
        await conn.execute(statement)
