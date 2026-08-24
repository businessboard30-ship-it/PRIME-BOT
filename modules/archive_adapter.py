"""
Bots Archive — automated submission review, listing, voting, and boost
system. Extends botstore_listings conceptually but is kept as its own set
of tables (archive_listings, archive_votes, archive_boosts,
archive_enabled_guilds, archive_disputes) rather than overloading
botstore_listings, since the review workflow (pending/flagged/approved/
denied + risk scoring + Discord-verified data) doesn't fit botstore's
simpler status field without breaking it for existing botstore users.

Design:
- Submission is ID-first: the submitter gives a Discord Application ID,
  we look it up against Discord's public, unauthenticated RPC endpoint
  (https://discord.com/api/v10/applications/{id}/rpc) to pull the real
  bot name/icon/description straight from Discord, rather than trusting
  free-typed text. This is the same endpoint Discord's own invite/RPC
  pages use and needs no bot token.
- Risk scoring is a deterministic heuristic (application age from the
  ID's embedded snowflake timestamp, submitter history, keyword scan)
  plus an optional AI text-classifier pass reusing modules/ai_features.py
  — kept optional so this module has no hard dependency on any specific
  AI provider being configured.
- Everything here is OFF by default: nothing posts/reviews/lists in a
  guild until that guild_id is in archive_enabled_guilds.
"""

import logging
import re
import secrets
import hashlib
import hmac
import json
import time
from typing import Optional, Dict, Any, List

import aiohttp

from database import get_pool
from modules.ai_features import GROQ_API_KEY

logger = logging.getLogger(__name__)

RESUBMIT_LIMIT = 3
CLASSIFIER_MODEL = "llama-3.1-8b-instant"  # small/cheap — this is a binary risk check, not a chat

DISCORD_EPOCH = 1420070400000  # ms, per Discord's snowflake spec

NSFW_KEYWORDS = {
    "nsfw", "porn", "hentai", "onlyfans", "18+", "xxx", "nude", "sex bot",
}
SCAM_PATTERNS = [
    r"free\s+nitro", r"claim\s+your", r"click\s+here", r"steam\s+gift",
    r"double\s+your", r"airdrop", r"giveaway.*claim", r"verify.*account.*now",
]

RISK_APPROVE_BELOW = 25
RISK_DENY_ABOVE = 70

CATEGORIES = ["Moderation", "Economy", "Leveling", "AI", "Anime", "Utility", "Music", "Other"]


async def _init_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_enabled_guilds (
                guild_id BIGINT PRIMARY KEY,
                listing_channel_ids BIGINT[] NOT NULL DEFAULT '{}',
                featured_channel_id BIGINT,
                enabled_by BIGINT NOT NULL,
                enabled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_listings (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                submitter_id BIGINT NOT NULL,
                application_id BIGINT NOT NULL,
                bot_name TEXT NOT NULL,
                bot_icon_url TEXT,
                description TEXT,
                category TEXT,
                tags TEXT[] DEFAULT '{}',
                invite_link TEXT,
                support_server TEXT,
                status TEXT NOT NULL DEFAULT 'pending_review',
                risk_score INTEGER DEFAULT 0,
                deny_reason TEXT,
                message_id BIGINT,
                channel_id BIGINT,
                resubmit_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP,
                UNIQUE (guild_id, application_id)
            )
        """)
        # ALTER TABLE ... ADD COLUMN IF NOT EXISTS patches databases that had
        # this table created before the `tags` column existed. CREATE TABLE
        # IF NOT EXISTS above is a no-op once the table already exists, so
        # this is required for any pre-existing deployment (like this one).
        await conn.execute("""
            ALTER TABLE archive_listings ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}'
        """)
        # Optional per-listing webhook: submitters can opt in to receiving
        # signed HTTP POSTs on status changes (approved/denied/flagged/
        # boosted). webhook_secret is auto-generated if not user-supplied,
        # and is what we HMAC-sign payloads with so the receiver can verify
        # a request genuinely came from this bot.
        await conn.execute("""
            ALTER TABLE archive_listings ADD COLUMN IF NOT EXISTS webhook_url TEXT
        """)
        await conn.execute("""
            ALTER TABLE archive_listings ADD COLUMN IF NOT EXISTS webhook_secret TEXT
        """)
        # Automation bookkeeping — last time this listing was auto re-scanned
        # (dead-bot sweep) / re-scored (risk re-check), and which vote
        # milestones have already been announced so the featured-channel
        # announcer doesn't repeat itself every loop tick.
        await conn.execute("""
            ALTER TABLE archive_listings ADD COLUMN IF NOT EXISTS last_rescan_at TIMESTAMP
        """)
        await conn.execute("""
            ALTER TABLE archive_listings ADD COLUMN IF NOT EXISTS last_rescore_at TIMESTAMP
        """)
        await conn.execute("""
            ALTER TABLE archive_listings ADD COLUMN IF NOT EXISTS milestone_votes_announced INTEGER NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_webhook_failures (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES archive_listings(id),
                event TEXT NOT NULL,
                payload JSONB NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Tracks the single auto-updating trending post per guild so the
        # hourly repost loop edits it in place instead of spamming a new
        # message every run.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_trending_pins (
                guild_id BIGINT PRIMARY KEY,
                channel_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_votes (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES archive_listings(id),
                voter_id BIGINT NOT NULL,
                voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_boosts (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES archive_listings(id),
                buyer_id BIGINT NOT NULL,
                tier TEXT NOT NULL,
                amount_usd NUMERIC(10,2) NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                payment_reference TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_disputes (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL REFERENCES archive_listings(id),
                submitter_id BIGINT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            ALTER TABLE archive_disputes ADD COLUMN IF NOT EXISTS last_reminded_at TIMESTAMP
        """)
        await conn.execute("""
            ALTER TABLE archive_boosts ADD COLUMN IF NOT EXISTS expiry_warned BOOLEAN NOT NULL DEFAULT FALSE
        """)


_initialized = False


async def ensure_ready():
    global _initialized
    if not _initialized:
        await _init_tables()
        _initialized = True


# ── Discord application lookup ──────────────────────────────────────────

def snowflake_age_days(snowflake_id: int) -> float:
    created_ms = (snowflake_id >> 22) + DISCORD_EPOCH
    return (time.time() * 1000 - created_ms) / 86_400_000


async def fetch_application(application_id: str) -> Optional[Dict[str, Any]]:
    """Looks up a bot application against Discord's public RPC endpoint.
    Needs no bot token — same endpoint Discord's own invite pages use.
    Returns None if the ID doesn't resolve to a real application."""
    if not application_id.isdigit():
        return None
    url = f"https://discord.com/api/v10/applications/{application_id}/rpc"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception as e:
        logger.error(f"[v0] archive: application lookup failed for {application_id}: {e}")
        return None

    icon = data.get("icon")
    icon_url = (
        f"https://cdn.discordapp.com/app-icons/{application_id}/{icon}.png"
        if icon else None
    )

    # Discord's RPC response includes these when the developer has set them:
    # - tags: up to 5 App Directory tags the dev picked in the Portal
    # - custom_install_url: an explicit invite link the dev configured instead
    #   of the standard OAuth2 flow (e.g. their own onboarding page)
    # - install_params: {scopes, permissions} from the Portal's "Default
    #   Install Settings" → Guild Install section — the same thing we set up
    #   manually in the URL Generator earlier. Only present if the dev has
    #   filled that in.
    tags = data.get("tags") or []

    custom_install_url = data.get("custom_install_url")
    install_params = data.get("install_params") or {}
    if custom_install_url:
        invite_link = custom_install_url
    elif install_params.get("scopes"):
        scopes = "+".join(install_params["scopes"])
        permissions = install_params.get("permissions", "0")
        invite_link = (
            f"https://discord.com/oauth2/authorize?client_id={application_id}"
            f"&permissions={permissions}&scope={scopes}"
        )
    else:
        # Fallback: a generic invite requesting bot + applications.commands
        # with no preset permissions. Works for any public bot, but the
        # server admin adding it will need to grant permissions manually
        # since we have no way to know what the bot actually needs.
        invite_link = (
            f"https://discord.com/oauth2/authorize?client_id={application_id}"
            f"&permissions=0&scope=bot+applications.commands"
        )

    return {
        "id": int(application_id),
        "name": data.get("name") or "Unknown Bot",
        "icon_url": icon_url,
        "description": data.get("description") or "",
        "tags": tags,
        "invite_link": invite_link,
        "age_days": snowflake_age_days(int(application_id)),
    }


# ── AI classifier (optional — degrades gracefully if GROQ_API_KEY unset) ──

async def _ai_scam_score(description: str) -> int:
    """Returns 0-40 (added to the heuristic score), or 0 if the AI call
    fails/isn't configured — this is a bonus signal, never a hard
    dependency, so archive submission still works with no AI provider set
    up. Deliberately isolated from modules.ai_features.ai_chat: that
    function writes to the submitter's own AI conversation history, which
    would incorrectly mix an internal risk check into their personal
    /ai chat log."""
    if not GROQ_API_KEY or not description.strip():
        return 0
    prompt = (
        "You are a fraud/scam classifier for a Discord bot listing site. "
        "Reply with ONLY a single integer 0-100 (no words), where 100 means "
        "this description is almost certainly a scam, phishing, or fraud "
        "attempt, and 0 means it reads like a normal, legitimate bot "
        f"description.\n\nDescription:\n{description[:800]}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "model": CLASSIFIER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_completion_tokens": 5,
            }
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return 0
                data = await resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                match = re.search(r"\d+", text)
                if not match:
                    return 0
                raw = min(100, max(0, int(match.group())))
                return round(raw * 0.4)  # scaled to contribute at most 40 pts to the total score
    except Exception as e:
        logger.error(f"[v0] archive: AI classifier call failed: {e}")
        return 0


# ── Risk scoring ─────────────────────────────────────────────────────────

def contains_nsfw(app_data: Dict[str, Any], description: str) -> bool:
    """Cheap keyword check — used by the caller to force flagged_for_review
    regardless of the numeric risk score, so sexual content always gets a
    human look instead of being auto-approved OR auto-denied outright."""
    text = f"{app_data.get('description', '')} {description}".lower()
    return any(kw in text for kw in NSFW_KEYWORDS)


async def score_submission(app_data: Dict[str, Any], description: str, submitter_id: int) -> int:
    """0-100, higher = riskier. Deterministic heuristics + an optional AI
    scam-language pass (skipped silently if no GROQ_API_KEY is set).
    NSFW is handled separately by contains_nsfw() — not folded into this
    score — so the caller can flag-for-review on NSFW independent of
    whatever this score comes out to."""
    score = 0
    text = f"{app_data.get('description', '')} {description}".lower()

    if any(re.search(p, text) for p in SCAM_PATTERNS):
        score += 45

    score += await _ai_scam_score(description)

    age_days = app_data.get("age_days", 999)
    if age_days < 1:
        score += 30
    elif age_days < 7:
        score += 15

    pool = await get_pool()
    async with pool.acquire() as conn:
        prior = await conn.fetchval(
            "SELECT COUNT(*) FROM archive_listings WHERE submitter_id = $1 AND status = 'approved'",
            submitter_id,
        )
    if not prior:
        score += 10

    return min(score, 100)


# ── Listings CRUD ────────────────────────────────────────────────────────

async def get_resubmit_count(guild_id: int, application_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT resubmit_count FROM archive_listings WHERE guild_id=$1 AND application_id=$2",
            guild_id, application_id,
        )
        return val or 0


def generate_webhook_secret() -> str:
    return secrets.token_hex(32)


async def create_listing(guild_id: int, submitter_id: int, app_data: Dict, description: str,
                          category: str, invite_link: str, support_server: str, risk_score: int,
                          status: str, webhook_url: str = None, webhook_secret: str = None) -> int:
    # Auto-generate a signing key the moment a webhook URL is set, whether
    # the caller supplied their own key or not — a webhook with no secret
    # can't be verified by the receiver, so we never leave one unset.
    if webhook_url and not webhook_secret:
        webhook_secret = generate_webhook_secret()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO archive_listings
                (guild_id, submitter_id, application_id, bot_name, bot_icon_url, description,
                 category, tags, invite_link, support_server, status, risk_score, reviewed_at,
                 webhook_url, webhook_secret)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                    CASE WHEN $11 IN ('approved','denied') THEN CURRENT_TIMESTAMP ELSE NULL END,
                    $13,$14)
            ON CONFLICT (guild_id, application_id) DO UPDATE SET
                description = EXCLUDED.description, category = EXCLUDED.category,
                tags = EXCLUDED.tags,
                invite_link = EXCLUDED.invite_link, support_server = EXCLUDED.support_server,
                status = EXCLUDED.status, risk_score = EXCLUDED.risk_score,
                resubmit_count = archive_listings.resubmit_count + 1,
                webhook_url = COALESCE(EXCLUDED.webhook_url, archive_listings.webhook_url),
                webhook_secret = COALESCE(EXCLUDED.webhook_secret, archive_listings.webhook_secret)
            RETURNING id
            """,
            guild_id, submitter_id, app_data["id"], app_data["name"], app_data.get("icon_url"),
            description, category, app_data.get("tags", []), invite_link, support_server, status, risk_score,
            webhook_url, webhook_secret,
        )


async def set_webhook(listing_id: int, submitter_id: int, webhook_url: Optional[str],
                       webhook_secret: Optional[str] = None) -> bool:
    """Lets a submitter add/edit/clear their own listing's webhook + key.
    Returns False if the listing doesn't exist or isn't theirs."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        owner = await conn.fetchval("SELECT submitter_id FROM archive_listings WHERE id=$1", listing_id)
        if owner != submitter_id:
            return False
        if webhook_url and not webhook_secret:
            existing = await conn.fetchval("SELECT webhook_secret FROM archive_listings WHERE id=$1", listing_id)
            webhook_secret = existing or generate_webhook_secret()
        if not webhook_url:
            webhook_secret = None  # clearing the URL clears the key too — nothing left to sign for
        await conn.execute(
            "UPDATE archive_listings SET webhook_url=$2, webhook_secret=$3 WHERE id=$1",
            listing_id, webhook_url, webhook_secret,
        )
        return True


async def send_webhook(listing_id: int, event: str, extra: Dict[str, Any] = None):
    """Best-effort, fire-and-forget style notification. Never raises —
    a broken/slow receiver on the submitter's end must never block or fail
    the Discord-facing flow (card posting, interaction responses, etc.).
    On failure, queues into archive_webhook_failures for the retry loop in
    archive_automation.py instead of just giving up silently."""
    listing = await get_listing(listing_id)
    if not listing or not listing.get("webhook_url"):
        return
    payload = {
        "event": event,
        "listing_id": listing_id,
        "application_id": str(listing["application_id"]),
        "bot_name": listing["bot_name"],
        "status": listing["status"],
        "timestamp": int(time.time()),
        **(extra or {}),
    }
    ok = await _post_webhook(listing["webhook_url"], listing.get("webhook_secret") or "", payload)
    if not ok:
        await queue_webhook_failure(listing_id, event, payload)


async def _post_webhook(url: str, secret: str, payload: Dict[str, Any]) -> bool:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, data=body,
                headers={"Content-Type": "application/json", "X-Archive-Signature": signature},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 400:
                    logger.warning(f"[v0] archive webhook to {url} returned {resp.status}")
                    return False
                return True
    except Exception as e:
        logger.warning(f"[v0] archive webhook to {url} failed: {e}")
        return False


async def get_listing(listing_id: int) -> Optional[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM archive_listings WHERE id = $1", listing_id)
        return dict(row) if row else None


async def set_status(listing_id: int, status: str, deny_reason: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE archive_listings SET status=$2, deny_reason=$3, reviewed_at=CURRENT_TIMESTAMP WHERE id=$1",
            listing_id, status, deny_reason,
        )


async def set_message_ref(listing_id: int, channel_id: int, message_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE archive_listings SET channel_id=$2, message_id=$3 WHERE id=$1",
            listing_id, channel_id, message_id,
        )


async def pending_review_queue(guild_id: int) -> List[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM archive_listings WHERE guild_id=$1 AND status='flagged_for_review' ORDER BY created_at",
            guild_id,
        )
        return [dict(r) for r in rows]


# ── Guild config ─────────────────────────────────────────────────────────

async def is_enabled(guild_id: int) -> Optional[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM archive_enabled_guilds WHERE guild_id=$1", guild_id)
        return dict(row) if row else None


async def enable_guild(guild_id: int, channel_ids: List[int], featured_channel_id: Optional[int], enabled_by: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO archive_enabled_guilds (guild_id, listing_channel_ids, featured_channel_id, enabled_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (guild_id) DO UPDATE SET
                listing_channel_ids=EXCLUDED.listing_channel_ids,
                featured_channel_id=EXCLUDED.featured_channel_id
            """,
            guild_id, channel_ids, featured_channel_id, enabled_by,
        )


# ── Voting ───────────────────────────────────────────────────────────────

VOTE_COOLDOWN_SECONDS = 50 * 60


async def try_vote(listing_id: int, voter_id: int) -> bool:
    """Returns True if the vote was recorded, False if still on cooldown."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        elapsed = await conn.fetchval(
            "SELECT NOW() - voted_at FROM archive_votes WHERE listing_id=$1 AND voter_id=$2 ORDER BY voted_at DESC LIMIT 1",
            listing_id, voter_id,
        )
        if elapsed is not None:
            if elapsed.total_seconds() < VOTE_COOLDOWN_SECONDS:
                return False
        await conn.execute(
            "INSERT INTO archive_votes (listing_id, voter_id) VALUES ($1,$2)", listing_id, voter_id
        )
        return True


async def vote_count(listing_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM archive_votes WHERE listing_id=$1", listing_id)


async def debug_votes_for_listing(listing_id: int) -> List[Dict]:
    """Raw dump of vote rows for a listing — diagnostic only."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT voter_id, voted_at FROM archive_votes WHERE listing_id=$1 ORDER BY voted_at DESC LIMIT 10",
            listing_id,
        )
        return [dict(r) for r in rows]


async def trending(guild_id: int, limit: int = 10) -> List[Dict]:
    """Recency-weighted votes, with active boosts pinned to the top."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.*,
                   COALESCE(SUM(GREATEST(0, 1 - EXTRACT(EPOCH FROM (NOW()-v.voted_at))/1209600.0)), 0) AS score,
                   EXISTS (SELECT 1 FROM archive_boosts b WHERE b.listing_id = l.id AND b.expires_at > NOW()) AS boosted
            FROM archive_listings l
            LEFT JOIN archive_votes v ON v.listing_id = l.id
            WHERE l.guild_id=$1 AND l.status='approved'
            GROUP BY l.id
            ORDER BY boosted DESC, score DESC
            LIMIT $2
            """,
            guild_id, limit,
        )
        return [dict(r) for r in rows]


# ── Boosts ───────────────────────────────────────────────────────────────

BOOST_TIERS = {
    "bronze": {"usd": 1, "hours": 24},
    "silver": {"usd": 3, "hours": 72},
    "gold": {"usd": 5, "hours": 168},
}


async def record_boost(listing_id: int, buyer_id: int, tier: str, expires_at, payment_reference: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO archive_boosts (listing_id, buyer_id, tier, amount_usd, expires_at, payment_reference)
            VALUES ($1,$2,$3,$4,$5,$6)
            """,
            listing_id, buyer_id, tier, BOOST_TIERS[tier]["usd"], expires_at, payment_reference,
        )


async def active_boost(listing_id: int) -> Optional[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM archive_boosts WHERE listing_id=$1 AND expires_at > NOW() ORDER BY expires_at DESC LIMIT 1",
            listing_id,
        )
        return dict(row) if row else None


# ── Disputes ─────────────────────────────────────────────────────────────

async def suggest_category(description: str, current_category: str) -> Optional[str]:
    """Best-effort AI check for whether the submitter's chosen category
    looks like a mismatch. Returns the suggested category only if it
    disagrees with current_category, or None (no opinion / not configured /
    call failed) — this must never block or fail a listing on its own."""
    if not GROQ_API_KEY or not description.strip():
        return None
    prompt = (
        f"Categories: {', '.join(CATEGORIES)}. A Discord bot was listed under "
        f"category '{current_category}' with this description:\n{description[:500]}\n\n"
        "Reply with ONLY the single best-fitting category name from the list above, nothing else."
    )
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "model": CLASSIFIER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_completion_tokens": 10,
            }
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                suggested = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[v0] archive suggest_category failed: {e}")
        return None
    if suggested in CATEGORIES and suggested != current_category:
        return suggested
    return None


async def create_dispute(listing_id: int, submitter_id: int, message: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO archive_disputes (listing_id, submitter_id, message) VALUES ($1,$2,$3) RETURNING id",
            listing_id, submitter_id, message,
        )


# ── Automation (see discord_bot/cogs/archive_automation.py) ───────────────

async def expire_stale_reviews(older_than_days: int) -> List[Dict]:
    """Auto-denies pending/flagged listings that have sat untouched past the
    cutoff. Returns the rows that were denied so the caller can notify."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE archive_listings SET status='denied',
                deny_reason='Auto-denied: no owner decision within the review window.',
                reviewed_at=CURRENT_TIMESTAMP
            WHERE status IN ('pending_review','flagged_for_review')
              AND created_at < NOW() - ($1 || ' days')::interval
            RETURNING *
            """,
            str(older_than_days),
        )
        return [dict(r) for r in rows]


async def approved_listings_for_rescan(stale_hours: int, limit: int = 25) -> List[Dict]:
    """Approved listings whose liveness/risk hasn't been rechecked recently."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM archive_listings
            WHERE status='approved'
              AND (last_rescan_at IS NULL OR last_rescan_at < NOW() - ($1 || ' hours')::interval)
            ORDER BY last_rescan_at NULLS FIRST
            LIMIT $2
            """,
            str(stale_hours), limit,
        )
        return [dict(r) for r in rows]


async def mark_rescanned(listing_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE archive_listings SET last_rescan_at=CURRENT_TIMESTAMP WHERE id=$1", listing_id)


async def sync_bot_details(listing_id: int, name: str = None, icon_url: str = None) -> Dict[str, bool]:
    """Called by the periodic recheck when a listing's bot has renamed
    itself and/or finally set an icon on Discord since it was submitted.
    Only writes fields that actually changed, and reports which changed
    so the caller knows whether the posted card needs refreshing."""
    pool = await get_pool()
    changed = {"name": False, "icon": False}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT bot_name, bot_icon_url FROM archive_listings WHERE id=$1", listing_id
        )
        if not row:
            return changed
        sets, args = [], []
        if name and name != row["bot_name"]:
            sets.append(f"bot_name=${len(args) + 2}")
            args.append(name)
            changed["name"] = True
        if icon_url and icon_url != row["bot_icon_url"]:
            sets.append(f"bot_icon_url=${len(args) + 2}")
            args.append(icon_url)
            changed["icon"] = True
        if sets:
            await conn.execute(
                f"UPDATE archive_listings SET {', '.join(sets)} WHERE id=$1", listing_id, *args
            )
    return changed


async def update_icon(listing_id: int, icon_url: str):
    """Kept for any existing caller expecting icon-only updates. Prefer
    sync_bot_details, which also picks up name changes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE archive_listings SET bot_icon_url=$2 WHERE id=$1", listing_id, icon_url
        )


async def mark_rescored(listing_id: int, new_risk: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE archive_listings SET risk_score=$2, last_rescore_at=CURRENT_TIMESTAMP WHERE id=$1",
            listing_id, new_risk,
        )


async def boosts_expiring_soon(within_hours: int = 24) -> List[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.*, l.bot_name FROM archive_boosts b
            JOIN archive_listings l ON l.id = b.listing_id
            WHERE b.expires_at > NOW() AND b.expires_at < NOW() + ($1 || ' hours')::interval
              AND b.expiry_warned = FALSE
            """,
            str(within_hours),
        )
        return [dict(r) for r in rows]


async def mark_boost_warned(boost_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE archive_boosts SET expiry_warned=TRUE WHERE id=$1", boost_id)


async def just_expired_boosted_listing_ids() -> List[int]:
    """Listings whose most recent boost expired since we last checked (used
    to know which cards need a re-render out of boosted styling)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT listing_id FROM archive_boosts
            WHERE expires_at <= NOW() AND expires_at > NOW() - interval '15 minutes'
            """
        )
        return [r["listing_id"] for r in rows]


async def stale_open_disputes(older_than_hours: int) -> List[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.*, l.bot_name FROM archive_disputes d
            JOIN archive_listings l ON l.id = d.listing_id
            WHERE d.status='open'
              AND d.created_at < NOW() - ($1 || ' hours')::interval
              AND (d.last_reminded_at IS NULL OR d.last_reminded_at < NOW() - ($1 || ' hours')::interval)
            """,
            str(older_than_hours),
        )
        return [dict(r) for r in rows]


async def mark_dispute_reminded(dispute_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE archive_disputes SET last_reminded_at=CURRENT_TIMESTAMP WHERE id=$1", dispute_id)


async def queue_webhook_failure(listing_id: int, event: str, payload: Dict[str, Any]):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO archive_webhook_failures (listing_id, event, payload) VALUES ($1,$2,$3)",
            listing_id, event, json.dumps(payload),
        )


async def due_webhook_retries(limit: int = 25) -> List[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM archive_webhook_failures WHERE next_attempt_at <= NOW() AND attempts < 5 ORDER BY next_attempt_at LIMIT $1",
            limit,
        )
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("payload"), str):
                d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out


async def bump_webhook_retry(failure_id: int, backoff_minutes: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE archive_webhook_failures SET attempts=attempts+1, next_attempt_at=NOW() + ($2 || ' minutes')::interval WHERE id=$1",
            failure_id, str(backoff_minutes),
        )


async def clear_webhook_failure(failure_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM archive_webhook_failures WHERE id=$1", failure_id)


async def check_vote_milestone(listing_id: int, votes: int, thresholds: List[int]) -> Optional[int]:
    """Returns the milestone just crossed (if any) and records it, so the
    caller announces each threshold exactly once."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        already = await conn.fetchval(
            "SELECT milestone_votes_announced FROM archive_listings WHERE id=$1", listing_id
        ) or 0
        crossed = [t for t in thresholds if votes >= t > already]
        if not crossed:
            return None
        newest = max(crossed)
        await conn.execute(
            "UPDATE archive_listings SET milestone_votes_announced=$2 WHERE id=$1", listing_id, newest
        )
        return newest


async def all_guild_ids_with_archive_enabled() -> List[int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT guild_id FROM archive_enabled_guilds")
        return [r["guild_id"] for r in rows]


async def get_trending_pin(guild_id: int) -> Optional[Dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM archive_trending_pins WHERE guild_id=$1", guild_id)
        return dict(row) if row else None


async def set_trending_pin(guild_id: int, channel_id: int, message_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO archive_trending_pins (guild_id, channel_id, message_id, updated_at)
            VALUES ($1,$2,$3,CURRENT_TIMESTAMP)
            ON CONFLICT (guild_id) DO UPDATE SET
                channel_id=EXCLUDED.channel_id, message_id=EXCLUDED.message_id, updated_at=CURRENT_TIMESTAMP
            """,
            guild_id, channel_id, message_id,
        )
