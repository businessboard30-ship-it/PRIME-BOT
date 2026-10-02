# Clone/Main Bot Data Isolation Audit

## Fixed
- `bot_group_membership` — clone_id-scoped (groups/channels no longer leak
  across bots).
- Username-mention escaping bug ("username not found").
- **`users` tier/subscription/quota** — new `user_clone_status` table keyed
  by `(user_id, clone_id)`. Tier, ToS acceptance, subscription status/expiry,
  free-use counters, utility subscription, and language are now per-clone.
  `users` itself stays a single global identity row (username/first_name/
  is_admin/joined_date/submissions_count — kept global on purpose, and
  `submissions.user_id`'s foreign key requires it). Backfill migration
  copies existing data into clone_id=0 (main bot) so nothing was lost.
- **Subscription payments** — `activate_subscription`/`deactivate_subscription`/
  `get_active_subscribers` in `handlers/subscription.py` scoped by clone_id.
  The `ai_subscription` and `botstore_premium` Paystack flows now carry
  `clone_id` in payment metadata so the webhook (`api/paystack_webhook.py`,
  which has no Telegram context to read clone_config from) activates the
  correct bot's subscription/premium instead of defaulting to the main bot.
- **Payment gateway routing (optional, per clone)** — clone owners can now
  open a clone's **Payment Settings** (My Clones → a clone → 💳 Payment
  Settings) and choose: "Use Main Bot (default)" — all payments collected
  by the main bot's own Paystack, as instructed, until further notice — or
  connect their own Paystack/Stripe secret key, encrypted at rest with the
  same cipher used for clone bot tokens (`utils/crypto.py`'s
  `secret_manager`). Storage: `db.set_clone_payment_provider()` /
  `db.get_clone_payment_config()`, both in `database.py`. Every screen has
  a Back button.
  **Not yet done:** the actual payment-initiation code paths (botstore
  premium, AI subscription, clone-creation fee, group Pay Now button, etc.
  in `handlers/*.py` and `payments.py`) still always use the main bot's
  `PAYSTACK_SECRET_KEY` — none of them check `get_clone_payment_config()`
  yet to route to a connected owner key. That's the next step once you
  confirm you want per-clone charges to actually go out via Stripe too
  (right now `payments.py`'s `PaystackPayment` class only speaks Paystack's
  API; Stripe would need its own client).

## Still shared — group-scoped (low risk)
Keyed by Telegram `group_id`, unique per chat regardless of which bot is in
it — only a problem if a group has both the main bot and a clone added:
`group_moderation_settings`, `blocked_words`, `user_warns`,
`custom_link_buttons`, `custom_group_commands`, `join_gate_settings`,
`join_gate_verifications`, `chat_memberships`, `recurring_posts`,
`flood_events`, `link_whitelist_domains`, `moderation_logs`,
`user_group_events`.

## Still shared — user-scoped (left as-is per your instruction, ask before touching)
`payments`, `subscription_payments`, `payment_logs`, `clone_payments`,
`ai_chat_usage`, `ai_image_usage`, `superbot_user_tiers`,
`superbot_referrals`, `superbot_crypto_alerts`, `deep_links`, `user_shares`,
`user_write_access`, `user_analytics`, `user_games`, `user_locations`,
`proximity_alerts`, `user_profile_photos`, `web_app_sessions`,
`passport_verifications`, `inline_searches`, `shipping_orders`,
`submissions`.

Questions before I touch any of these:
1. `ai_chat_usage` / `ai_image_usage` — same treatment as the utility
   paywall (per-clone quotas)? These are the next-highest-risk ones since
   they gate a paid feature.
2. `payments` / `payment_logs` / `clone_payments` / `subscription_payments`
   — do you want these scoped by clone_id (filterable per bot) or does the
   main bot need an unfiltered view across all clones for reconciliation?
3. The rest (`deep_links`, `user_games`, `user_locations`, etc.) are
   lower-stakes — fine to leave fully shared, or scope them too while I'm
   in there?

## Still shared — marketplace/content (assumed intentional, confirm)
`botstore_listings`, `botstore_ratings`, `sponsored_posts`,
`ad_submissions`, `services_listings`, `ad_analytics`, `anime_entries`,
`categories`, `admin_config`, `broadcast_jobs`, `broadcast_recipients`,
`managed_bots`, `managed_bot_tokens`.

