-- ============================================================================
-- ANIME BOT DATABASE SCHEMA - SUPABASE MIGRATION
-- ============================================================================
-- Run this entire script in Supabase SQL Editor
-- ============================================================================

-- ============================================================================
-- 1. USERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username VARCHAR(255) UNIQUE,
    first_name VARCHAR(255),
    joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    tier VARCHAR(50) DEFAULT 'free',
    submissions_count INTEGER DEFAULT 0,
    is_admin BOOLEAN DEFAULT FALSE,
    subscription_status VARCHAR(50) DEFAULT 'inactive',
    subscription_expiry TIMESTAMP NULL,
    stripe_key VARCHAR(255) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_is_admin ON users(is_admin);
CREATE INDEX idx_users_subscription_status ON users(subscription_status);

-- ============================================================================
-- 2. ANIME ENTRIES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS anime_entries (
    anime_id SERIAL PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    episodes INTEGER,
    genres VARCHAR(500),
    rating DECIMAL(3,1),
    description TEXT,
    image_url VARCHAR(500),
    source VARCHAR(50),
    external_id VARCHAR(100),
    status VARCHAR(50) DEFAULT 'ongoing',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_anime_title ON anime_entries(title);
CREATE INDEX idx_anime_status ON anime_entries(status);
CREATE INDEX idx_anime_source ON anime_entries(source);

-- ============================================================================
-- 3. USER SUBMISSIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS submissions (
    submission_id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    anime_title VARCHAR(255) NOT NULL,
    anime_url VARCHAR(500),
    description TEXT,
    status VARCHAR(50) DEFAULT 'pending',
    submitted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_date TIMESTAMP NULL,
    reviewed_by BIGINT REFERENCES users(user_id),
    rejection_reason TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_submissions_user_id ON submissions(user_id);
CREATE INDEX idx_submissions_status ON submissions(status);
CREATE INDEX idx_submissions_submitted_date ON submissions(submitted_date DESC);

-- ============================================================================
-- 4. CLONED BOTS TABLE
-- ============================================================================
-- Kept in sync with database.py's _create_tables()/migration ALTERs, the
-- actual runtime source of truth (tables are created by the app itself on
-- cold start; this file is a reference copy for manual provisioning).
CREATE TABLE IF NOT EXISTS cloned_bots (
    clone_id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    bot_name TEXT NOT NULL,
    bot_token TEXT NOT NULL UNIQUE,
    bot_username TEXT UNIQUE,
    webhook_url TEXT,
    webhook_secret TEXT,
    custom_data TEXT,
    status TEXT DEFAULT 'active',
    payment_id TEXT,
    payment_status TEXT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_cloned_bots_owner ON cloned_bots(owner_id);
CREATE INDEX idx_cloned_bots_status ON cloned_bots(status);
CREATE INDEX idx_cloned_bots_bot_token ON cloned_bots(bot_token);

-- ============================================================================
-- 5. PAYMENT LOGS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS payment_logs (
    payment_id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    amount DECIMAL(10,2) NOT NULL,
    status VARCHAR(50) DEFAULT 'pending',
    payment_method VARCHAR(50),
    paystack_reference VARCHAR(255) UNIQUE,
    payment_type VARCHAR(50),
    chat_id BIGINT,
    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_date TIMESTAMP NULL
);

CREATE INDEX idx_payment_logs_user_id ON payment_logs(user_id);
CREATE INDEX idx_payment_logs_status ON payment_logs(status);
CREATE INDEX idx_payment_logs_created_date ON payment_logs(created_date DESC);
CREATE INDEX idx_payment_logs_paystack_reference ON payment_logs(paystack_reference);

-- ============================================================================
-- 6. COMMISSION TRACKING TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS commission_tracking (
    commission_id SERIAL PRIMARY KEY,
    cloned_bot_id INTEGER NOT NULL REFERENCES cloned_bots(clone_id) ON DELETE CASCADE,
    payment_amount DECIMAL(10,2) NOT NULL,
    main_commission DECIMAL(10,2) NOT NULL,
    owner_amount DECIMAL(10,2) NOT NULL,
    stripe_key_id VARCHAR(255),
    payment_intent_id VARCHAR(255) UNIQUE,
    status VARCHAR(50) DEFAULT 'completed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    settled_at TIMESTAMP NULL
);

CREATE INDEX idx_commission_cloned_bot_id ON commission_tracking(cloned_bot_id);
CREATE INDEX idx_commission_status ON commission_tracking(status);
CREATE INDEX idx_commission_created_at ON commission_tracking(created_at DESC);

-- ============================================================================
-- 7. SUBSCRIPTION PAYMENTS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS subscription_payments (
    subscription_id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    payment_amount DECIMAL(10,2) NOT NULL,
    subscription_month VARCHAR(20) NOT NULL,
    payment_method VARCHAR(50),
    payment_reference VARCHAR(255) UNIQUE,
    status VARCHAR(50) DEFAULT 'completed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_subscription_user_id ON subscription_payments(user_id);
CREATE INDEX idx_subscription_status ON subscription_payments(status);
CREATE INDEX idx_subscription_month ON subscription_payments(subscription_month);
CREATE INDEX idx_subscription_created_at ON subscription_payments(created_at DESC);

-- ============================================================================
-- 8. AI USAGE TRACKING TABLES
-- ============================================================================
-- NOTE: the actual tables queried by modules/ai_features.py are
-- ai_chat_usage and ai_image_usage, auto-created by database.py on cold
-- start (see "NEW FEATURES TABLES" section there). Mirrored here for
-- anyone provisioning by hand instead of relying on auto-create.
CREATE TABLE IF NOT EXISTS ai_chat_usage (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    prompt TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_image_usage (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    prompt TEXT,
    image_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ai_chat_usage_user_id ON ai_chat_usage(user_id);
CREATE INDEX idx_ai_chat_usage_created_at ON ai_chat_usage(created_at DESC);
CREATE INDEX idx_ai_image_usage_user_id ON ai_image_usage(user_id);
CREATE INDEX idx_ai_image_usage_created_at ON ai_image_usage(created_at DESC);

-- ============================================================================
-- 9. BOT ANALYTICS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS bot_analytics (
    analytics_id SERIAL PRIMARY KEY,
    cloned_bot_id INTEGER NOT NULL REFERENCES cloned_bots(clone_id) ON DELETE CASCADE,
    total_users INTEGER DEFAULT 0,
    active_users INTEGER DEFAULT 0,
    total_commands INTEGER DEFAULT 0,
    feature_usage TEXT,
    recorded_date DATE DEFAULT CURRENT_DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_analytics_cloned_bot_id ON bot_analytics(cloned_bot_id);
CREATE INDEX idx_analytics_recorded_date ON bot_analytics(recorded_date DESC);

-- ============================================================================
-- 10. ADMIN LOGS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS admin_logs (
    log_id SERIAL PRIMARY KEY,
    admin_user_id BIGINT NOT NULL REFERENCES users(user_id),
    action VARCHAR(255),
    target_user_id BIGINT REFERENCES users(user_id),
    target_submission_id INTEGER REFERENCES submissions(submission_id),
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_admin_logs_admin_user_id ON admin_logs(admin_user_id);
CREATE INDEX idx_admin_logs_action ON admin_logs(action);
CREATE INDEX idx_admin_logs_created_at ON admin_logs(created_at DESC);

-- ============================================================================
-- CONSTRAINTS
-- ============================================================================
ALTER TABLE payment_logs ADD CONSTRAINT check_payment_amount 
    CHECK (amount > 0);

ALTER TABLE commission_tracking ADD CONSTRAINT check_commission_amount 
    CHECK (payment_amount > 0 AND main_commission > 0 AND owner_amount > 0);

ALTER TABLE subscription_payments ADD CONSTRAINT check_subscription_amount 
    CHECK (payment_amount > 0);

-- ============================================================================
-- VIEWS FOR ANALYTICS
-- ============================================================================
CREATE OR REPLACE VIEW active_subscribers AS
SELECT 
    u.user_id,
    u.username,
    u.subscription_status,
    u.subscription_expiry,
    COUNT(sp.subscription_id) as total_subscriptions
FROM users u
LEFT JOIN subscription_payments sp ON u.user_id = sp.user_id AND sp.status = 'completed'
WHERE u.subscription_status = 'active' AND u.subscription_expiry > CURRENT_TIMESTAMP
GROUP BY u.user_id, u.username, u.subscription_status, u.subscription_expiry;

CREATE OR REPLACE VIEW monthly_revenue AS
SELECT 
    DATE_TRUNC('month', created_date)::DATE as month,
    COUNT(*) as transaction_count,
    SUM(amount) as total_amount,
    payment_method,
    payment_type
FROM payment_logs
WHERE status = 'completed'
GROUP BY DATE_TRUNC('month', created_date), payment_method, payment_type
ORDER BY month DESC;

CREATE OR REPLACE VIEW top_cloned_bots_revenue AS
SELECT 
    cb.clone_id,
    cb.bot_name,
    cb.owner_id,
    u.username as owner_username,
    COUNT(ct.commission_id) as total_transactions,
    SUM(ct.payment_amount) as total_revenue,
    SUM(ct.owner_amount) as owner_earned,
    SUM(ct.main_commission) as main_commission
FROM cloned_bots cb
LEFT JOIN commission_tracking ct ON cb.clone_id = ct.cloned_bot_id AND ct.status = 'completed'
LEFT JOIN users u ON cb.owner_id = u.user_id
GROUP BY cb.clone_id, cb.bot_name, cb.owner_id, u.username
ORDER BY total_revenue DESC NULLS LAST;

-- ============================================================================
-- MIGRATION COMPLETE
-- ============================================================================
