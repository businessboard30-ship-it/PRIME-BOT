# Task 11 Pre-Analysis Findings

## Additional Findings Beyond Bugs #1-11

### Context State Management Issues
- **Issue A1:** `context.user_data` is used for multi-step flows across 6+ handlers but has no collision detection mechanism
  - Keys: `submission_step`, `submit_step`, `botstore_mode`, `alert_step`, `clone_step`, `payment_reference`, `clone_payment_pending`, etc.
  - Risk: User switching between flows rapidly could corrupt state
  - Impact: Low-medium (requires specific user behavior sequence)
  - Fix in Task 10.2 (structured logging)

- **Issue A2:** `discover.py` uses in-process cache (`user_pages`, `anime_cache` dicts on line 11-12)
  - Problem: Does not persist across serverless invocations
  - Solution: Move to Postgres or Redis in Task 6 (rate limiter pool migration)

### Rate Limiter Not Wired
- **Search.py line 31:** Sets `context.user_data["mode"] = "search"` but never calls `rate_limiter.check_search_limit()`
- **Submit.py line 20:** Sets `context.user_data["submission_flow"] = True` but never calls rate limiter
- **Discover.py:** No rate limiting at all despite fetching data
- **Fix in Task 6**

### Missing Admin Authorization Checks
- **admin_config.py lines 26-29:** Displays internal adapter constants (`botstore_adapter.FEATURED_PRICE_GHS`) without escaping — if these ever contain user data, could be injection vector (low risk but should escape)
- **admin_config.py line 18:** Checks `user_id != ADMIN_ID` correctly ✅

### Bare Except Statements
- **main.py line 137:** `except:` (mentioned in audit Task 10)
- **feature_handlers.py line 246:** `except:` (mentioned in audit Task 10)
- **Also found in:** handlers/admin_panel.py (grep needed to confirm)

### Unwired Adapter References
- **botstore_adapter** imported in admin_config.py but never actually used for writes — only reads constants
- **superbot_adapter** imported in admin_config.py but never actually used for writes — only reads constants
- Both adapters are imported but their functions aren't called anywhere in active handlers (low risk, just dead imports)

### Payment Flow Issues Beyond #1-2
- **Issue A3:** `clone_bot.py` still has references to my previous incomplete fixes (e.g., `context.user_data["payment_reference"]` stored but never verified in DB before finalize)
- **Issue A4:** Subscription `activate_subscription()` function signature not confirmed to exist (must verify before Task 1's webhook tries to call it)
- **Fix in Tasks 1-3**

### Database State Queries with No Indexes
- **High-cost queries:** `submissions.status`, `botstore_listings.status`, `users.subscription_status` all used in WHERE clauses (found via audit)
- **Confirmed missing:** Trigram GIN index on `anime_entries.title` for text search
- **Fix in Task 10**

## Files Scanned (Task 11 Phase 1)
✅ admin_config.py
✅ discover.py  
✅ search.py
✅ submit.py
✅ (Additional: admin_panel.py, botstore_handler.py, superbot_handler.py, clone_bot.py)

## Files Still Need Full Scan (Task 11 Phase 1 continuation)
- handlers/feature_handlers.py
- keyboards.py
- formatter.py
- disclaimers.py
- init_system.py
- anime_service.py
- modules/botstore_adapter.py
- modules/superbot_adapter.py
- modules/external_apis.py
- modules/ai_features.py
- (Next.js app if present)

## Summary: Additional Bugs Identified
- **A1:** Context state collision risk (low impact)
- **A2:** In-process cache doesn't survive serverless restarts (medium impact)  
- **A3:** Clone/subscription flows still broken per audit (high impact - covered by Tasks 1-3)
- **A4:** Bare except statements (low impact, covered by Task 10)
- **A5:** Unindexed frequently-queried columns (medium impact on performance)

All of the above are covered by the recommended task execution order. Proceeding with Tasks in order.
