# prime-bot-cron

Cloudflare Worker that replaces the GitHub Actions schedule in `.github/workflows/cron.yml`.
Deployed automatically by `.github/workflows/deploy-cron-worker.yml` on pushes touching `cron-worker/**`
(uses repo secrets `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CRON_SECRET`).

Manual run: `curl -H "Authorization: Bearer $CRON_SECRET" "https://<worker-url>/?group=frequent"`
If the backend URL changes, update `BACKEND_URL` in `wrangler.toml`.

## Endpoints by group (`src/index.js`)

| Group | Schedule (UTC) | Endpoints |
|---|---|---|
| `frequent` | every 5 minutes | `cron_discord_announcements`, `cron_discord_owner_broadcast`, `cron_dash_dropbox_dm`, `cron_dev_scheduled` |
| `hourly` | `:17` | `cron_ad_placement` |
| `daily` | 03:23 | `cron_expire_monetization`, `cron_renew_yandex_search`, `cron_cleanup_pending_payments` |

### `cron_dev_scheduled` (Developer-mode scheduled jobs)

One endpoint serves every member's jobs. Cloudflare triggers are fixed in `wrangler.toml`, so there is **no trigger per user**: the backend
reads the `dev_jobs` table and runs whatever is due on each 5-minute tick (so a job can start up to ~5 minutes after its slot). Jobs use
allowlisted presets (hourly / daily / weekly, UTC, never closer than 1 hour), at most 5 per person. Owner kill switch: `dev_jobs` in the owner panel.
Paused jobs (lapsed Developer plan) are deleted after 30 days by `cron_expire_monetization`.
