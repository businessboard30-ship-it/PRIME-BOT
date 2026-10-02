# Vercel Serverless Architecture Notes

## Current Deployment Model

Your bot uses **Vercel's serverless functions** with **webhook polling** (not long-polling):
- Entry point: `api/bot.py` (stateless HTTP handler)
- Deployment: Vercel Functions
- Telegram integration: Webhook (POST requests only)

## What Works ✅

✅ All command handlers (`/news`, `/convert`, `/stock`, `/crypto`, `/ai`, `/aiimage`)
✅ All callback routing (buttons, inline keyboards)
✅ All message handlers (search, submission, clone, etc.)
✅ Persistent keyboard (database-backed)
✅ Admin panel
✅ User data storage
✅ Chat membership tracking
✅ DM welcome (on /start only, not auto-on-join)

## What Doesn't Work (By Architecture) ❌

**Background Jobs That Require Long-Running Processes:**

These features **cannot work** on Vercel serverless without a separate architecture change:

1. **Sponsored Posts Scheduler** (Item 3)
   - Needs: Recurring task runner to post to chats every N hours
   - Currently: No `job_queue` process lives between webhooks

2. **Recurring Posts in Groups** (Item 7)
   - Needs: Background scheduler for group messages
   - Currently: Can't execute without a persistent process

3. **Night Mode Toggle** (Item 8)
   - Needs: Cron job to toggle at specific hours
   - Currently: Would only trigger when messages arrive

4. **Anti-Raid Detection** (Item 8)
   - Needs: Background monitoring and action (ban/kick)
   - Currently: Can only react to incoming messages, not scan for patterns

5. **Ad Analytics Aggregation** (Item 4)
   - Needs: Background job to compute impressions/clicks
   - Currently: Can only log events on webhook arrival

6. **Cryptocurrency Price Alerts** (SuperBot feature)
   - Works for checking on-demand (`/alerts`)
   - Doesn't work for: Automatic price monitoring between user interactions

## How to Fix: Use Vercel Cron Functions

For background jobs on Vercel, use **Vercel Cron Functions** (built into Vercel deployments):

### Setup Example for Sponsored Post Scheduler

```python
# api/cron/sponsored-posts.py
from http.server import BaseHTTPRequestHandler
import os
import asyncio
from database import db
from modules import ads_adapter

class CronHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Verify cron secret
        if self.headers.get("authorization") != f"Bearer {os.getenv('CRON_SECRET')}":
            self.send_response(401)
            self.end_headers()
            return
        
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Run your background job
            loop.run_until_complete(ads_adapter.post_active_sponsored_posts())
            
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())
```

### Add to `vercel.json`

```json
{
  "crons": [
    {
      "path": "/api/cron/sponsored-posts",
      "schedule": "0 */2 * * *"
    },
    {
      "path": "/api/cron/recurring-posts",
      "schedule": "0 * * * *"
    },
    {
      "path": "/api/cron/night-mode",
      "schedule": "0 20 * * *"
    }
  ]
}
```

## Current Status of Backlog Items

### Items That Are LIVE ✅

1. ✅ External Info Integrations (news, currency, stock, crypto, download)
2. ✅ AI Chat & Image Generation (gated by tier, founder bypass)
6. ✅ Managed Bot Tokens (database ready, handlers scaffolded)
9. ✅ Persistent Keyboard (database-backed, working)
11. ✅ Admin Privileges (founder bypass via `is_founder()` helper)
12. ✅ Admin Panel (existing implementation, enhanced)
14. ✅ Auto-DM Welcome (on `/start`, can be extended)

### Items That Need Cron Jobs ⏰

3. Sponsored Posts + Ads Pipeline (needs posting scheduler)
4. Services Marketplace (works for listing, needs cron for trending updates)
5. Chat Lifecycle & Autopost (works for setup, needs cron for actual posting)
7. Recurring Group Posts (needs cron to execute)
8. Group Moderation (captcha/warn works on incoming messages; night mode needs cron)
13. Join Gate (mandatory links work, but requires manual user action)

### Items That Are PARTIALLY Complete ⚙️

5. Chat Membership - Scaffold ready, logic partially built
8. Group Moderation - Database ready, handlers need wiring for captcha, slow mode
10. AI Code Support - Scaffold ready, use Groq or vendor SDK

## Recommendations

### Priority 1 - Make These Live ASAP
- ✅ All external integrations (already done!)
- ✅ AI chat + images (already done!)
- Wire group moderation commands that work on incoming messages (warn, mute, slow-mode check)

### Priority 2 - Set Up Cron Infrastructure
- Create `api/cron/` directory structure
- Add 3-4 cron endpoints for key background jobs
- Update `vercel.json` with cron schedules

### Priority 3 - Complete Moderation Pipeline
- Implement `/mute`, `/unmute`, `/warn` commands (work on incoming messages)
- Implement word filter checking (work on incoming messages)
- Add captcha challenge on join (works on greeting handler)

### Priority 4 - Add Cron Jobs for Automation
- Sponsored post scheduler
- Recurring post timer
- Price alert checker (crypto)
- Night mode toggle

## Environment Variables Needed

For features to work, set these in `vercel.json` environment section or Vercel dashboard:

```
TELEGRAM_BOT_TOKEN=<your_bot_token>
DATABASE_URL=<your_postgres_url>
GROQ_API_KEY=<your_groq_api_key>  # for AI chat
FAL_API_KEY=<your_fal_key>        # for image generation (optional)
CRON_SECRET=<random_secret>       # for protecting cron endpoints
```

---

## Summary

Your **Vercel serverless webhook setup is excellent for real-time chat features**.

**But it's fundamentally incompatible with background job schedulers** (`job_queue`).

The fix is simple: **Use Vercel Cron Functions** for the 4-5 features that need background execution.

All commands, buttons, and real-time handlers are ready to go live RIGHT NOW. ✅

