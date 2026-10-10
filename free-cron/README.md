# free-cron

A standalone, public cron service on Cloudflare Workers + D1. It has its own Worker and database.

- Sign in with **GitHub** (public id + username only, no scope). Cloudflare **Turnstile** guards sign-in and every create.
- **5 free crons** per account, minimum interval 15 minutes. **Premium: unlimited**, minimum 5 minutes.
- A cron calls a public `https://` URL (GET, or POST with a body up to 1 KB) every 5 / 15 / 30 min, 1 / 6 / 12 / 24 h. No free-form cron strings.
- One Cloudflare trigger (`*/5 * * * *`) runs everyone's due crons (40 per tick, oldest first).

## Safety rules (all tested)
- https only, standard port, domain names only. IP literals (any notation), `localhost`, `.local/.internal/...`, credentials in the URL and this service's own host are refused at create **and re-checked on every run**.
- Redirects are never followed; each call has a 10 s timeout; responses are **never read or stored** (only the status code and time).
- 20 creates per account per 24 h, counted atomically. A cron switches itself off after 5 failures in a row.
- Session cookie is HMAC-signed, HttpOnly, Secure, SameSite=Lax; state-changing calls need the `X-FreeCron` header and a matching Origin. Strict CSP with a per-response nonce.
- The GitHub token is used once to read the id and login, then dropped. "Delete my account" removes everything.
- Kill switch: set the `DISABLED=1` variable and nothing runs.

## One-time setup (owner)
1. **GitHub OAuth app** (github.com/settings/developers > New OAuth App): callback URL `https://<your-free-cron-host>/auth/callback`. Put the Client ID in `wrangler.toml` `GITHUB_CLIENT_ID`.
2. **Turnstile widget** (Cloudflare dashboard > Turnstile > Add site) for the same host. Put the site key in `wrangler.toml` `TURNSTILE_SITE_KEY`.
3. **Repo secrets:** `FREECRON_GITHUB_CLIENT_SECRET`, `FREECRON_TURNSTILE_SECRET`, `FREECRON_SESSION_SECRET` (any long random string), `FREECRON_ADMIN_KEY` (long random string). `CLOUDFLARE_API_TOKEN` needs **D1:Edit** as well as Workers Scripts:Edit; `CLOUDFLARE_ACCOUNT_ID` is shared with `cron-worker`.
4. Merge: `.github/workflows/deploy-free-cron.yml` runs the tests, applies `migrations/`, deploys and syncs secrets. The D1 database `free-cron` already exists (id in `wrangler.toml`).

## Premium (automatic, Gumroad)
Buyers get a **Gumroad license key**, paste it on the page while signed in, and Premium switches on. Nothing for the owner to do.
- Memberships (`GUMROAD_RECURRING = "1"`): the key is re-verified with Gumroad about once a day. While it is paid, Premium rolls forward; if it is cancelled, refunded, disputed or its payment fails, Premium lapses on its own within 3 days. A Gumroad outage never ends anyone's Premium.
- One-time purchases (`GUMROAD_RECURRING = "0"`): each key gives `PREMIUM_DAYS` of Premium once.
- A key belongs to the first account that redeems it. Redeeming needs the Turnstile check.
- Setup: in Gumroad enable **license keys** on the product. Put the product id in `GUMROAD_PRODUCT_ID` and the product link in `GUMROAD_BUY_URL` (both in `wrangler.toml`).
- Backup: the owner can still grant by hand:
```
curl -X POST https://<host>/api/admin/premium -H "Authorization: Bearer $ADMIN_KEY" -H "X-FreeCron: 1" -H "Content-Type: application/json" -d '{"login":"their-github-name","days":30}'
```
`days: 0` revokes. When Premium lapses the account is free again: only its 5 oldest crons keep running and intervals under 15 minutes are stretched to 15.

## Develop
`cd free-cron && npm test` (Node 22+, uses the built-in SQLite as a stand-in for D1).

## Status pages

A signed-in user can publish up to 1 page (Premium 10) listing up to 5 crons (Premium 50) under labels they choose.
Public routes need no login: `GET /s/:slug` (HTML, no script) and `GET /api/public/status/:slug` (JSON). Both show only the label, up or down,
uptime for 24 h and 7 d (30 d for Premium only, because free accounts keep 7 days of runs) and the last-checked time. Never a URL, method, body or status text.
Slugs are random by default. Pages are `noindex`, cached for 60 s, and 404 when switched off or blocked.
Block a page fast (abuse): `POST /api/admin/status-page` with `Authorization: Bearer <ADMIN_KEY>` and `{"slug":"...","blocked":true}`.
Set `CONTACT_URL` (https or mailto) in `wrangler.toml` to show a contact on the legal pages and a "Report this page" link on status pages.

## Failure alerts

Settings, Failure alerts: save a Discord webhook (`https://discord.com/api/webhooks/<id>/<token>`, also `discordapp.com`; anything else is rejected, so it cannot be used to reach other hosts).
The webhook is a credential: the API only returns it masked, and it is never logged. Alerts are sent on state changes only: after 2 failures in a row (down), when a cron is switched off after 5 (disabled), and when it recovers.
Messages never contain the URL. Max 20 alerts per account per day. Alerts are queued in `alert_outbox` and sent (3 per tick, 5 s timeout, 3 tries) after `runDue()` finishes, so a broken webhook cannot affect the crons.
