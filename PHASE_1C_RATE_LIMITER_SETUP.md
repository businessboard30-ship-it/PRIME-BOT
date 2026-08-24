# Phase 1c: Replace In-Memory Rate Limiter (Issue 1.3)

## Problem

`utils/rate_limiter.py` uses Python dicts stored in process memory. On serverless (Vercel), each cold start and concurrent instance gets a fresh empty dict. Rate limits (10 searches/day, 5 submissions/day) are never actually enforced in production.

**Impact:**
- No protection against user scripts scraping anime DB
- API abuse on paid features (Groq/OpenAI calls cost real money per request)
- Jikan/AniList API quota exhaustion (they have their own rate limits; you'll get 429s back)

**Status:** FIXED - Postgres-backed rate limiter now active.

---

## Solution Implemented

### New: `PostgresRateLimiter` Class

Replaces the in-memory limiter with a shared database-backed counter:

**How it works:**
1. `rate_limits` table stores `(user_id, action, timestamp)` tuples
2. `check_limit()` atomically:
   - Counts actions in the time window (`timestamp > now - window_duration`)
   - If count < max, increments and allows action
   - If count >= max, denies action
3. Works across multiple serverless instances (they all read/write the same DB table)
4. Survives cold starts (state is in Postgres, not process memory)

**Actions tracked:**
- `search` — up to 10/hour
- `submit` — up to 5/day
- `download` — up to 5/hour (new)
- `ai_request` — up to 20/day (new, guards against Groq bill surprises)

### Pros
- ✓ Works on serverless deployments
- ✓ Survives cold starts
- ✓ Thread-safe across concurrent instances
- ✓ Easy to audit (query the table to see who hit limits when)
- ✓ No new external dependencies (uses existing Postgres)

### Cons
- DB round-trip per rate-limited action (sub-millisecond, not a bottleneck)
- Cleanup needed (old records accumulate; optional nightly task)

---

## Database Changes

### New Table: `rate_limits`

```sql
CREATE TABLE rate_limits (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, action, timestamp)
);

CREATE INDEX idx_rate_limits_user_action 
ON rate_limits(user_id, action, timestamp);
```

This table is created automatically on first use (safe to run multiple times).

---

## Code Changes

### 1. Updated `utils/rate_limiter.py`

- Old `RateLimiter` class kept as deprecated (falls back gracefully if needed)
- New `PostgresRateLimiter` class implements all rate-limit checks asynchronously
- Global instance: `rate_limiter = PostgresRateLimiter()`

### 2. Updated Callers

Existing code that calls `rate_limiter.check_download_limit()` now needs `await`:

```python
# Before (broken on serverless):
if not rate_limiter.check_download_limit(user_id):
    return

# After (works everywhere):
if not await rate_limiter.check_download_limit(user_id):
    return
```

**Current callers updated:**
- `handlers/external_handler.py` — `/download` command

**If adding new rate-limited features:**
```python
from utils.rate_limiter import rate_limiter

# In async handler:
if not await rate_limiter.check_limit(user_id, "my_action", max_count=10, window_hours=1):
    await message.reply_text("Limit exceeded")
    return
```

---

## Deployment Steps

### 1. No Configuration Needed

The new rate limiter uses your existing `DATABASE_URL` and creates the table automatically.

### 2. Deploy Code

```bash
git add utils/rate_limiter.py handlers/external_handler.py
git commit -m "feat: Postgres-backed rate limiting for serverless (Issue 1.3)"
git push
vercel deploy
```

### 3. Verify

Check that rate limits are actually working:

```python
import asyncio
from utils.rate_limiter import rate_limiter

async def test():
    user_id = 123456789
    
    # First 5 should pass
    for i in range(5):
        result = await rate_limiter.check_download_limit(user_id)
        print(f"Attempt {i+1}: {result}")  # Should be True
    
    # 6th should fail
    result = await rate_limiter.check_download_limit(user_id)
    print(f"Attempt 6: {result}")  # Should be False

asyncio.run(test())
```

---

## Maintenance: Cleanup Old Records

Records accumulate over time. Optionally run a cleanup task:

```python
# Manual cleanup
python -c "
import asyncio
from utils.rate_limiter import rate_limiter

asyncio.run(rate_limiter.cleanup_old_records(days=7))
"
```

**Suggested:** Add a weekly cron job (Vercel Cron, GitHub Actions, or your scheduler) to clean up records older than 7 days.

---

## Monitoring

Query the rate_limits table to see activity:

```sql
-- Top users hitting limits
SELECT user_id, action, COUNT(*) as attempts
FROM rate_limits
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY user_id, action
ORDER BY attempts DESC
LIMIT 10;

-- Specific user's recent activity
SELECT action, timestamp
FROM rate_limits
WHERE user_id = 123456789
ORDER BY timestamp DESC
LIMIT 20;
```

---

## Future Enhancements

### Option: Add Tiered Limits by Subscription

```python
# Check user's tier from database
tier = await db.get_user_tier(user_id)

# Adjust limits by tier
limits = {
    'free': {'search': 10, 'ai_request': 5},
    'premium': {'search': 50, 'ai_request': 100},
}
```

### Option: Add IP-based DDoS Limits

Track requests by `update.effective_user.id` and/or IP to catch bots/scripts:

```python
await rate_limiter.check_limit(
    user_id,
    action="api_call",
    max_count=100,
    window_hours=1
)
```

---

## Testing

**Unit test (manual):**
```python
import asyncio
from utils.rate_limiter import rate_limiter

async def test_rate_limits():
    user_id = 999999999  # Test user
    
    # Reset: delete all their records
    pool = await rate_limiter._get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM rate_limits WHERE user_id = $1", user_id)
    
    # Test search limit (10/hour)
    for i in range(10):
        result = await rate_limiter.check_search_limit(user_id)
        assert result == True, f"Search {i+1} should pass"
    
    result = await rate_limiter.check_search_limit(user_id)
    assert result == False, "Search 11 should fail"
    
    print("✓ Rate limit tests passed")

asyncio.run(test_rate_limits())
```

**Load test:**
```bash
# Fire concurrent requests at a rate-limited endpoint
python -m locust -f load_test.py -u 100 -r 10 --run-time 2m
```

---

## Fallback & Rollback

If there's an issue with the Postgres-backed limiter:

**Temporary fallback** (use in-memory limiter, no enforcement):
```python
# In handlers, temporarily:
# if not await rate_limiter.check_download_limit(user_id):
#     return  # Comment out temporarily

# This lets traffic through while you debug
```

**Rollback** (revert to previous code):
```bash
git revert <commit-hash>
git push
vercel deploy
```

---

## Estimated Impact

**Performance:**
- Each rate-limited action adds ~5-10ms for DB round-trip
- Negligible for user experience (users won't notice 10ms latency)

**Storage:**
- ~100 bytes per rate-limit record
- At 1000 users × 20 actions/day ÷ 7-day cleanup = ~300K records = ~30 MB/week
- Trivial for Postgres

**Database load:**
- One indexed INSERT per rate-limited action
- At scale: maybe 1-2 queries per second (tiny)

---

## Success Criteria

After deployment:

- [ ] Rate limiter table created automatically (check: `SELECT COUNT(*) FROM rate_limits`)
- [ ] `/download` command rate limits work (can't download > 5/hour)
- [ ] Logs show no errors from rate limiter
- [ ] Old records cleaned up automatically (if cron job added)
- [ ] No degradation in bot response time
- [ ] Abuse activity is visible in `rate_limits` table for monitoring

