import logging
from datetime import datetime, timedelta

from config import RATE_LIMIT_SEARCHES, RATE_LIMIT_SUBMISSIONS, DATABASE_URL
import asyncpg

logger = logging.getLogger(__name__)


class PostgresRateLimiter:
    """
    Postgres-backed rate limiter for serverless deployments (Issue 1.3).
    
    Survives serverless cold starts and concurrent instances by storing
    counters in a shared database instead of process-local memory.
    """
    
    def __init__(self):
        self.pool = None
    
    async def _get_pool(self):
        """Lazy-initialize connection pool."""
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=1,
                statement_cache_size=0
            )
        return self.pool
    
    async def _ensure_table(self):
        """Create rate_limits table if it doesn't exist."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS rate_limits (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, action, timestamp)
                )
            """)
            # Index for cleanup queries
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rate_limits_user_action
                ON rate_limits(user_id, action, timestamp)
            """)
    
    async def check_limit(
        self,
        user_id: int,
        action: str,
        max_count: int,
        window_hours: int
    ) -> bool:
        """
        Check if user has exceeded rate limit for action.
        
        Args:
            user_id: Telegram user ID
            action: action type (e.g., 'search', 'submit', 'ai_request')
            max_count: max allowed actions in window
            window_hours: time window in hours
            
        Returns:
            True if action allowed, False if limit exceeded
        """
        await self._ensure_table()
        pool = await self._get_pool()
        
        now = datetime.utcnow()
        window_start = now - timedelta(hours=window_hours)
        
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Count actions in window
                count = await conn.fetchval("""
                    SELECT COUNT(*) 
                    FROM rate_limits 
                    WHERE user_id = $1 
                    AND action = $2 
                    AND timestamp > $3
                """, user_id, action, window_start)
                
                if count >= max_count:
                    logger.warning(
                        f"[v0] Rate limit exceeded: user {user_id}, "
                        f"action {action}, count {count}/{max_count}"
                    )
                    return False
                
                # Record this action
                try:
                    await conn.execute("""
                        INSERT INTO rate_limits (user_id, action, timestamp)
                        VALUES ($1, $2, $3)
                    """, user_id, action, now)
                except asyncpg.UniqueViolationError:
                    # Same user/action/timestamp — shouldn't happen but is safe to ignore
                    pass
                
                return True
    
    async def check_search_limit(self, user_id: int) -> bool:
        """Check if user exceeded search limit (10/hour by default)."""
        return await self.check_limit(
            user_id,
            action="search",
            max_count=RATE_LIMIT_SEARCHES,
            window_hours=1
        )
    
    async def check_submission_limit(self, user_id: int) -> bool:
        """Check if user exceeded submission limit (5/day by default)."""
        return await self.check_limit(
            user_id,
            action="submit",
            max_count=RATE_LIMIT_SUBMISSIONS,
            window_hours=24
        )
    
    async def check_download_limit(self, user_id: int) -> bool:
        """Check if user exceeded download limit (5/hour)."""
        return await self.check_limit(
            user_id,
            action="download",
            max_count=5,
            window_hours=1
        )
    
    async def check_ai_request_limit(self, user_id: int) -> bool:
        """Check if user exceeded AI request limit (20/day)."""
        return await self.check_limit(
            user_id,
            action="ai_request",
            max_count=20,
            window_hours=24
        )
    
    async def get_remaining(
        self,
        user_id: int,
        action: str,
        max_count: int,
        window_hours: int
    ) -> int:
        """Get remaining actions before hitting limit."""
        await self._ensure_table()
        pool = await self._get_pool()
        
        now = datetime.utcnow()
        window_start = now - timedelta(hours=window_hours)
        
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) 
                FROM rate_limits 
                WHERE user_id = $1 
                AND action = $2 
                AND timestamp > $3
            """, user_id, action, window_start)
            
            return max(0, max_count - count)
    
    async def get_search_remaining(self, user_id: int) -> int:
        """Get remaining searches for user."""
        return await self.get_remaining(
            user_id,
            action="search",
            max_count=RATE_LIMIT_SEARCHES,
            window_hours=1
        )
    
    async def get_submission_remaining(self, user_id: int) -> int:
        """Get remaining submissions for user."""
        return await self.get_remaining(
            user_id,
            action="submit",
            max_count=RATE_LIMIT_SUBMISSIONS,
            window_hours=24
        )
    
    async def cleanup_old_records(self, days: int = 7):
        """
        Delete records older than N days (maintenance task).
        Call this periodically from a cron job or admin task.
        """
        await self._ensure_table()
        pool = await self._get_pool()
        
        cutoff = datetime.utcnow() - timedelta(days=days)
        
        async with pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM rate_limits WHERE timestamp < $1
            """, cutoff)
        
        logger.info(f"[v0] Cleaned up rate_limit records older than {days} days")


# Global instance
rate_limiter = PostgresRateLimiter()


# Legacy in-memory class for fallback (kept for backwards compatibility)
class RateLimiter:
    """DEPRECATED: Use PostgresRateLimiter instead. This in-memory version doesn't work on serverless."""
    
    def __init__(self):
        logger.warning(
            "[v0] RateLimiter (in-memory) is deprecated. "
            "Use PostgresRateLimiter for serverless deployments."
        )
        self.search_limits = {}
        self.submission_limits = {}
        self.download_limits = {}
        self.max_downloads_per_hour = 5
    
    def check_search_limit(self, user_id: int) -> bool:
        """DEPRECATED. Use rate_limiter.check_search_limit() instead."""
        logger.warning("[v0] In-memory rate limiter in use — limits NOT enforced on serverless")
        return True
    
    def check_submission_limit(self, user_id: int) -> bool:
        """DEPRECATED. Use rate_limiter.check_submission_limit() instead."""
        logger.warning("[v0] In-memory rate limiter in use — limits NOT enforced on serverless")
        return True
