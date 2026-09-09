-- ============================================================================
-- PRIME-BOT SERVER DIRECTORY FEATURE ENHANCEMENTS
-- Database Migration Script
-- Run this in Supabase SQL Editor or via Python migration runner
-- ============================================================================

-- ============================================================================
-- PHASE 1: Reviews & Ratings System
-- ============================================================================

-- Enhance server_listings with rating fields
ALTER TABLE server_listings ADD COLUMN IF NOT EXISTS avg_rating DECIMAL(2,1);
ALTER TABLE server_listings ADD COLUMN IF NOT EXISTS review_count INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_server_listings_avg_rating 
    ON server_listings(avg_rating DESC NULLS LAST);

-- Create reviews table
CREATE TABLE IF NOT EXISTS server_reviews (
    review_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    reviewer_user_id BIGINT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    review_text TEXT NOT NULL DEFAULT '',
    review_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    helpful_count INTEGER DEFAULT 0,
    is_verified_member BOOLEAN DEFAULT FALSE
);

-- Plain UNIQUE(guild_id, clone_id, reviewer_user_id) would NOT dedupe rows
-- where clone_id IS NULL (the main bot, not a clone) — Postgres treats
-- NULL <> NULL for uniqueness purposes, so two "same reviewer, same guild,
-- no clone" reviews wouldn't collide and ON CONFLICT wouldn't match them.
-- Match the COALESCE(clone_id, -1) pattern already used elsewhere in this
-- schema (see server_listings_guild_clone_key) via an expression index,
-- and target it explicitly from add_server_review()'s ON CONFLICT.
CREATE UNIQUE INDEX IF NOT EXISTS idx_server_reviews_unique
    ON server_reviews (guild_id, COALESCE(clone_id, -1), reviewer_user_id);

CREATE INDEX IF NOT EXISTS idx_server_reviews_guild 
    ON server_reviews(guild_id, clone_id);
    
CREATE INDEX IF NOT EXISTS idx_server_reviews_rating 
    ON server_reviews(rating DESC);
    
CREATE INDEX IF NOT EXISTS idx_server_reviews_date 
    ON server_reviews(review_date DESC);
    
CREATE INDEX IF NOT EXISTS idx_server_reviews_helpful 
    ON server_reviews(helpful_count DESC);


-- ============================================================================
-- PHASE 2: Verification Tier System
-- ============================================================================

-- Update server_listings with verification fields
ALTER TABLE server_listings ADD COLUMN IF NOT EXISTS verification_tier VARCHAR(50) DEFAULT 'unverified';
-- Tiers: unverified, auto_verified, manual_verified, gold_verified, platinum_verified

ALTER TABLE server_listings ADD COLUMN IF NOT EXISTS auto_verified_at TIMESTAMPTZ;
ALTER TABLE server_listings ADD COLUMN IF NOT EXISTS manual_verified_at TIMESTAMPTZ;
ALTER TABLE server_listings ADD COLUMN IF NOT EXISTS verified_by_admin BIGINT;

-- Create verification tier pricing & features table
CREATE TABLE IF NOT EXISTS verification_tier_config (
    tier_id SERIAL PRIMARY KEY,
    tier_name VARCHAR(50) NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    badge_emoji VARCHAR(50),
    badge_color VARCHAR(20),
    price_usd DECIMAL(10,2),
    duration_days INTEGER,
    listing_priority INTEGER DEFAULT 0,
    featured_on_homepage BOOLEAN DEFAULT FALSE,
    boost_multiplier DECIMAL(2,1) DEFAULT 1.0,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create verification purchases/subscriptions table
CREATE TABLE IF NOT EXISTS verification_purchases (
    purchase_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    tier_name VARCHAR(50) NOT NULL,
    payment_id VARCHAR(255),
    payment_method VARCHAR(50),
    status VARCHAR(50) DEFAULT 'active',
    purchased_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    auto_renew BOOLEAN DEFAULT FALSE,
    cancelled_at TIMESTAMPTZ,
    FOREIGN KEY (tier_name) REFERENCES verification_tier_config(tier_name)
);

CREATE INDEX IF NOT EXISTS idx_verification_purchases_guild 
    ON verification_purchases(guild_id, clone_id);
    
CREATE INDEX IF NOT EXISTS idx_verification_purchases_expires 
    ON verification_purchases(expires_at DESC);
    
CREATE INDEX IF NOT EXISTS idx_verification_purchases_status 
    ON verification_purchases(status);

-- Track verification qualifications for auto-verify logic
CREATE TABLE IF NOT EXISTS verification_qualifications (
    qualification_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    clicks_30d INTEGER DEFAULT 0,
    votes_total INTEGER DEFAULT 0,
    avg_rating DECIMAL(2,1),
    age_days INTEGER,
    qualifies_for_auto BOOLEAN DEFAULT FALSE,
    last_checked TIMESTAMPTZ DEFAULT NOW()
);

-- Same NULL-uniqueness issue as server_reviews above — expression index
-- instead of a plain column UNIQUE, so ON CONFLICT can target rows where
-- clone_id IS NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_verification_qualifications_unique
    ON verification_qualifications (guild_id, COALESCE(clone_id, -1));

CREATE INDEX IF NOT EXISTS idx_verification_qualifications_guild 
    ON verification_qualifications(guild_id, clone_id);


-- ============================================================================
-- PHASE 3: Analytics & Tracking
-- ============================================================================

-- Click/visit tracking
CREATE TABLE IF NOT EXISTS listing_click_log (
    click_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    ip_hash VARCHAR(64),
    referrer_source VARCHAR(255),
    ref_code_used VARCHAR(255),
    user_agent_hash VARCHAR(64),
    clicked_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_listing_clicks_guild 
    ON listing_click_log(guild_id, clone_id, clicked_at DESC);
    
CREATE INDEX IF NOT EXISTS idx_listing_clicks_refcode 
    ON listing_click_log(ref_code_used);
    
CREATE INDEX IF NOT EXISTS idx_listing_clicks_date 
    ON listing_click_log(clicked_at DESC);

-- Referral link performance tracking
CREATE TABLE IF NOT EXISTS listing_referral_stats (
    referral_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    ref_code VARCHAR(255) NOT NULL UNIQUE,
    source_platform VARCHAR(50) DEFAULT 'direct',  -- 'discord', 'twitter', 'reddit', 'direct', etc
    clicks INTEGER DEFAULT 0,
    conversions INTEGER DEFAULT 0,
    last_click_at TIMESTAMPTZ,
    last_conversion_at TIMESTAMPTZ,
    tracked_since TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_stats_guild 
    ON listing_referral_stats(guild_id, clone_id);
    
CREATE INDEX IF NOT EXISTS idx_referral_stats_code 
    ON listing_referral_stats(ref_code);
    
CREATE INDEX IF NOT EXISTS idx_referral_stats_conversions 
    ON listing_referral_stats(conversions DESC);

-- Daily analytics snapshot (for graphs)
CREATE TABLE IF NOT EXISTS listing_daily_analytics (
    analytics_id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    clone_id INTEGER,
    analytics_date DATE NOT NULL,
    clicks_count INTEGER DEFAULT 0,
    unique_ip_count INTEGER DEFAULT 0,
    votes_received INTEGER DEFAULT 0,
    new_reviews_count INTEGER DEFAULT 0,
    avg_rating DECIMAL(2,1),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Same NULL-uniqueness issue as above.
CREATE UNIQUE INDEX IF NOT EXISTS idx_listing_daily_analytics_unique
    ON listing_daily_analytics (guild_id, COALESCE(clone_id, -1), analytics_date);

CREATE INDEX IF NOT EXISTS idx_daily_analytics_guild 
    ON listing_daily_analytics(guild_id, clone_id, analytics_date DESC);
    
CREATE INDEX IF NOT EXISTS idx_daily_analytics_date 
    ON listing_daily_analytics(analytics_date DESC);


-- ============================================================================
-- PHASE 4: Category & Discovery Enhancements
-- ============================================================================

-- Track tag popularity and metadata
CREATE TABLE IF NOT EXISTS tag_metadata (
    tag_id SERIAL PRIMARY KEY,
    tag_name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    icon_emoji VARCHAR(50),
    server_count INTEGER DEFAULT 0,
    total_clicks INTEGER DEFAULT 0,
    trending_score DECIMAL(5,2) DEFAULT 0,
    featured BOOLEAN DEFAULT FALSE,
    featured_at TIMESTAMPTZ,
    last_updated TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tag_metadata_trending 
    ON tag_metadata(trending_score DESC);
    
CREATE INDEX IF NOT EXISTS idx_tag_metadata_featured 
    ON tag_metadata(featured DESC);

-- Browse history/analytics
CREATE TABLE IF NOT EXISTS category_browse_log (
    browse_id BIGSERIAL PRIMARY KEY,
    tag_name VARCHAR(100) NOT NULL,
    ip_hash VARCHAR(64),
    browsed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_category_browse_tag 
    ON category_browse_log(tag_name, browsed_at DESC);
    
CREATE INDEX IF NOT EXISTS idx_category_browse_date 
    ON category_browse_log(browsed_at DESC);


-- ============================================================================
-- PHASE 5: Views for Analytics & Dashboard Queries
-- ============================================================================

-- View: Server with current metrics
CREATE OR REPLACE VIEW server_listing_metrics AS
SELECT
    sl.guild_id,
    sl.clone_id,
    sl.guild_name,
    sl.invite_url,
    sl.verification_tier,
    sl.avg_rating,
    sl.review_count,
    sl.member_count,
    COALESCE(COUNT(DISTINCT lcl.click_id), 0) as total_clicks_30d,
    COALESCE(COUNT(DISTINCT sv.voter_id), 0) as total_votes,
    sl.updated_at,
    CASE 
        WHEN sl.verification_tier = 'platinum_verified' THEN 5
        WHEN sl.verification_tier = 'gold_verified' THEN 4
        WHEN sl.verification_tier = 'manual_verified' THEN 3
        WHEN sl.verification_tier = 'auto_verified' THEN 2
        ELSE 1
    END as verification_priority
FROM server_listings sl
LEFT JOIN listing_click_log lcl 
    ON sl.guild_id = lcl.guild_id 
    AND sl.clone_id = lcl.clone_id
    AND lcl.clicked_at > NOW() - INTERVAL '30 days'
LEFT JOIN server_listing_votes sv 
    ON sl.guild_id = sv.guild_id 
    AND sv.created_at > NOW() - INTERVAL '90 days'
GROUP BY sl.guild_id, sl.clone_id, sl.guild_name, sl.invite_url, 
         sl.verification_tier, sl.avg_rating, sl.review_count, 
         sl.member_count, sl.updated_at;

-- View: Top referral performers
CREATE OR REPLACE VIEW top_referral_performers AS
SELECT
    guild_id,
    clone_id,
    ref_code,
    clicks,
    conversions,
    ROUND((conversions::numeric / NULLIF(clicks, 0)) * 100, 2) as conversion_rate,
    last_conversion_at
FROM listing_referral_stats
WHERE clicks > 0
ORDER BY conversions DESC;

-- View: Category trending
CREATE OR REPLACE VIEW category_trending AS
SELECT
    tm.tag_name,
    tm.server_count,
    COALESCE(SUM(CASE WHEN cbl.browse_id IS NOT NULL THEN 1 ELSE 0 END), 0) as recent_views,
    ROUND(
        (COALESCE(SUM(CASE WHEN cbl.browsed_at > NOW() - INTERVAL '7 days' THEN 1 ELSE 0 END), 0)::numeric /
        NULLIF(COALESCE(SUM(CASE WHEN cbl.browsed_at > NOW() - INTERVAL '30 days' THEN 1 ELSE 0 END), 0), 0)) * 100,
        1
    ) as trending_percentage
FROM tag_metadata tm
LEFT JOIN category_browse_log cbl ON tm.tag_name = cbl.tag_name
GROUP BY tm.tag_name, tm.server_count, tm.trending_score
ORDER BY trending_percentage DESC NULLS LAST;


-- ============================================================================
-- Helpful stored procedures for common operations
-- ============================================================================

-- Update listing metrics (called after votes, clicks, reviews)
CREATE OR REPLACE FUNCTION update_listing_metrics(p_guild_id BIGINT, p_clone_id INTEGER)
RETURNS void AS $$
BEGIN
    -- Update average rating
    UPDATE server_listings
    SET avg_rating = (
        SELECT ROUND(AVG(rating)::numeric, 1)
        FROM server_reviews
        WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1)
    ),
    review_count = (
        SELECT COUNT(*)
        FROM server_reviews
        WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1)
    ),
    updated_at = NOW()
    WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1);
END;
$$ LANGUAGE plpgsql;

-- Check and update verification qualifications
CREATE OR REPLACE FUNCTION check_auto_verification(p_guild_id BIGINT, p_clone_id INTEGER)
RETURNS BOOLEAN AS $$
DECLARE
    v_clicks INT;
    v_votes INT;
    v_age INT;
    v_avg_rating DECIMAL(2,1);
    v_qualifies BOOLEAN;
BEGIN
    -- Get metrics
    SELECT COALESCE(COUNT(*), 0)
    INTO v_clicks
    FROM listing_click_log
    WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1)
    AND clicked_at > NOW() - INTERVAL '30 days';

    SELECT COALESCE(COUNT(*), 0)
    INTO v_votes
    FROM server_listing_votes
    WHERE guild_id = p_guild_id
    AND created_at > NOW() - INTERVAL '90 days';

    SELECT EXTRACT(DAY FROM (NOW() - sl.created_at))
    INTO v_age
    FROM server_listings sl
    WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1);

    SELECT avg_rating
    INTO v_avg_rating
    FROM server_listings
    WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1);

    -- Check thresholds: 50+ clicks, 25+ votes, 30+ days old
    v_qualifies := (v_clicks >= 50 AND v_votes >= 25 AND COALESCE(v_age, 0) >= 30 
                    AND (v_avg_rating IS NULL OR v_avg_rating >= 3.5));

    -- Update qualifications table
    INSERT INTO verification_qualifications
        (guild_id, clone_id, clicks_30d, votes_total, avg_rating, age_days, qualifies_for_auto, last_checked)
    VALUES (p_guild_id, p_clone_id, v_clicks, v_votes, v_avg_rating, COALESCE(v_age, 0), v_qualifies, NOW())
    ON CONFLICT (guild_id, (COALESCE(clone_id, -1)))
    DO UPDATE SET
        clicks_30d = v_clicks,
        votes_total = v_votes,
        avg_rating = v_avg_rating,
        age_days = COALESCE(v_age, 0),
        qualifies_for_auto = v_qualifies,
        last_checked = NOW();

    -- Auto-update verification_tier if qualifies and not manually verified
    IF v_qualifies THEN
        UPDATE server_listings
        SET verification_tier = 'auto_verified',
            auto_verified_at = NOW()
        WHERE guild_id = p_guild_id AND COALESCE(clone_id, -1) = COALESCE(p_clone_id, -1)
        AND verification_tier = 'unverified';
    END IF;

    RETURN v_qualifies;
END;
$$ LANGUAGE plpgsql;


-- Seed the tier catalog itself — verification_purchases.tier_name has an FK
-- to this table, so purchases/wiring above would fail on an empty table.
INSERT INTO verification_tier_config
    (tier_name, display_name, badge_emoji, badge_color, price_usd, duration_days,
     listing_priority, featured_on_homepage, boost_multiplier, description)
VALUES
    ('unverified', 'Unverified', NULL, NULL, 0, NULL, 0, FALSE, 1.0,
     'Default state — no badge, standard listing priority.'),
    ('auto_verified', 'Auto-Verified', '✅', '#22c55e', 0, NULL, 2, FALSE, 1.0,
     'Earned automatically once a listing clears the click/vote/age/rating thresholds — see check_auto_verification().'),
    ('manual_verified', 'Manual-Verified', '☑️', '#3b82f6', 0, NULL, 3, FALSE, 1.1,
     'Granted by an admin, independent of the automatic thresholds.'),
    ('gold_verified', 'Gold-Verified', '⭐', '#eab308', 4.99, 30, 4, TRUE, 1.25,
     'Paid tier: homepage featuring plus a modest listing boost.'),
    ('platinum_verified', 'Platinum-Verified', '💎', '#a855f7', 9.99, 30, 5, TRUE, 1.5,
     'Top paid tier: highest listing priority and homepage featuring.')
ON CONFLICT (tier_name) DO NOTHING;


-- ============================================================================
-- Migration Complete
-- ============================================================================
-- Next steps:
-- 1. Update database.py _create_tables() to run these statements
-- 2. Add new API endpoints in api/server_listings.py and new api/server_reviews.py
-- 3. Update frontend components with new review/rating UI
-- 4. Add analytics dashboard page
-- 5. Deploy and test
