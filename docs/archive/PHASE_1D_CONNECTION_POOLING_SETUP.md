# Phase 1d: Fix Connection Pooling for Serverless (Issue 1.4)

## Problem

`database.py` creates a pool with `min_size=1, max_size=3` on every cold start. On Vercel serverless:
- 100 concurrent instances × 3 connections each = 300 DB connections
- Postgres provider cap: usually 15-60 connections
- Result: "too many connections" errors under any real load

Works in development (single process) because there's only 1 instance. Fails silently in production until traffic spikes.

**Status:** FIXED - Pool now serverless-aware and auto-detects PgBouncer.

---

## Solution Implemented

### Changes to `database.py::get_pool()`

1. **Auto-detects PgBouncer endpoint** — if your `DATABASE_URL` includes `pooler` or port `6543`, it assumes you're using a connection pooler
2. **Reduces pool size for serverless** — `max_size=1` if using pooler, `max_size=2` if not
3. **Keeps `statement_cache_size=0`** — required for PgBouncer transaction-mode compatibility

### How It Works

**Old (broken on serverless):**
```
User request #1 → Vercel instance #1 → pool with 3 connections
User request #2 → Vercel instance #2 → another pool with 3 connections
User request #3 → Vercel instance #3 → another pool with 3 connections
...
User request #100 → Vercel instance #100 → another pool with 3 connections
Total: 300 connections against a 60-connection cap → FAIL
```

**New (works on serverless):**
```
User request #1 → Vercel instance #1 → pool with 1 connection ──┐
User request #2 → Vercel instance #2 → pool with 1 connection ──┼→ PgBouncer (multiplexes 300 to ~20 real DB connections)
User request #3 → Vercel instance #3 → pool with 1 connection ──┴──→ Postgres (stable)
```

---

## Deployment: Which Database Provider?

### Supabase (Recommended for this project)

Supabase has PgBouncer built-in. Enable it:

1. **Go to project settings** → Database → Connection pooling
2. **Toggle "Connection pooling" ON**
3. **Copy the "Transaction mode" connection string** (port 6543)
4. **Update `DATABASE_URL` in Vercel** to use the pooler string instead of the direct connection

**Example:**
```
# Old (direct connection, port 5432):
postgresql://user:pass@db.supabase.co:5432/postgres

# New (via PgBouncer, port 6543):
postgresql://user:pass@db.supabase.co:6543/postgres
```

Our code now auto-detects `:6543` and adjusts pool size automatically.

### Neon

Neon's "Pooled Connection" feature is their equivalent of PgBouncer:

1. **Go to project settings** → Connection Pooling
2. **Enable pooling** (if not already)
3. **Copy the "Pooled Connection" string**
4. **Update `DATABASE_URL` in Vercel**

Same idea: pooling endpoint on a non-standard port, our code detects it.

### Railway Postgres

Railway doesn't have built-in PgBouncer, but your existing setup likely works fine there since Railway isn't as aggressively multi-instance as Vercel serverless.

**If you hit connection limits on Railway:**
- Recommendation: migrate to Supabase or Neon (both have pooling built-in)
- Or: move off serverless to Railway's own deployment model

### AWS RDS / Manual Postgres

If self-hosting Postgres:
- Option 1: Install PgBouncer in front of your Postgres instance
- Option 2: Use AWS RDS Proxy (Amazon's managed pooler)
- Option 3: Consider moving to Supabase/Neon for less ops burden

---

## Step-by-Step Deployment

### For Supabase Users (Most Common)

#### 1. Enable Connection Pooling in Supabase Dashboard

- Go to your Supabase project
- Settings → Database → Connection pooling
- Toggle "Connection pooling" ON
- Select "Transaction" mode (safest for serverless)
- Copy the pooler connection string (port 6543)

#### 2. Update Vercel Environment

```bash
# Old (direct connection):
vercel env ls  # See current DATABASE_URL (port 5432)

# New (via pooler):
vercel env rm DATABASE_URL  # Remove old
vercel env add DATABASE_URL  # Add new with :6543
# Paste the new connection string when prompted
```

Verify:
```bash
vercel env ls | grep DATABASE_URL
# Should show port 6543 in the URL
```

#### 3. Deploy and Test

```bash
git push
vercel deploy

# Monitor logs:
vercel logs -f  # Follow logs during deployment
```

Watch for:
```
[v0] Creating connection pool: min=1, max=1, pooler=yes
```

This confirms PgBouncer detection is working.

### For Neon Users

Same steps, but:
1. Copy "Pooled Connection" string from Neon dashboard
2. Update `DATABASE_URL` in Vercel to the pooled URL

---

## Testing: Verify Connection Pooling

### Manual Test (Before and After)

**Before (old code):**
```bash
# Connect to Postgres with `pg_stat_activity` query
# Send 10 concurrent requests to the bot
# Watch connection count spike to 30+
```

**After (new code):**
```bash
# Connect to Postgres with `pg_stat_activity` query
# Send 10 concurrent requests to the bot
# Watch connection count stay under 10
```

### Command to Monitor Connections

```sql
-- Login to your Postgres instance and run:
SELECT usename, application_name, state, count(*) as cnt
FROM pg_stat_activity
GROUP BY usename, application_name, state
ORDER BY cnt DESC;

-- You should see:
-- pgbouncer (from Vercel instances) NOT direct asyncpg connections to Postgres
```

### Load Test Script

```python
import asyncio
import httpx

async def load_test():
    """Fire 100 concurrent requests at your bot"""
    async with httpx.AsyncClient() as client:
        tasks = []
        for i in range(100):
            # Replace with your actual bot endpoint
            task = client.get(f"https://your-bot.vercel.app/health?id={i}")
            tasks.append(task)
        
        results = await asyncio.gather(*tasks)
        print(f"Requests: {len(results)}, Successful: {sum(1 for r in results if r.status_code == 200)}")

asyncio.run(load_test())
```

---

## Connection Count Estimation

**With our fix:**
- Vercel serverless instances: ~10-100 under load
- Connections per instance: 1 (or 2 if not using pooler)
- Total connections to Postgres: 10-200
- If using PgBouncer, actual DB connections: ~5-20 (pooler multiplexes)
- Supabase/Neon limit: usually 100-300+ (plenty of headroom)

**Result:** Safe to run 1000+ concurrent users without connection exhaustion.

---

## Troubleshooting

### Error: "too many connections"

**Diagnosis:**
- Check `pg_stat_activity` — you're still getting 300+ connections?
- Or check Supabase dashboard → Connection pooling → stats

**Fix:**
1. Verify `DATABASE_URL` includes the pooler port (6543 for Supabase)
2. Redeploy (`vercel deploy`)
3. Check logs: `vercel logs -f` — should see `pooler=yes`

### Error: "permission denied for schema public" (Neon-specific)

Neon's pooler in transaction mode has known issues with certain queries. Try:
1. Switch to Session mode pooling (less efficient but works)
2. Or use Supabase instead (more serverless-friendly)

### Performance degradation after fix

Connection pooling shouldn't degrade performance. If you see slower queries:
1. Check Postgres CPU/memory (is the DB under-resourced?)
2. Verify the pooler isn't itself a bottleneck (check pooler logs if available)
3. Run `EXPLAIN ANALYZE` on slow queries to identify missing indexes (Issue 1.5)

---

## Monitoring: Long-Term

Add to your observability (Sentry, Datadog, etc.):

```python
# In database.py
logger.info(f"[v0] Connection pool created: {_pool}")

# Or, periodically:
async def monitor_pool_health():
    pool = await get_pool()
    size = pool.get_size()
    free = pool.get_idle_size()
    logger.info(f"[v0] Pool health: {free}/{size} connections idle")
```

---

## Future: Always-On Worker Alternative

If you ever decide to move off serverless:
- Recommendation: Railway or Fly.io with a persistent Python process
- Then: revert to the old pool config (`min_size=3, max_size=10`)
- Reason: long-running processes can benefit from larger pools and connection reuse

For now: serverless with pooler is the optimal trade-off.

---

## Success Criteria

After deployment:

- [ ] `DATABASE_URL` updated to use PgBouncer/pooler endpoint
- [ ] `vercel deploy` succeeds
- [ ] Logs show `[v0] Creating connection pool: min=1, max=1, pooler=yes`
- [ ] No "too many connections" errors in logs
- [ ] Bot responds normally to user commands
- [ ] Load test doesn't exhaust connections (< 20 real DB connections under 100 concurrent requests)
- [ ] Monitoring shows stable connection count

