# Stable-URL redirector (GitHub Pages)

Static pages that forward the *browser* to wherever the backend/dashboard currently lives.
**Change the host in one place: `redirector/config.json`** (and push). Nothing else moves.

| Stable URL (under your Pages site)      | Forwards to                          |
|-----------------------------------------|--------------------------------------|
| `/oauth/discord-login/`                 | `{backend_url}/api/discord_login_oauth` |
| `/oauth/gdrive/`                        | `{backend_url}/api/oauth_gdrive`     |
| `/pay/`                                 | `{backend_url}/pay`                  |
| `/terms/` `/privacy/` `/pricing/` `/refund/` | `{backend_url}/terms` etc.      |
| `/manual/`                              | `{dashboard_url}/manual`             |

`?query` and `#hash` are preserved (OAuth `code`/`state` pass straight through).
**Not for webhooks** (Gumroad/Paystack/Telegram/Discord interactions): those are
server-to-server POSTs and static hosting can't forward them.

## One-time setup
1. Repo → Settings → Pages → Source: **GitHub Actions** (the existing deploy workflow now ships this folder too).
2. Repo → Settings → Secrets → Actions: add **`CRON_SECRET`** (same value as on the backend). Optional variable `UNLOCK_PAGES_BASE_PATH=/PRIME-BOT` if this is a project page.
3. Register the new URLs, e.g. `https://<user>.github.io/PRIME-BOT/oauth/discord-login/` (with trailing slash) in the Discord Developer Portal, and `/oauth/gdrive/` in Google Cloud.
4. On the backend set `STABLE_BASE_URL=https://<user>.github.io/PRIME-BOT` (blank = old behaviour).
5. Turn off any other scheduler calling the `/api/cron_*` URLs; `.github/workflows/cron.yml` now does it.

## Moving hosts later
Edit `backend_url` in `config.json`, commit to `main`. Done.
