# prime-bot-cron

Cloudflare Worker that replaces the GitHub Actions schedule in `.github/workflows/cron.yml`.
Deployed automatically by `.github/workflows/deploy-cron-worker.yml` on pushes touching `cron-worker/**`
(uses repo secrets `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CRON_SECRET`).

Manual run: `curl -H "Authorization: Bearer $CRON_SECRET" "https://<worker-url>/?group=frequent"`
If the backend URL changes, update `BACKEND_URL` in `wrangler.toml`.
